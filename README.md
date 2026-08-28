# AWS Tools and Scripts

Tools and scripts for working with AWS, covering both the CLI and the web console.

## Contents

- `install_aws_cli.py` — detects the host OS (Windows or Linux, plus version/architecture) and installs AWS CLI v2 using AWS's official installer for that platform.
- `provision_ec2.py` — interactive CLI that sizes an EC2 instance from your requirements (vCPU/RAM/workload type), then provisions it end to end: resolves the latest AMI for the OS you pick, sets up networking/security group/key pair, injects Linux users via cloud-init, and launches the instance.

More tools and scripts are in progress and will be added here over time.

## Requirements

- Python 3.6+
- `boto3` for `provision_ec2.py` (`pip install boto3`)
- AWS credentials configured for `provision_ec2.py` (`aws configure`, a named profile, or an instance/role profile) — the script never asks you to paste access keys
- Admin/root privileges for scripts that install software (the CLI installer writes to system directories)

## Usage

```bash
# Detect OS and AWS CLI status only, no changes made
python3 install_aws_cli.py --check-only

# Install (Linux needs sudo, Windows needs an elevated prompt)
sudo python3 install_aws_cli.py

# Reinstall/update even if already present
sudo python3 install_aws_cli.py --force

# Walk through EC2 sizing/OS/network/user prompts without launching anything
python3 provision_ec2.py --dry-run

# Provision an EC2 instance using a named AWS CLI profile/region
python3 provision_ec2.py --profile myprofile --region us-east-1
```
