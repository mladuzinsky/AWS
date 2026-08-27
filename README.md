# AWS Tools and Scripts

Tools and scripts for working with AWS, covering both the CLI and the web console.

## Contents

- `install_aws_cli.py` — detects the host OS (Windows or Linux, plus version/architecture) and installs AWS CLI v2 using AWS's official installer for that platform.

## Requirements

- Python 3.6+
- Admin/root privileges for scripts that install software (the CLI installer writes to system directories)

## Usage

```bash
# Detect OS and AWS CLI status only, no changes made
python3 install_aws_cli.py --check-only

# Install (Linux needs sudo, Windows needs an elevated prompt)
sudo python3 install_aws_cli.py

# Reinstall/update even if already present
sudo python3 install_aws_cli.py --force
```
