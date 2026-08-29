#!/usr/bin/env python3
"""Interactive AWS console: log in, then browse AWS services through nested
menus (category -> service -> action) instead of remembering CLI flags.

Credentials entered at the prompt are held only in memory for this process —
nothing is written to ~/.aws. Prefer temporary/short-lived keys (STS session
token or SSO via a profile) over long-lived IAM user keys.

This is not literally every AWS product (AWS ships 200+ services) — it
covers ~20 of the most commonly used ones, grouped the way the AWS Console
groups them, with the everyday actions for each (list/describe plus a few
guarded create/start/stop/delete operations). The SERVICES registry near
the bottom of the file is the place to add more.

Destructive actions (terminate, delete, purge) always ask you to type the
resource's name/id back before doing anything.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound

RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

def prompt_credentials(default_region: str | None) -> boto3.Session:
    default_region = default_region or "us-east-1"
    print("== AWS credentials (kept in memory only, never written to disk) ==")
    access_key = getpass.getpass("AWS Access Key ID: ").strip()
    secret_key = getpass.getpass("AWS Secret Access Key: ").strip()
    session_token = getpass.getpass(
        "AWS Session Token (blank if not using temporary credentials): "
    ).strip()
    region = input(f"Default AWS region [{default_region}]: ").strip() or default_region

    if not access_key or not secret_key:
        sys.exit("Access key and secret key are both required.")

    return boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token or None,
        region_name=region,
    )


def session_from_profile(profile: str, region: str | None) -> boto3.Session:
    try:
        return boto3.Session(profile_name=profile, region_name=region)
    except ProfileNotFound as e:
        sys.exit(str(e))


def choose_login(args) -> boto3.Session:
    if args.profile:
        return session_from_profile(args.profile, args.region)

    available = boto3.Session().available_profiles
    print("== AWS login ==")
    print("  1) Enter Access Key / Secret Key (and optional session token)")
    print("  2) Use a named profile from ~/.aws/credentials (or SSO)")
    print("  3) Use the default credential chain (env vars / instance role)")
    choice = input("Choose [1]: ").strip() or "1"

    if choice == "2":
        if available:
            print("Available profiles: " + ", ".join(available))
        else:
            print("No profiles found in ~/.aws/credentials, but you can still type one.")
        profile = input("Profile name: ").strip()
        if not profile:
            sys.exit("A profile name is required.")
        return session_from_profile(profile, args.region)

    if choice == "3":
        return boto3.Session(region_name=args.region)

    return prompt_credentials(args.region)


def verify_identity(session: boto3.Session) -> dict:
    sts = client_for(session, "sts", session.region_name or "us-east-1")
    try:
        identity = sts.get_caller_identity()
    except (NoCredentialsError, ClientError, BotoCoreError) as e:
        sys.exit(f"Could not authenticate to AWS: {e}")
    print(
        f"Authenticated as {identity['Arn']} "
        f"(account {identity['Account']})"
    )
    return identity


# --------------------------------------------------------------------------
# Small helpers shared by every action
# --------------------------------------------------------------------------

_CLIENT_CACHE: dict[tuple[str, str], object] = {}


def client_for(session: boto3.Session, service: str, region: str):
    key = (service, region)
    if key not in _CLIENT_CACHE:
        _CLIENT_CACHE[key] = session.client(service, region_name=region, config=RETRY_CONFIG)
    return _CLIENT_CACHE[key]


def print_table(headers: list[str], rows: list[list], limit: int = 50) -> None:
    if not rows:
        print("(no results)")
        return
    shown = rows[:limit]
    widths = [len(h) for h in headers]
    for row in shown:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in shown:
        print(fmt.format(*[str(c) for c in row]))
    if len(rows) > limit:
        print(f"... {len(rows) - limit} more not shown")


def paginate(paginator_iter, key: str, limit: int = 500) -> list:
    items = []
    for page in paginator_iter:
        items.extend(page.get(key, []))
        if len(items) >= limit:
            break
    return items[:limit]


def confirm(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{prompt} ({hint}): ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def confirm_destructive(action_desc: str, identifier: str) -> bool:
    print(f"This will {action_desc}.")
    typed = input(f"Type '{identifier}' to confirm, anything else cancels: ").strip()
    return typed == identifier


def pause() -> None:
    input("\nPress Enter to continue... ")


def run_action(func, *args) -> None:
    try:
        func(*args)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "?")
        msg = e.response.get("Error", {}).get("Message", str(e))
        eprint(f"AWS error [{code}]: {msg}")
    except (BotoCoreError, NoCredentialsError) as e:
        eprint(f"AWS error: {e}")
    except KeyboardInterrupt:
        print()
        return
    except Exception as e:  # last resort so a typo in one action doesn't kill the menu
        eprint(f"Unexpected error: {e}")
    pause()


# --------------------------------------------------------------------------
# EC2
# --------------------------------------------------------------------------

def ec2_list_instances(session, region):
    ec2 = client_for(session, "ec2", region)
    reservations = paginate(ec2.get_paginator("describe_instances").paginate(), "Reservations")
    rows = []
    for r in reservations:
        for i in r["Instances"]:
            name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "")
            rows.append([
                i["InstanceId"], name, i["InstanceType"], i["State"]["Name"],
                i.get("PublicIpAddress", "-"), i.get("PrivateIpAddress", "-"),
            ])
    print_table(["InstanceId", "Name", "Type", "State", "PublicIP", "PrivateIP"], rows)


def _ec2_instance_action(session, region, verb, method):
    ec2 = client_for(session, "ec2", region)
    instance_id = input("Instance ID: ").strip()
    if not instance_id:
        return
    if verb == "terminate" and not confirm_destructive("permanently terminate this instance", instance_id):
        print("Cancelled.")
        return
    getattr(ec2, method)(InstanceIds=[instance_id])
    print(f"Requested {verb} for {instance_id}.")


def ec2_start_instance(session, region):
    _ec2_instance_action(session, region, "start", "start_instances")


def ec2_stop_instance(session, region):
    _ec2_instance_action(session, region, "stop", "stop_instances")


def ec2_reboot_instance(session, region):
    _ec2_instance_action(session, region, "reboot", "reboot_instances")


def ec2_terminate_instance(session, region):
    _ec2_instance_action(session, region, "terminate", "terminate_instances")


def ec2_list_security_groups(session, region):
    ec2 = client_for(session, "ec2", region)
    groups = paginate(ec2.get_paginator("describe_security_groups").paginate(), "SecurityGroups")
    rows = [[g["GroupId"], g["GroupName"], g.get("VpcId", "-"), g.get("Description", "")] for g in groups]
    print_table(["GroupId", "Name", "VpcId", "Description"], rows)


def ec2_list_key_pairs(session, region):
    ec2 = client_for(session, "ec2", region)
    pairs = ec2.describe_key_pairs()["KeyPairs"]
    rows = [[k["KeyName"], k.get("KeyFingerprint", "-"), k.get("KeyType", "-")] for k in pairs]
    print_table(["KeyName", "Fingerprint", "Type"], rows)


def ec2_list_amis_owned_by_self(session, region):
    ec2 = client_for(session, "ec2", region)
    images = ec2.describe_images(Owners=["self"])["Images"]
    rows = [[i["ImageId"], i.get("Name", "-"), i["State"], i.get("CreationDate", "-")] for i in images]
    print_table(["ImageId", "Name", "State", "Created"], rows)


# --------------------------------------------------------------------------
# VPC / Networking
# --------------------------------------------------------------------------

def vpc_list_vpcs(session, region):
    ec2 = client_for(session, "ec2", region)
    vpcs = ec2.describe_vpcs()["Vpcs"]
    rows = [[v["VpcId"], v["CidrBlock"], v["State"], "yes" if v.get("IsDefault") else "no"] for v in vpcs]
    print_table(["VpcId", "CIDR", "State", "Default"], rows)


def vpc_list_subnets(session, region):
    ec2 = client_for(session, "ec2", region)
    subnets = paginate(ec2.get_paginator("describe_subnets").paginate(), "Subnets")
    rows = [[s["SubnetId"], s["VpcId"], s["CidrBlock"], s["AvailabilityZone"], s["AvailableIpAddressCount"]] for s in subnets]
    print_table(["SubnetId", "VpcId", "CIDR", "AZ", "FreeIPs"], rows)


def vpc_list_route_tables(session, region):
    ec2 = client_for(session, "ec2", region)
    tables = paginate(ec2.get_paginator("describe_route_tables").paginate(), "RouteTables")
    rows = [[t["RouteTableId"], t["VpcId"], len(t["Routes"]), len(t["Associations"])] for t in tables]
    print_table(["RouteTableId", "VpcId", "#Routes", "#Associations"], rows)


def vpc_list_internet_gateways(session, region):
    ec2 = client_for(session, "ec2", region)
    igws = ec2.describe_internet_gateways()["InternetGateways"]
    rows = []
    for g in igws:
        vpcs = ",".join(a["VpcId"] for a in g.get("Attachments", [])) or "-"
        rows.append([g["InternetGatewayId"], vpcs])
    print_table(["InternetGatewayId", "AttachedVpcs"], rows)


def vpc_list_nat_gateways(session, region):
    ec2 = client_for(session, "ec2", region)
    nats = paginate(ec2.get_paginator("describe_nat_gateways").paginate(), "NatGateways")
    rows = [[n["NatGatewayId"], n["VpcId"], n["SubnetId"], n["State"]] for n in nats]
    print_table(["NatGatewayId", "VpcId", "SubnetId", "State"], rows)


# --------------------------------------------------------------------------
# S3
# --------------------------------------------------------------------------

def s3_list_buckets(session, region):
    s3 = client_for(session, "s3", region)
    buckets = s3.list_buckets()["Buckets"]
    rows = [[b["Name"], b["CreationDate"]] for b in buckets]
    print_table(["Bucket", "Created"], rows)


def s3_list_objects(session, region):
    s3 = client_for(session, "s3", region)
    bucket = input("Bucket name: ").strip()
    prefix = input("Prefix (optional): ").strip()
    if not bucket:
        return
    objs = paginate(
        s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix), "Contents"
    )
    rows = [[o["Key"], o["Size"], o["LastModified"]] for o in objs]
    print_table(["Key", "SizeBytes", "LastModified"], rows)


def s3_create_bucket(session, region):
    s3 = client_for(session, "s3", region)
    bucket = input("New bucket name: ").strip()
    if not bucket:
        return
    kwargs = {"Bucket": bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kwargs)
    print(f"Created bucket {bucket} in {region}.")


def s3_delete_bucket(session, region):
    s3 = client_for(session, "s3", region)
    bucket = input("Bucket to delete (must be empty): ").strip()
    if not bucket:
        return
    if not confirm_destructive("permanently delete this empty bucket", bucket):
        print("Cancelled.")
        return
    s3.delete_bucket(Bucket=bucket)
    print(f"Deleted bucket {bucket}.")


def s3_upload_file(session, region):
    s3 = client_for(session, "s3", region)
    local_path = input("Local file path: ").strip()
    bucket = input("Destination bucket: ").strip()
    key = input("Destination key (path in bucket): ").strip()
    if not (local_path and bucket and key):
        return
    s3.upload_file(local_path, bucket, key)
    print(f"Uploaded {local_path} -> s3://{bucket}/{key}")


def s3_download_object(session, region):
    s3 = client_for(session, "s3", region)
    bucket = input("Bucket: ").strip()
    key = input("Object key: ").strip()
    local_path = input("Save to local path: ").strip()
    if not (bucket and key and local_path):
        return
    s3.download_file(bucket, key, local_path)
    print(f"Downloaded s3://{bucket}/{key} -> {local_path}")


def s3_presigned_url(session, region):
    s3 = client_for(session, "s3", region)
    bucket = input("Bucket: ").strip()
    key = input("Object key: ").strip()
    expires = input("Expiry in seconds [3600]: ").strip() or "3600"
    if not (bucket and key):
        return
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=int(expires)
    )
    print(url)


# --------------------------------------------------------------------------
# Lambda
# --------------------------------------------------------------------------

def lambda_list_functions(session, region):
    lam = client_for(session, "lambda", region)
    fns = paginate(lam.get_paginator("list_functions").paginate(), "Functions")
    rows = [[f["FunctionName"], f["Runtime"], f["MemorySize"], f["Timeout"], f["LastModified"]] for f in fns]
    print_table(["FunctionName", "Runtime", "MemoryMB", "TimeoutS", "LastModified"], rows)


def lambda_get_function(session, region):
    lam = client_for(session, "lambda", region)
    name = input("Function name: ").strip()
    if not name:
        return
    cfg = lam.get_function_configuration(FunctionName=name)
    for k in ("FunctionName", "Runtime", "Handler", "MemorySize", "Timeout", "Role", "LastModified", "State"):
        print(f"{k}: {cfg.get(k)}")


def lambda_invoke(session, region):
    lam = client_for(session, "lambda", region)
    name = input("Function name: ").strip()
    payload_raw = input("JSON payload (blank for {}): ").strip() or "{}"
    if not name:
        return
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as e:
        eprint(f"Invalid JSON: {e}")
        return
    resp = lam.invoke(FunctionName=name, Payload=json.dumps(payload).encode())
    body = resp["Payload"].read().decode(errors="replace")
    print(f"StatusCode: {resp['StatusCode']}")
    print(body)


def lambda_delete_function(session, region):
    lam = client_for(session, "lambda", region)
    name = input("Function name to delete: ").strip()
    if not name:
        return
    if not confirm_destructive("permanently delete this function", name):
        print("Cancelled.")
        return
    lam.delete_function(FunctionName=name)
    print(f"Deleted function {name}.")


# --------------------------------------------------------------------------
# IAM (global service, region is ignored by the client but we keep the arg)
# --------------------------------------------------------------------------

def iam_list_users(session, region):
    iam = client_for(session, "iam", "us-east-1")
    users = paginate(iam.get_paginator("list_users").paginate(), "Users")
    rows = [[u["UserName"], u["Arn"], u["CreateDate"]] for u in users]
    print_table(["UserName", "Arn", "Created"], rows)


def iam_list_roles(session, region):
    iam = client_for(session, "iam", "us-east-1")
    roles = paginate(iam.get_paginator("list_roles").paginate(), "Roles")
    rows = [[r["RoleName"], r["Arn"], r["CreateDate"]] for r in roles]
    print_table(["RoleName", "Arn", "Created"], rows)


def iam_list_groups(session, region):
    iam = client_for(session, "iam", "us-east-1")
    groups = paginate(iam.get_paginator("list_groups").paginate(), "Groups")
    rows = [[g["GroupName"], g["Arn"], g["CreateDate"]] for g in groups]
    print_table(["GroupName", "Arn", "Created"], rows)


def iam_list_attached_user_policies(session, region):
    iam = client_for(session, "iam", "us-east-1")
    user = input("User name: ").strip()
    if not user:
        return
    policies = iam.list_attached_user_policies(UserName=user)["AttachedPolicies"]
    rows = [[p["PolicyName"], p["PolicyArn"]] for p in policies]
    print_table(["PolicyName", "PolicyArn"], rows)


def iam_create_user(session, region):
    iam = client_for(session, "iam", "us-east-1")
    user = input("New user name: ").strip()
    if not user:
        return
    if not confirm(f"Create IAM user '{user}'?"):
        print("Cancelled.")
        return
    resp = iam.create_user(UserName=user)
    print(f"Created {resp['User']['Arn']}")


def iam_delete_user(session, region):
    iam = client_for(session, "iam", "us-east-1")
    user = input("User name to delete: ").strip()
    if not user:
        return
    if not confirm_destructive("permanently delete this IAM user", user):
        print("Cancelled.")
        return
    iam.delete_user(UserName=user)
    print(f"Deleted user {user}.")


# --------------------------------------------------------------------------
# RDS
# --------------------------------------------------------------------------

def rds_list_instances(session, region):
    rds = client_for(session, "rds", region)
    dbs = paginate(rds.get_paginator("describe_db_instances").paginate(), "DBInstances")
    rows = [[d["DBInstanceIdentifier"], d["Engine"], d["DBInstanceClass"], d["DBInstanceStatus"]] for d in dbs]
    print_table(["Identifier", "Engine", "Class", "Status"], rows)


def _rds_instance_action(session, region, verb, method):
    rds = client_for(session, "rds", region)
    identifier = input("DB instance identifier: ").strip()
    if not identifier:
        return
    getattr(rds, method)(DBInstanceIdentifier=identifier)
    print(f"Requested {verb} for {identifier}.")


def rds_start_instance(session, region):
    _rds_instance_action(session, region, "start", "start_db_instance")


def rds_stop_instance(session, region):
    _rds_instance_action(session, region, "stop", "stop_db_instance")


def rds_reboot_instance(session, region):
    _rds_instance_action(session, region, "reboot", "reboot_db_instance")


def rds_list_snapshots(session, region):
    rds = client_for(session, "rds", region)
    snaps = paginate(rds.get_paginator("describe_db_snapshots").paginate(), "DBSnapshots")
    rows = [[s["DBSnapshotIdentifier"], s["DBInstanceIdentifier"], s["Status"], s.get("SnapshotCreateTime", "-")] for s in snaps]
    print_table(["SnapshotId", "DBInstance", "Status", "Created"], rows)


# --------------------------------------------------------------------------
# DynamoDB
# --------------------------------------------------------------------------

def ddb_list_tables(session, region):
    ddb = client_for(session, "dynamodb", region)
    tables = paginate(ddb.get_paginator("list_tables").paginate(), "TableNames")
    print_table(["TableName"], [[t] for t in tables])


def ddb_describe_table(session, region):
    ddb = client_for(session, "dynamodb", region)
    name = input("Table name: ").strip()
    if not name:
        return
    t = ddb.describe_table(TableName=name)["Table"]
    print(f"Status: {t['TableStatus']}  Items: {t.get('ItemCount', '?')}  SizeBytes: {t.get('TableSizeBytes', '?')}")
    keys = ", ".join(f"{k['AttributeName']}({k['KeyType']})" for k in t["KeySchema"])
    print(f"Keys: {keys}")


def ddb_scan_table(session, region):
    ddb = client_for(session, "dynamodb", region)
    name = input("Table name: ").strip()
    if not name:
        return
    resp = ddb.scan(TableName=name, Limit=10)
    for item in resp.get("Items", []):
        print(json.dumps(item, default=str))
    if not resp.get("Items"):
        print("(no items)")


def ddb_delete_table(session, region):
    ddb = client_for(session, "dynamodb", region)
    name = input("Table name to delete: ").strip()
    if not name:
        return
    if not confirm_destructive("permanently delete this table and all its data", name):
        print("Cancelled.")
        return
    ddb.delete_table(TableName=name)
    print(f"Deleting table {name}.")


# --------------------------------------------------------------------------
# ElastiCache
# --------------------------------------------------------------------------

def elasticache_list_clusters(session, region):
    ec = client_for(session, "elasticache", region)
    clusters = paginate(ec.get_paginator("describe_cache_clusters").paginate(), "CacheClusters")
    rows = [[c["CacheClusterId"], c["Engine"], c["CacheNodeType"], c["CacheClusterStatus"]] for c in clusters]
    print_table(["ClusterId", "Engine", "NodeType", "Status"], rows)


# --------------------------------------------------------------------------
# CloudWatch
# --------------------------------------------------------------------------

def cw_list_alarms(session, region):
    cw = client_for(session, "cloudwatch", region)
    alarms = paginate(cw.get_paginator("describe_alarms").paginate(), "MetricAlarms")
    rows = [[a["AlarmName"], a["StateValue"], a["MetricName"], a.get("Namespace", "-")] for a in alarms]
    print_table(["AlarmName", "State", "Metric", "Namespace"], rows)


def cw_list_log_groups(session, region):
    logs = client_for(session, "logs", region)
    groups = paginate(logs.get_paginator("describe_log_groups").paginate(), "logGroups")
    rows = [[g["logGroupName"], g.get("storedBytes", 0)] for g in groups]
    print_table(["LogGroup", "StoredBytes"], rows)


def cw_tail_log_group(session, region):
    logs = client_for(session, "logs", region)
    group = input("Log group name: ").strip()
    minutes = input("Look back how many minutes? [15]: ").strip() or "15"
    if not group:
        return
    start_ms = int((datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(minutes=int(minutes))).timestamp() * 1000)
    events = []
    for page in logs.get_paginator("filter_log_events").paginate(logGroupName=group, startTime=start_ms):
        events.extend(page.get("events", []))
        if len(events) >= 200:
            break
    events.sort(key=lambda e: e["timestamp"])
    for e in events[-200:]:
        ts = datetime.datetime.fromtimestamp(e["timestamp"] / 1000, datetime.timezone.utc)
        print(f"[{ts.isoformat()}] {e['message'].rstrip()}")
    if not events:
        print("(no events in that window)")


# --------------------------------------------------------------------------
# SNS
# --------------------------------------------------------------------------

def sns_list_topics(session, region):
    sns = client_for(session, "sns", region)
    topics = paginate(sns.get_paginator("list_topics").paginate(), "Topics")
    print_table(["TopicArn"], [[t["TopicArn"]] for t in topics])


def sns_create_topic(session, region):
    sns = client_for(session, "sns", region)
    name = input("Topic name: ").strip()
    if not name:
        return
    resp = sns.create_topic(Name=name)
    print(f"Created {resp['TopicArn']}")


def sns_subscribe_email(session, region):
    sns = client_for(session, "sns", region)
    arn = input("Topic ARN: ").strip()
    email = input("Email address: ").strip()
    if not (arn and email):
        return
    sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=email)
    print("Subscription requested — check the inbox to confirm it.")


def sns_publish(session, region):
    sns = client_for(session, "sns", region)
    arn = input("Topic ARN: ").strip()
    message = input("Message: ").strip()
    if not (arn and message):
        return
    resp = sns.publish(TopicArn=arn, Message=message)
    print(f"Published, MessageId={resp['MessageId']}")


def sns_delete_topic(session, region):
    sns = client_for(session, "sns", region)
    arn = input("Topic ARN to delete: ").strip()
    if not arn:
        return
    if not confirm_destructive("permanently delete this topic", arn):
        print("Cancelled.")
        return
    sns.delete_topic(TopicArn=arn)
    print("Deleted.")


# --------------------------------------------------------------------------
# SQS
# --------------------------------------------------------------------------

def sqs_list_queues(session, region):
    sqs = client_for(session, "sqs", region)
    urls = sqs.list_queues().get("QueueUrls", [])
    print_table(["QueueUrl"], [[u] for u in urls])


def sqs_create_queue(session, region):
    sqs = client_for(session, "sqs", region)
    name = input("Queue name: ").strip()
    if not name:
        return
    resp = sqs.create_queue(QueueName=name)
    print(f"Created {resp['QueueUrl']}")


def sqs_send_message(session, region):
    sqs = client_for(session, "sqs", region)
    url = input("Queue URL: ").strip()
    body = input("Message body: ").strip()
    if not (url and body):
        return
    sqs.send_message(QueueUrl=url, MessageBody=body)
    print("Sent.")


def sqs_receive_messages(session, region):
    sqs = client_for(session, "sqs", region)
    url = input("Queue URL: ").strip()
    if not url:
        return
    resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
    for m in resp.get("Messages", []):
        print(f"[{m['MessageId']}] {m['Body']}")
    if not resp.get("Messages"):
        print("(no messages available)")


def sqs_purge_queue(session, region):
    sqs = client_for(session, "sqs", region)
    url = input("Queue URL to purge: ").strip()
    if not url:
        return
    if not confirm_destructive("delete all messages currently in this queue", url):
        print("Cancelled.")
        return
    sqs.purge_queue(QueueUrl=url)
    print("Purge requested.")


def sqs_delete_queue(session, region):
    sqs = client_for(session, "sqs", region)
    url = input("Queue URL to delete: ").strip()
    if not url:
        return
    if not confirm_destructive("permanently delete this queue", url):
        print("Cancelled.")
        return
    sqs.delete_queue(QueueUrl=url)
    print("Deleted.")


# --------------------------------------------------------------------------
# Route 53 (global service)
# --------------------------------------------------------------------------

def r53_list_hosted_zones(session, region):
    r53 = client_for(session, "route53", "us-east-1")
    zones = paginate(r53.get_paginator("list_hosted_zones").paginate(), "HostedZones")
    rows = [[z["Id"].split("/")[-1], z["Name"], z["ResourceRecordSetCount"]] for z in zones]
    print_table(["ZoneId", "Name", "#Records"], rows)


def r53_list_records(session, region):
    r53 = client_for(session, "route53", "us-east-1")
    zone_id = input("Hosted zone ID: ").strip()
    if not zone_id:
        return
    records = paginate(
        r53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone_id),
        "ResourceRecordSets",
    )
    rows = []
    for r in records:
        values = ",".join(v["Value"] for v in r.get("ResourceRecords", [])) or (r.get("AliasTarget", {}).get("DNSName", "-"))
        rows.append([r["Name"], r["Type"], values])
    print_table(["Name", "Type", "Value"], rows)


# --------------------------------------------------------------------------
# CloudFormation
# --------------------------------------------------------------------------

def cfn_list_stacks(session, region):
    cfn = client_for(session, "cloudformation", region)
    stacks = paginate(cfn.get_paginator("list_stacks").paginate(
        StackStatusFilter=[
            "CREATE_COMPLETE", "UPDATE_COMPLETE", "ROLLBACK_COMPLETE",
            "CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", "REVIEW_IN_PROGRESS",
        ]
    ), "StackSummaries")
    rows = [[s["StackName"], s["StackStatus"], s.get("CreationTime", "-")] for s in stacks]
    print_table(["StackName", "Status", "Created"], rows)


def cfn_describe_stack(session, region):
    cfn = client_for(session, "cloudformation", region)
    name = input("Stack name: ").strip()
    if not name:
        return
    stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    print(f"Status: {stack['StackStatus']}")
    for o in stack.get("Outputs", []):
        print(f"  Output: {o['OutputKey']} = {o['OutputValue']}")


def cfn_delete_stack(session, region):
    cfn = client_for(session, "cloudformation", region)
    name = input("Stack name to delete: ").strip()
    if not name:
        return
    if not confirm_destructive("permanently delete this stack and its resources", name):
        print("Cancelled.")
        return
    cfn.delete_stack(StackName=name)
    print(f"Deletion requested for {name}.")


# --------------------------------------------------------------------------
# ECS
# --------------------------------------------------------------------------

def ecs_list_clusters(session, region):
    ecs = client_for(session, "ecs", region)
    arns = paginate(ecs.get_paginator("list_clusters").paginate(), "clusterArns")
    print_table(["ClusterArn"], [[a] for a in arns])


def ecs_list_services(session, region):
    ecs = client_for(session, "ecs", region)
    cluster = input("Cluster name or ARN: ").strip()
    if not cluster:
        return
    arns = paginate(ecs.get_paginator("list_services").paginate(cluster=cluster), "serviceArns")
    print_table(["ServiceArn"], [[a] for a in arns])


def ecs_list_tasks(session, region):
    ecs = client_for(session, "ecs", region)
    cluster = input("Cluster name or ARN: ").strip()
    if not cluster:
        return
    arns = paginate(ecs.get_paginator("list_tasks").paginate(cluster=cluster), "taskArns")
    print_table(["TaskArn"], [[a] for a in arns])


# --------------------------------------------------------------------------
# EKS
# --------------------------------------------------------------------------

def eks_list_clusters(session, region):
    eks = client_for(session, "eks", region)
    names = paginate(eks.get_paginator("list_clusters").paginate(), "clusters")
    print_table(["ClusterName"], [[n] for n in names])


def eks_describe_cluster(session, region):
    eks = client_for(session, "eks", region)
    name = input("Cluster name: ").strip()
    if not name:
        return
    c = eks.describe_cluster(name=name)["cluster"]
    print(f"Status: {c['status']}  Version: {c['version']}  Endpoint: {c.get('endpoint', '-')}")


# --------------------------------------------------------------------------
# Secrets Manager
# --------------------------------------------------------------------------

def secrets_list(session, region):
    sm = client_for(session, "secretsmanager", region)
    secrets = paginate(sm.get_paginator("list_secrets").paginate(), "SecretList")
    rows = [[s["Name"], s.get("LastChangedDate", "-")] for s in secrets]
    print_table(["SecretName", "LastChanged"], rows)


def secrets_get_value(session, region):
    sm = client_for(session, "secretsmanager", region)
    name = input("Secret name or ARN: ").strip()
    if not name:
        return
    if not confirm(f"This will print the plaintext value of '{name}' to your terminal. Continue?"):
        print("Cancelled.")
        return
    resp = sm.get_secret_value(SecretId=name)
    print(resp.get("SecretString", "<binary secret>"))


# --------------------------------------------------------------------------
# Systems Manager (SSM)
# --------------------------------------------------------------------------

def ssm_list_managed_instances(session, region):
    ssm = client_for(session, "ssm", region)
    infos = paginate(ssm.get_paginator("describe_instance_information").paginate(), "InstanceInformationList")
    rows = [[i["InstanceId"], i.get("PingStatus", "-"), i.get("PlatformType", "-"), i.get("AgentVersion", "-")] for i in infos]
    print_table(["InstanceId", "PingStatus", "Platform", "AgentVersion"], rows)


def ssm_list_parameters(session, region):
    ssm = client_for(session, "ssm", region)
    params = paginate(ssm.get_paginator("describe_parameters").paginate(), "Parameters")
    rows = [[p["Name"], p["Type"], p.get("LastModifiedDate", "-")] for p in params]
    print_table(["Name", "Type", "LastModified"], rows)


# --------------------------------------------------------------------------
# CloudTrail
# --------------------------------------------------------------------------

def cloudtrail_list_trails(session, region):
    ct = client_for(session, "cloudtrail", region)
    trails = ct.describe_trails()["trailList"]
    rows = [[t["Name"], t.get("HomeRegion", "-"), t.get("IsMultiRegionTrail", False)] for t in trails]
    print_table(["Name", "HomeRegion", "MultiRegion"], rows)


def cloudtrail_lookup_recent_events(session, region):
    ct = client_for(session, "cloudtrail", region)
    hours = input("Look back how many hours? [1]: ").strip() or "1"
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=int(hours))
    events = paginate(
        ct.get_paginator("lookup_events").paginate(StartTime=start), "Events", limit=100
    )
    rows = [[e["EventTime"], e["EventName"], e.get("Username", "-")] for e in events]
    print_table(["Time", "EventName", "User"], rows)


# --------------------------------------------------------------------------
# KMS
# --------------------------------------------------------------------------

def kms_list_keys(session, region):
    kms = client_for(session, "kms", region)
    keys = paginate(kms.get_paginator("list_keys").paginate(), "Keys")
    print_table(["KeyId"], [[k["KeyId"]] for k in keys])


# --------------------------------------------------------------------------
# ECR
# --------------------------------------------------------------------------

def ecr_list_repositories(session, region):
    ecr = client_for(session, "ecr", region)
    repos = paginate(ecr.get_paginator("describe_repositories").paginate(), "repositories")
    rows = [[r["repositoryName"], r["repositoryUri"]] for r in repos]
    print_table(["Repository", "URI"], rows)


def ecr_list_images(session, region):
    ecr = client_for(session, "ecr", region)
    repo = input("Repository name: ").strip()
    if not repo:
        return
    images = paginate(ecr.get_paginator("describe_images").paginate(repositoryName=repo), "imageDetails")
    rows = [[",".join(i.get("imageTags", ["<untagged>"])), i.get("imageSizeInBytes", 0), i.get("imagePushedAt", "-")] for i in images]
    print_table(["Tags", "SizeBytes", "Pushed"], rows)


# --------------------------------------------------------------------------
# Cost Explorer (billing, global service, must be called in us-east-1)
# --------------------------------------------------------------------------

def cost_this_month_by_service(session, region):
    ce = client_for(session, "ce", "us-east-1")
    today = datetime.date.today()
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    rows = []
    for group in resp["ResultsByTime"][0]["Groups"]:
        amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
        if amount > 0:
            rows.append([group["Keys"][0], f"${amount:.2f}"])
    rows.sort(key=lambda r: float(r[1][1:]), reverse=True)
    print_table(["Service", "CostSoFarThisMonth"], rows)


# --------------------------------------------------------------------------
# STS / account
# --------------------------------------------------------------------------

def sts_who_am_i(session, region):
    identity = client_for(session, "sts", "us-east-1").get_caller_identity()
    print(f"Account: {identity['Account']}")
    print(f"Arn:     {identity['Arn']}")
    print(f"UserId:  {identity['UserId']}")


# --------------------------------------------------------------------------
# Menu registry: category -> [(service_key, label, [(action_label, func), ...])]
# --------------------------------------------------------------------------

SERVICES: dict[str, list[tuple[str, list[tuple[str, object]]]]] = {
    "Compute": [
        ("EC2", [
            ("List instances", ec2_list_instances),
            ("Start an instance", ec2_start_instance),
            ("Stop an instance", ec2_stop_instance),
            ("Reboot an instance", ec2_reboot_instance),
            ("Terminate an instance", ec2_terminate_instance),
            ("List security groups", ec2_list_security_groups),
            ("List key pairs", ec2_list_key_pairs),
            ("List AMIs owned by me", ec2_list_amis_owned_by_self),
        ]),
        ("Lambda", [
            ("List functions", lambda_list_functions),
            ("Get function configuration", lambda_get_function),
            ("Invoke a function", lambda_invoke),
            ("Delete a function", lambda_delete_function),
        ]),
        ("ECS", [
            ("List clusters", ecs_list_clusters),
            ("List services in a cluster", ecs_list_services),
            ("List tasks in a cluster", ecs_list_tasks),
        ]),
        ("EKS", [
            ("List clusters", eks_list_clusters),
            ("Describe a cluster", eks_describe_cluster),
        ]),
    ],
    "Storage": [
        ("S3", [
            ("List buckets", s3_list_buckets),
            ("List objects in a bucket", s3_list_objects),
            ("Create a bucket", s3_create_bucket),
            ("Delete an empty bucket", s3_delete_bucket),
            ("Upload a file", s3_upload_file),
            ("Download an object", s3_download_object),
            ("Generate a presigned URL", s3_presigned_url),
        ]),
    ],
    "Database": [
        ("RDS", [
            ("List DB instances", rds_list_instances),
            ("Start a DB instance", rds_start_instance),
            ("Stop a DB instance", rds_stop_instance),
            ("Reboot a DB instance", rds_reboot_instance),
            ("List DB snapshots", rds_list_snapshots),
        ]),
        ("DynamoDB", [
            ("List tables", ddb_list_tables),
            ("Describe a table", ddb_describe_table),
            ("Scan a table (first 10 items)", ddb_scan_table),
            ("Delete a table", ddb_delete_table),
        ]),
        ("ElastiCache", [
            ("List cache clusters", elasticache_list_clusters),
        ]),
    ],
    "Networking & Content Delivery": [
        ("VPC", [
            ("List VPCs", vpc_list_vpcs),
            ("List subnets", vpc_list_subnets),
            ("List route tables", vpc_list_route_tables),
            ("List internet gateways", vpc_list_internet_gateways),
            ("List NAT gateways", vpc_list_nat_gateways),
        ]),
        ("Route 53", [
            ("List hosted zones", r53_list_hosted_zones),
            ("List records in a zone", r53_list_records),
        ]),
    ],
    "Security, Identity & Compliance": [
        ("IAM", [
            ("List users", iam_list_users),
            ("List roles", iam_list_roles),
            ("List groups", iam_list_groups),
            ("List a user's attached policies", iam_list_attached_user_policies),
            ("Create a user", iam_create_user),
            ("Delete a user", iam_delete_user),
        ]),
        ("Secrets Manager", [
            ("List secrets", secrets_list),
            ("Get a secret's value", secrets_get_value),
        ]),
        ("KMS", [
            ("List keys", kms_list_keys),
        ]),
    ],
    "Application Integration": [
        ("SNS", [
            ("List topics", sns_list_topics),
            ("Create a topic", sns_create_topic),
            ("Subscribe an email to a topic", sns_subscribe_email),
            ("Publish a message", sns_publish),
            ("Delete a topic", sns_delete_topic),
        ]),
        ("SQS", [
            ("List queues", sqs_list_queues),
            ("Create a queue", sqs_create_queue),
            ("Send a message", sqs_send_message),
            ("Receive messages", sqs_receive_messages),
            ("Purge a queue", sqs_purge_queue),
            ("Delete a queue", sqs_delete_queue),
        ]),
    ],
    "Containers & Developer Tools": [
        ("ECR", [
            ("List repositories", ecr_list_repositories),
            ("List images in a repository", ecr_list_images),
        ]),
    ],
    "Management & Governance": [
        ("CloudWatch", [
            ("List alarms", cw_list_alarms),
            ("List log groups", cw_list_log_groups),
            ("Tail recent log events", cw_tail_log_group),
        ]),
        ("CloudFormation", [
            ("List stacks", cfn_list_stacks),
            ("Describe a stack", cfn_describe_stack),
            ("Delete a stack", cfn_delete_stack),
        ]),
        ("CloudTrail", [
            ("List trails", cloudtrail_list_trails),
            ("Look up recent events", cloudtrail_lookup_recent_events),
        ]),
        ("Systems Manager", [
            ("List managed instances", ssm_list_managed_instances),
            ("List parameters", ssm_list_parameters),
        ]),
    ],
    "Billing & Cost Management": [
        ("Cost Explorer", [
            ("This month's cost by service", cost_this_month_by_service),
        ]),
    ],
    "Account": [
        ("STS", [
            ("Who am I", sts_who_am_i),
        ]),
    ],
}


# --------------------------------------------------------------------------
# Menu loop
# --------------------------------------------------------------------------

def choose_from(title: str, options: list[str], back_label: str = "Back") -> int | None:
    print(f"\n== {title} ==")
    for i, opt in enumerate(options, 1):
        print(f"  {i}) {opt}")
    print(f"  0) {back_label}")
    choice = input("Choose: ").strip()
    if choice == "0" or choice == "":
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(options):
        return int(choice) - 1
    print("Invalid choice.")
    return choose_from(title, options, back_label)


def service_menu(session, region: str, service_label: str, actions: list[tuple[str, object]]) -> None:
    while True:
        idx = choose_from(f"{service_label}  (region: {region})", [a[0] for a in actions], "Back to services")
        if idx is None:
            return
        label, func = actions[idx]
        print(f"\n-- {label} --")
        run_action(func, session, region)


def category_menu(session, region: str, category: str, services: list[tuple[str, list]]) -> None:
    while True:
        idx = choose_from(category, [s[0] for s in services], "Back to categories")
        if idx is None:
            return
        service_label, actions = services[idx]
        service_menu(session, region, service_label, actions)


def choose_region(session, current_region: str) -> str:
    ec2 = client_for(session, "ec2", current_region)
    try:
        regions = sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])
        print("\nAvailable regions: " + ", ".join(regions))
    except ClientError:
        pass
    new_region = input(f"New region [{current_region}]: ").strip()
    return new_region or current_region


def account_menu(session, region: str, category: str, services: list[tuple[str, list]]) -> str:
    while True:
        names = [s[0] for s in services] + ["Change region"]
        idx = choose_from(category, names, "Back to categories")
        if idx is None:
            return region
        if idx == len(names) - 1:
            region = choose_region(session, region)
            continue
        service_label, actions = services[idx]
        service_menu(session, region, service_label, actions)


def main_menu(session, region: str) -> None:
    categories = list(SERVICES.keys())
    while True:
        print(f"\n################ AWS Console Menu (region: {region}) ################")
        idx = choose_from("Categories", categories, "Quit")
        if idx is None:
            print("Goodbye.")
            return
        category = categories[idx]
        if category == "Account":
            region = account_menu(session, region, category, SERVICES[category])
            continue
        category_menu(session, region, category, SERVICES[category])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Use an existing AWS shared-credentials profile instead of prompting")
    parser.add_argument("--region", help="Starting AWS region (default: profile's region, or us-east-1)")
    args = parser.parse_args()

    try:
        session = choose_login(args)
        verify_identity(session)
        region = session.region_name or args.region or "us-east-1"
        main_menu(session, region)
    except KeyboardInterrupt:
        print("\nGoodbye.")
        sys.exit(0)


if __name__ == "__main__":
    main()
