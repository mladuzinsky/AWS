#!/usr/bin/env python3
"""Detect the host OS/version and install the AWS CLI v2 for it.

Windows: downloads and runs the official AWSCLIV2.msi (64-bit only, admin
required). Linux: downloads the official awscli-exe zip for the detected
architecture (x86_64 or aarch64) and runs its bundled installer (root
required). Both are the install methods AWS documents for CLI v2.
"""

import argparse
import ctypes
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

LINUX_DOWNLOAD_URLS = {
    "x86_64": "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip",
    "aarch64": "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip",
}
WINDOWS_DOWNLOAD_URL = "https://awscli.amazonaws.com/AWSCLIV2.msi"


def read_os_release(path="/etc/os-release"):
    """Parse /etc/os-release by hand (works back to Python 3.6, unlike
    platform.freedesktop_os_release() which needs 3.10+)."""
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                data[key] = value.strip().strip('"')
    except OSError:
        pass
    return data


def detect_os():
    system = platform.system()
    info = {"system": system, "arch": platform.machine()}

    if system == "Linux":
        os_release = read_os_release()
        info["distro"] = os_release.get("PRETTY_NAME", "Unknown Linux")
        info["distro_id"] = os_release.get("ID", "")
        info["distro_version"] = os_release.get("VERSION_ID", "")
    elif system == "Windows":
        release, version, _csd, _ptype = platform.win32_ver()
        build = int(version.split(".")[-1]) if version.split(".")[-1].isdigit() else 0
        if release == "10" and build >= 22000:
            release = "11"
        info["release"] = release
        info["version"] = version
        info["is_64bit"] = platform.architecture()[0] == "64bit"
    elif system == "Darwin":
        info["version"] = platform.mac_ver()[0]

    return info


def get_installed_aws_version():
    aws_path = shutil.which("aws")
    if not aws_path:
        return None
    try:
        result = subprocess.run(
            [aws_path, "--version"], capture_output=True, text=True, check=True
        )
        return (result.stdout or result.stderr).strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def download(url, dest_path):
    print(f"Downloading {url}")
    urllib.request.urlretrieve(url, dest_path)


def install_linux(info, force):
    arch = info["arch"]
    url = LINUX_DOWNLOAD_URLS.get(arch)
    if not url:
        sys.exit(
            f"Unsupported Linux architecture for AWS CLI v2: {arch} "
            f"(supported: {', '.join(LINUX_DOWNLOAD_URLS)})"
        )

    if os.geteuid() != 0:
        sys.exit(
            "Installing the AWS CLI on Linux writes to /usr/local and "
            "/usr/bin, which needs root. Re-run this script with sudo."
        )

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "awscliv2.zip")
        download(url, zip_path)

        print("Extracting installer")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)

        cmd = [os.path.join(tmp, "aws", "install")]
        if force:
            cmd.append("--update")

        print("Running installer:", " ".join(cmd))
        subprocess.run(cmd, check=True)


def install_windows(info, force):
    if not info.get("is_64bit", True):
        sys.exit("AWS CLI v2 only ships a 64-bit Windows installer; this is a 32-bit Python/OS.")

    if not ctypes.windll.shell32.IsUserAnAdmin():
        sys.exit("Installing the AWS CLI on Windows needs admin rights. Re-run from an elevated prompt.")

    with tempfile.TemporaryDirectory() as tmp:
        msi_path = os.path.join(tmp, "AWSCLIV2.msi")
        download(WINDOWS_DOWNLOAD_URL, msi_path)

        print("Running MSI installer")
        cmd = ["msiexec.exe", "/i", msi_path, "/qn"]
        subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="Reinstall/update even if the AWS CLI is already present",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Only detect the OS and report AWS CLI status; do not install",
    )
    args = parser.parse_args()

    info = detect_os()
    print("Detected OS:")
    for key, value in info.items():
        print(f"  {key}: {value}")

    existing = get_installed_aws_version()
    print(f"\nAWS CLI: {existing or 'not found'}")

    if args.check_only:
        return

    if existing and not args.force:
        print("Already installed. Pass --force to reinstall/update.")
        return

    system = info["system"]
    if system == "Linux":
        install_linux(info, args.force)
    elif system == "Windows":
        install_windows(info, args.force)
    else:
        sys.exit(f"This script only installs on Linux and Windows (detected: {system}).")

    print("\nDone. Verifying installation:")
    version = get_installed_aws_version()
    print(version or "aws command not found on PATH — you may need to open a new shell.")


if __name__ == "__main__":
    main()
