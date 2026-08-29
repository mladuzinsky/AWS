# AWS Tools and Scripts

Tools and scripts for working with AWS, covering both the CLI and the web console.

## Contents

- `install_aws_cli.py` — detects the host OS (Windows or Linux, plus version/architecture) and installs AWS CLI v2 using AWS's official installer for that platform.
- `provision_ec2.py` — interactive CLI that sizes an EC2 instance from your requirements (vCPU/RAM/workload type), then provisions it end to end: resolves the latest AMI for the OS you pick, sets up networking/security group/key pair, injects Linux users via cloud-init, and launches the instance.
- `pull_aws_logs.py` — interactively prompts for AWS credentials (or use `--profile`), then pulls CloudWatch Logs from every log group and CloudTrail account-activity events for a time window (default: last 7 days), across one or more regions, and saves them as local JSON-lines files (output dir mode 0700, files mode 0600).
- `aws_menu.py` — interactive AWS console: prompts for login (access keys, a named profile, or the default credential chain), then browses ~20 of the most commonly used AWS services through nested menus (category → service → action) instead of remembering CLI flags — EC2, S3, Lambda, IAM, RDS, DynamoDB, VPC, Route 53, SNS, SQS, CloudWatch, CloudFormation, ECS, EKS, ElastiCache, Secrets Manager, KMS, ECR, Systems Manager, CloudTrail, Cost Explorer, and STS. Covers everyday list/describe actions plus a few guarded create/start/stop/delete operations; destructive actions require typing the resource's name/id back to confirm.

More tools and scripts are in progress and will be added here over time.

## Requirements

- Python 3.6+
- `boto3` for `provision_ec2.py`, `pull_aws_logs.py`, and `aws_menu.py` (`pip install boto3`)
- AWS credentials configured for `provision_ec2.py` (`aws configure`, a named profile, or an instance/role profile) — the script never asks you to paste access keys
- `pull_aws_logs.py` prompts interactively for an Access Key ID / Secret Access Key (and optional session token) each run unless `--profile` is passed; credentials are never written to disk, but exported log files can contain secrets from log lines/API parameters, so treat the output directory as sensitive
- `aws_menu.py` prompts for login the same way (access keys / profile / default chain) unless `--profile` is passed; credentials are kept in memory only, never written to disk
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

# Pull the last 7 days of CloudWatch Logs + CloudTrail events (prompts for credentials)
python3 pull_aws_logs.py

# Pull only CloudTrail activity from the last 24 hours, no confirmation prompt
python3 pull_aws_logs.py --source cloudtrail --hours 24 --yes

# Use an existing AWS CLI profile instead of prompting, across every enabled region
python3 pull_aws_logs.py --profile myprofile --regions all

# Interactive AWS console menu (prompts for login, then browse services by category)
python3 aws_menu.py

# Same, but skip the login prompt and start in a specific region
python3 aws_menu.py --profile myprofile --region us-west-2
```
