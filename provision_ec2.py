#!/usr/bin/env python3
"""Interactive CLI that sizes an EC2 instance from your requirements, then
provisions it end to end: picks an instance type, resolves the latest AMI for
the OS you choose, sets up networking/security group/key pair, injects users
via cloud-init, and launches the instance.

Credentials are never typed into this script. It uses boto3's normal chain
(--profile, environment variables, or an instance/role profile) via
`aws configure` beforehand. Run `python3 provision_ec2.py --dry-run` to see
every decision and the exact launch parameters without calling AWS.
"""

import argparse
import ipaddress
import os
import sys
import time
import urllib.request

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound

INSTANCE_CATALOG = [
    # type, vcpu, mem_gib, arch, family
    ("t3.micro", 2, 1, "x86_64", "burstable"),
    ("t3.small", 2, 2, "x86_64", "burstable"),
    ("t3.medium", 2, 4, "x86_64", "burstable"),
    ("t3.large", 2, 8, "x86_64", "burstable"),
    ("t3.xlarge", 4, 16, "x86_64", "burstable"),
    ("t4g.micro", 2, 1, "arm64", "burstable"),
    ("t4g.small", 2, 2, "arm64", "burstable"),
    ("t4g.medium", 2, 4, "arm64", "burstable"),
    ("t4g.large", 2, 8, "arm64", "burstable"),
    ("t4g.xlarge", 4, 16, "arm64", "burstable"),
    ("m6i.large", 2, 8, "x86_64", "general"),
    ("m6i.xlarge", 4, 16, "x86_64", "general"),
    ("m6i.2xlarge", 8, 32, "x86_64", "general"),
    ("m6g.large", 2, 8, "arm64", "general"),
    ("m6g.xlarge", 4, 16, "arm64", "general"),
    ("m6g.2xlarge", 8, 32, "arm64", "general"),
    ("c6i.large", 2, 4, "x86_64", "compute"),
    ("c6i.xlarge", 4, 8, "x86_64", "compute"),
    ("c6i.2xlarge", 8, 16, "x86_64", "compute"),
    ("c6g.large", 2, 4, "arm64", "compute"),
    ("c6g.xlarge", 4, 8, "arm64", "compute"),
    ("c6g.2xlarge", 8, 16, "arm64", "compute"),
    ("r6i.large", 2, 16, "x86_64", "memory"),
    ("r6i.xlarge", 4, 32, "x86_64", "memory"),
    ("r6g.large", 2, 16, "arm64", "memory"),
    ("r6g.xlarge", 4, 32, "arm64", "memory"),
]

OS_CATALOG = {
    "1": {
        "label": "Amazon Linux 2023",
        "owners": ["amazon"],
        "name_pattern": "al2023-ami-*-{arch}",
        "default_user": "ec2-user",
        "sudo_group": "wheel",
        "windows": False,
    },
    "2": {
        "label": "Ubuntu 24.04 LTS (Noble)",
        "owners": ["099720109477"],
        "name_pattern": "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-{arch}-server-*",
        "default_user": "ubuntu",
        "sudo_group": "sudo",
        "windows": False,
    },
    "3": {
        "label": "Ubuntu 22.04 LTS (Jammy)",
        "owners": ["099720109477"],
        "name_pattern": "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-{arch}-server-*",
        "default_user": "ubuntu",
        "sudo_group": "sudo",
        "windows": False,
    },
    "4": {
        "label": "Red Hat Enterprise Linux 9",
        "owners": ["309956199498"],
        "name_pattern": "RHEL-9*_HVM-*-{arch}-*-Hourly2-GP3",
        "default_user": "ec2-user",
        "sudo_group": "wheel",
        "windows": False,
    },
    "5": {
        "label": "Windows Server 2022 (base)",
        "owners": ["amazon"],
        "name_pattern": "Windows_Server-2022-English-Full-Base-*",
        "default_user": "Administrator",
        "sudo_group": None,
        "windows": True,
        "arch_only": "x86_64",
    },
}


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value if value else (default or "")


def ask_yes_no(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    value = input(f"{prompt} ({hint}): ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def ask_choice(prompt, options):
    """options: list of (key, label). Returns the chosen key."""
    print(prompt)
    for key, label in options:
        print(f"  {key}) {label}")
    valid = {k for k, _ in options}
    while True:
        choice = input("> ").strip()
        if choice in valid:
            return choice
        print(f"Please enter one of: {', '.join(sorted(valid))}")


def ask_int(prompt, default):
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print("Enter a whole number.")


def detect_public_ip():
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=3) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None


def choose_instance_type():
    print("\n== Sizing the instance ==")
    vcpu = ask_int("Minimum vCPUs needed", 2)
    mem = ask_int("Minimum memory needed (GiB)", 4)
    family = ask_choice(
        "Workload type:",
        [
            ("1", "General purpose (balanced)"),
            ("2", "Burstable / low, spiky traffic (cheapest)"),
            ("3", "Compute optimized (CPU-bound, batch/render)"),
            ("4", "Memory optimized (databases, caches)"),
        ],
    )
    family_map = {"1": "general", "2": "burstable", "3": "compute", "4": "memory"}
    wanted_family = family_map[family]
    prefer_graviton = ask_yes_no(
        "Prefer AWS Graviton (arm64) for lower cost? Requires arm64-compatible software", True
    )

    arch_pref = "arm64" if prefer_graviton else "x86_64"
    candidates = [
        c for c in INSTANCE_CATALOG
        if c[1] >= vcpu and c[2] >= mem and c[3] == arch_pref and c[4] == wanted_family
    ]
    if not candidates:
        # relax arch, then relax family, in that order
        candidates = [c for c in INSTANCE_CATALOG if c[1] >= vcpu and c[2] >= mem and c[4] == wanted_family]
    if not candidates:
        candidates = [c for c in INSTANCE_CATALOG if c[1] >= vcpu and c[2] >= mem]
    if not candidates:
        candidates = sorted(INSTANCE_CATALOG, key=lambda c: (c[1], c[2]))[-5:]

    candidates.sort(key=lambda c: (c[1] * c[2], c[1], c[2]))
    top = candidates[:5]

    print("\nBest-fit instance types:")
    options = []
    for itype, v, m, arch, fam in top:
        label = f"{itype}  ({v} vCPU, {m} GiB RAM, {arch}, {fam})"
        options.append((itype, label))
    options.append(("custom", "Enter a specific instance type manually"))
    chosen = ask_choice("Pick one:", [(k, v) for k, v in options])

    if chosen == "custom":
        itype = ask("Instance type (e.g. m6i.large)")
        arch = "arm64" if itype.split(".")[0].endswith("g") or "g." in itype else "x86_64"
        return itype, arch

    arch = next(c[3] for c in top if c[0] == chosen)
    return chosen, arch


def choose_os(architecture):
    print("\n== Operating system ==")
    options = [(k, v["label"]) for k, v in OS_CATALOG.items()]
    key = ask_choice("Choose an OS:", options)
    os_info = OS_CATALOG[key]
    if os_info.get("arch_only") and os_info["arch_only"] != architecture:
        print(f"Note: {os_info['label']} only ships for x86_64; overriding architecture choice.")
        architecture = os_info["arch_only"]
    return os_info, architecture


def resolve_ami(ec2, os_info, architecture):
    name = os_info["name_pattern"].format(arch=architecture)
    resp = ec2.describe_images(
        Owners=os_info["owners"],
        Filters=[
            {"Name": "name", "Values": [name]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "architecture", "Values": [architecture]},
            {"Name": "root-device-type", "Values": ["ebs"]},
        ],
    )
    images = resp.get("Images", [])
    if not images:
        sys.exit(f"No AMI found matching '{name}' for {architecture}. Try a different OS.")
    images.sort(key=lambda i: i["CreationDate"], reverse=True)
    latest = images[0]
    return latest["ImageId"], latest["Name"]


def choose_network(ec2, region):
    print("\n== Networking ==")
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}]).get("Vpcs", [])
    if vpcs:
        vpc_id = vpcs[0]["VpcId"]
        print(f"Using default VPC: {vpc_id}")
    else:
        vpc_id = ask("No default VPC found. Enter a VPC ID to use")

    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]).get("Subnets", [])
    if not subnets:
        sys.exit(f"No subnets found in VPC {vpc_id}.")
    print("Available subnets:")
    for s in subnets:
        print(f"  {s['SubnetId']}  {s['AvailabilityZone']}  {s['CidrBlock']}")
    default_subnet = subnets[0]["SubnetId"]
    subnet_id = ask("Subnet ID to launch into", default_subnet)

    assign_public_ip = ask_yes_no("Auto-assign a public IP to the instance?", True)

    make_sg = ask_yes_no("Create a new security group for this instance?", True)
    if make_sg:
        my_ip = detect_public_ip()
        default_cidr = f"{my_ip}/32" if my_ip else "0.0.0.0/0"
        ssh_cidr = ask("CIDR allowed to reach SSH/RDP (your public IP recommended)", default_cidr)
        try:
            ipaddress.ip_network(ssh_cidr)
        except ValueError:
            sys.exit(f"'{ssh_cidr}' is not a valid CIDR block.")
        open_web = ask_yes_no("Also open HTTP (80) and HTTPS (443) to the world?", False)

        sg_name = ask("Security group name", "provision-ec2-sg")
        sg = ec2.create_security_group(
            GroupName=f"{sg_name}-{int(time.time())}",
            Description="Created by provision_ec2.py",
            VpcId=vpc_id,
        )
        sg_id = sg["GroupId"]
        perms = [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": ssh_cidr, "Description": "SSH"}],
            },
            {
                "IpProtocol": "tcp",
                "FromPort": 3389,
                "ToPort": 3389,
                "IpRanges": [{"CidrIp": ssh_cidr, "Description": "RDP"}],
            },
        ]
        if open_web:
            perms += [
                {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP"}]},
                {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                 "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS"}]},
            ]
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=perms)
        print(f"Created security group {sg_id}")
    else:
        sg_id = ask("Existing security group ID to use")

    return vpc_id, subnet_id, [sg_id], assign_public_ip


def choose_key_pair(ec2):
    print("\n== SSH key pair ==")
    existing = ec2.describe_key_pairs().get("KeyPairs", [])
    if existing:
        print("Existing key pairs:", ", ".join(k["KeyName"] for k in existing))
    use_existing = existing and ask_yes_no("Use an existing key pair?", True)
    if use_existing:
        return ask("Key pair name", existing[0]["KeyName"])

    key_name = ask("Name for a new key pair to create", "provision-ec2-key")
    resp = ec2.create_key_pair(KeyName=key_name, KeyType="ed25519")
    pem_path = os.path.join(os.getcwd(), f"{key_name}.pem")
    with open(pem_path, "w", encoding="utf-8") as f:
        f.write(resp["KeyMaterial"])
    os.chmod(pem_path, 0o400)
    print(f"Saved private key to {pem_path} (chmod 400). Keep it safe — AWS does not store a copy.")
    return key_name


def collect_users(default_user, os_info):
    print("\n== Users ==")
    print(f"Default login user for this AMI is '{default_user}'.")
    if os_info["windows"]:
        print("Windows AMIs don't take cloud-init user-data here; use the EC2 "
              "'Get Windows password' flow for Administrator access, or RDP in "
              "and create users manually / via a PowerShell user-data script.")
        return []

    users = []
    if ask_yes_no("Add extra Linux users via cloud-init?", False):
        while True:
            username = ask("Username (blank to stop)")
            if not username:
                break
            is_admin = ask_yes_no(f"Give '{username}' passwordless sudo?", True)
            pubkey = ask(f"Paste an SSH public key for '{username}' (blank = none)")
            users.append({"name": username, "sudo": is_admin, "ssh_key": pubkey})
    return users


def build_user_data(users, os_info):
    if not users:
        return None
    lines = ["#cloud-config", "users:", "  - default"]
    for u in users:
        lines.append(f"  - name: {u['name']}")
        lines.append("    shell: /bin/bash")
        if u["sudo"]:
            lines.append(f"    groups: [{os_info['sudo_group']}]")
            lines.append("    sudo: ['ALL=(ALL) NOPASSWD:ALL']")
        if u["ssh_key"]:
            lines.append("    ssh_authorized_keys:")
            lines.append(f"      - {u['ssh_key']}")
    return "\n".join(lines) + "\n"


def confirm_summary(**kwargs):
    print("\n== Launch summary ==")
    for key, value in kwargs.items():
        print(f"  {key}: {value}")
    return ask_yes_no("\nLaunch this instance now? This will incur AWS charges.", False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="AWS CLI profile to use (default: default chain)")
    parser.add_argument("--region", help="AWS region (default: profile/env default)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Walk through all prompts and print the plan, but never call AWS to launch anything")
    args = parser.parse_args()

    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
    except ProfileNotFound as e:
        sys.exit(str(e))

    if not session.region_name:
        region = ask("AWS region", "us-east-1")
        session = boto3.Session(profile_name=args.profile, region_name=region)

    sts = session.client("sts")
    try:
        identity = sts.get_caller_identity()
    except (NoCredentialsError, ClientError) as e:
        sys.exit(f"Could not authenticate to AWS: {e}\nRun 'aws configure' or set --profile.")

    print(f"Authenticated as {identity['Arn']} (account {identity['Account']}) in {session.region_name}")

    ec2 = session.client("ec2")

    instance_type, architecture = choose_instance_type()
    os_info, architecture = choose_os(architecture)
    ami_id, ami_name = resolve_ami(ec2, os_info, architecture)
    print(f"Resolved AMI: {ami_id} ({ami_name})")

    vpc_id, subnet_id, sg_ids, assign_public_ip = choose_network(ec2, session.region_name)
    key_name = choose_key_pair(ec2)

    users = collect_users(os_info["default_user"], os_info)
    user_data = build_user_data(users, os_info)

    print("\n== Storage ==")
    volume_size = ask_int("Root volume size (GiB)", 20 if not os_info["windows"] else 30)

    name_tag = ask("Name tag for this instance", "provisioned-instance")

    if not confirm_summary(
        InstanceType=instance_type,
        AMI=f"{ami_id} ({ami_name})",
        VPC=vpc_id,
        Subnet=subnet_id,
        SecurityGroups=sg_ids,
        PublicIP=assign_public_ip,
        KeyPair=key_name,
        RootVolumeGiB=volume_size,
        Users=[u["name"] for u in users] or "none added",
        NameTag=name_tag,
        Region=session.region_name,
    ):
        print("Aborted, nothing was launched.")
        return

    if args.dry_run:
        print("\n--dry-run set: skipping the actual run_instances call.")
        return

    run_kwargs = dict(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        KeyName=key_name,
        NetworkInterfaces=[{
            "DeviceIndex": 0,
            "SubnetId": subnet_id,
            "Groups": sg_ids,
            "AssociatePublicIpAddress": assign_public_ip,
        }],
        BlockDeviceMappings=[{
            "DeviceName": "/dev/xvda",
            "Ebs": {"VolumeSize": volume_size, "VolumeType": "gp3", "DeleteOnTermination": True},
        }],
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [{"Key": "Name", "Value": name_tag}],
        }],
    )
    if user_data:
        run_kwargs["UserData"] = user_data

    print("\nLaunching instance...")
    resp = ec2.run_instances(**run_kwargs)
    instance_id = resp["Instances"][0]["InstanceId"]
    print(f"Instance {instance_id} launching. Waiting for it to reach 'running'...")

    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    desc = ec2.describe_instances(InstanceIds=[instance_id])
    inst = desc["Reservations"][0]["Instances"][0]
    public_ip = inst.get("PublicIpAddress")
    private_ip = inst.get("PrivateIpAddress")

    if assign_public_ip and ask_yes_no("Allocate a static Elastic IP for this instance?", False):
        eip = ec2.allocate_address(Domain="vpc")
        ec2.associate_address(InstanceId=instance_id, AllocationId=eip["AllocationId"])
        public_ip = eip["PublicIp"]
        print(f"Elastic IP {public_ip} associated (allocation {eip['AllocationId']}).")

    print("\n== Done ==")
    print(f"  Instance ID:  {instance_id}")
    print(f"  Public IP:    {public_ip or 'none'}")
    print(f"  Private IP:   {private_ip}")
    print(f"  Login user:   {os_info['default_user']}")
    if key_name.endswith("-key") or not os_info["windows"]:
        print(f"  SSH example:  ssh -i {key_name}.pem {os_info['default_user']}@{public_ip or private_ip}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
