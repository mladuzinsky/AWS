#!/usr/bin/env python3
"""Interactively collect AWS credentials (or use a named profile), then pull
CloudWatch Logs and CloudTrail management events for a time window and save
them to local JSON-lines files.

Credentials entered at the prompt are held only in memory for this process —
nothing is written to ~/.aws. Prefer temporary/short-lived keys (STS session
token or SSO) over long-lived IAM user keys.

Required IAM actions:
  sts:GetCallerIdentity
  logs:DescribeLogGroups
  logs:FilterLogEvents
  cloudtrail:LookupEvents
  ec2:DescribeRegions   (only with --regions all)

Notes:
  - CloudWatch Logs and CloudTrail LookupEvents are regional. By default this
    script queries only the session region. Use --regions all or
    --regions us-east-1,us-west-2 to cover more.
  - lookup_events returns only the last 90 days of MANAGEMENT events. It is
    not a full CloudTrail Lake / S3 trail (no data events).
  - Pulling every CloudWatch log group can mean a lot of API calls and data.
    Use --filter, --max-groups, and --max-events-per-group. The script lists
    groups and asks before downloading unless --yes is passed.
  - Exported files can contain secrets from log lines and API parameters.
    The output directory is created mode 0700; files are 0600.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})
CLOUDTRAIL_MAX_DAYS = 90


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def prompt_credentials(default_region: str | None) -> boto3.Session:
    default_region = default_region or "us-east-1"
    print("== AWS credentials (kept in memory only, never written to disk) ==")
    print("Access Key ID will not be echoed.")
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
    return boto3.Session(profile_name=profile, region_name=region)


def client_for(session: boto3.Session, service: str, region: str):
    return session.client(service, region_name=region, config=RETRY_CONFIG)


def verify_identity(session: boto3.Session) -> dict:
    sts = client_for(session, "sts", session.region_name or "us-east-1")
    try:
        identity = sts.get_caller_identity()
    except (NoCredentialsError, ClientError, BotoCoreError) as e:
        sys.exit(f"Could not authenticate to AWS: {e}")
    print(
        f"Authenticated as {identity['Arn']} "
        f"(account {identity['Account']}) default region {session.region_name}"
    )
    return identity


def parse_time_range(args) -> tuple[datetime.datetime, datetime.datetime]:
    if args.days is not None and args.days <= 0:
        sys.exit("--days must be > 0")
    if args.hours is not None and args.hours <= 0:
        sys.exit("--hours must be > 0")

    now = datetime.datetime.now(datetime.timezone.utc)
    if args.hours is not None:
        hours = args.hours
    else:
        hours = args.days * 24
    start = now - datetime.timedelta(hours=hours)
    return start, now


def parse_regions(session: boto3.Session, regions_arg: str | None) -> list[str]:
    default = session.region_name or "us-east-1"
    if not regions_arg:
        return [default]
    if regions_arg.strip().lower() == "all":
        ec2 = client_for(session, "ec2", default)
        try:
            resp = ec2.describe_regions(AllRegions=False)
        except ClientError as e:
            sys.exit(f"Could not list regions (need ec2:DescribeRegions): {e}")
        names = sorted(r["RegionName"] for r in resp.get("Regions", []))
        if not names:
            sys.exit("DescribeRegions returned no regions.")
        return names
    names = [p.strip() for p in regions_arg.split(",") if p.strip()]
    if not names:
        sys.exit("--regions was empty.")
    return names


def sanitize_filename(name: str, max_len: int = 80) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name.strip("/")) or "root"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    if len(safe) > max_len:
        safe = safe[:max_len]
    return f"{safe}_{digest}"


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{prompt} ({hint}): ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def secure_makedirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def atomic_write_jsonl(path: str, rows_iter) -> int:
    """Write rows as JSONL to path via a temp file. Returns row count.
    Removes path if zero rows. Sets mode 0600 on success."""
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".jsonl", dir=directory)
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows_iter:
                f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
                count += 1
        if count == 0:
            os.remove(tmp_path)
            if os.path.exists(path):
                os.remove(path)
            return 0
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return count
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def print_runtime_warnings(regions: list[str], start: datetime.datetime, end: datetime.datetime, source: str) -> None:
    print("\n== Scope and limits ==")
    print(f"Regions: {', '.join(regions)}")
    if len(regions) == 1:
        print(
            "Only this one region will be queried. CloudWatch log groups and "
            "most CloudTrail LookupEvents in other regions will be missed. "
            "Pass --regions all or --regions us-east-1,us-west-2 to expand."
        )
    print(
        "CloudTrail lookup_events: last 90 days of MANAGEMENT events only "
        "(not data events, not a substitute for the S3/Lake trail)."
    )
    window_days = (end - start).total_seconds() / 86400
    if source in ("cloudtrail", "both") and window_days > CLOUDTRAIL_MAX_DAYS:
        print(
            f"Requested window is ~{window_days:.1f} days; CloudTrail start "
            f"will be clamped to {CLOUDTRAIL_MAX_DAYS} days ago. CloudWatch "
            "is not clamped."
        )
    print(
        "Output may contain secrets from log lines and API parameters. "
        "Treat the directory as sensitive."
    )
    print(
        "IAM needed: sts:GetCallerIdentity, logs:DescribeLogGroups, "
        "logs:FilterLogEvents, cloudtrail:LookupEvents"
        + (", ec2:DescribeRegions" if len(regions) != 1 else "")
    )


def iter_log_groups(client, name_filter: str | None):
    paginator = client.get_paginator("describe_log_groups")
    for page in paginator.paginate():
        for group in page.get("logGroups", []):
            name = group.get("logGroupName", "")
            if name_filter and name_filter.lower() not in name.lower():
                continue
            yield group


def export_cloudwatch_logs(
    session: boto3.Session,
    regions: list[str],
    start: datetime.datetime,
    end: datetime.datetime,
    out_dir: str,
    max_events: int | None,
    name_filter: str | None,
    max_groups: int | None,
    skip_confirm: bool,
) -> int:
    cw_root = os.path.join(out_dir, "cloudwatch")
    secure_makedirs(cw_root)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    total_events = 0

    for region in regions:
        client = client_for(session, "logs", region)
        print(f"\nCloudWatch: listing log groups in {region}...")
        try:
            groups_iter = iter_log_groups(client, name_filter)
            if max_groups is not None:
                groups_iter = itertools.islice(groups_iter, max_groups)
            groups = list(groups_iter)
        except ClientError as e:
            eprint(f"  {region}: describe_log_groups failed: {e}")
            continue

        if not groups:
            print(f"  No matching CloudWatch log groups in {region}.")
            continue

        region_dir = os.path.join(cw_root, sanitize_filename(region))
        secure_makedirs(region_dir)

        print(f"  Found {len(groups)} CloudWatch log group(s) in {region}:")
        preview = groups if len(groups) <= 50 else groups[:50]
        for g in preview:
            print(f"    {g['logGroupName']}")
        if len(groups) > 50:
            print(f"    ... and {len(groups) - 50} more")

        if not skip_confirm and not ask_yes_no(
            f"\nDownload events from {start.isoformat()} to {end.isoformat()} "
            f"for these {len(groups)} group(s) in {region}?",
            True,
        ):
            print(f"  Skipping CloudWatch in {region}.")
            continue

        for i, group in enumerate(groups, 1):
            group_name = group["logGroupName"]
            out_path = os.path.join(region_dir, sanitize_filename(group_name) + ".jsonl")
            state = {"truncated": False}

            def event_rows():
                count = 0
                events_paginator = client.get_paginator("filter_log_events")
                for page in events_paginator.paginate(
                    logGroupName=group_name,
                    startTime=start_ms,
                    endTime=end_ms,
                ):
                    for event in page.get("events", []):
                        if max_events and count >= max_events:
                            state["truncated"] = True
                            return
                        yield {
                            "region": region,
                            "logGroup": group_name,
                            "logStreamName": event.get("logStreamName"),
                            "timestamp": event.get("timestamp"),
                            "ingestionTime": event.get("ingestionTime"),
                            "message": event.get("message"),
                        }
                        count += 1

            try:
                count = atomic_write_jsonl(out_path, event_rows())
            except ClientError as e:
                eprint(f"  [{i}/{len(groups)}] {group_name}: ERROR {e}")
                continue
            except OSError as e:
                eprint(f"  [{i}/{len(groups)}] {group_name}: write ERROR {e}")
                continue

            cap_note = f" (capped at {max_events}, more were available)" if state["truncated"] else ""
            print(f"  [{i}/{len(groups)}] {group_name}: {count} events{cap_note}")
            total_events += count

    print(f"CloudWatch Logs: {total_events} total events written under {cw_root}")
    return total_events


def export_cloudtrail_events(
    session: boto3.Session,
    regions: list[str],
    start: datetime.datetime,
    end: datetime.datetime,
    out_dir: str,
) -> int:
    ct_root = os.path.join(out_dir, "cloudtrail")
    secure_makedirs(ct_root)

    ninety_days_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=CLOUDTRAIL_MAX_DAYS
    )
    effective_start = start
    if effective_start < ninety_days_ago:
        print(
            f"Note: CloudTrail lookup_events only covers the last {CLOUDTRAIL_MAX_DAYS} days; "
            f"clamping start time to {ninety_days_ago.isoformat()}."
        )
        effective_start = ninety_days_ago

    total = 0
    for region in regions:
        client = client_for(session, "cloudtrail", region)
        out_path = os.path.join(ct_root, f"{sanitize_filename(region)}.jsonl")

        def event_rows():
            paginator = client.get_paginator("lookup_events")
            for page in paginator.paginate(StartTime=effective_start, EndTime=end):
                for record in page.get("Events", []):
                    row = dict(record)
                    row["queryRegion"] = region
                    if "EventTime" in row and hasattr(row["EventTime"], "isoformat"):
                        row["EventTime"] = row["EventTime"].isoformat()
                    raw = row.get("CloudTrailEvent")
                    if isinstance(raw, str):
                        try:
                            row["CloudTrailEvent"] = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    yield row

        try:
            count = atomic_write_jsonl(out_path, event_rows())
        except ClientError as e:
            eprint(f"CloudTrail {region}: lookup_events failed: {e}")
            continue
        except OSError as e:
            eprint(f"CloudTrail {region}: write failed: {e}")
            continue

        if count == 0:
            print(f"CloudTrail {region}: no events found in range.")
        else:
            print(f"CloudTrail {region}: {count} events written to {out_path}")
        total += count

    print(f"CloudTrail: {total} total events written under {ct_root}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        choices=["cloudwatch", "cloudtrail", "both"],
        default="both",
        help="Which logs to pull (default: both)",
    )
    parser.add_argument("--days", type=int, default=7, help="How many days back to pull (default: 7)")
    parser.add_argument("--hours", type=int, default=None, help="Override --days with an exact hour window")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Where to write logs (default: ./aws_logs_export_<timestamp>)",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="Only pull CloudWatch log groups whose name contains this substring",
    )
    parser.add_argument(
        "--max-events-per-group",
        type=int,
        default=None,
        help="Cap events pulled per CloudWatch log group (default: no cap)",
    )
    parser.add_argument(
        "--max-groups",
        type=int,
        default=None,
        help="Cap how many CloudWatch log groups to pull per region",
    )
    parser.add_argument(
        "--regions",
        default=None,
        help="Comma-separated regions, or 'all'. Default: the session region only",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Use an existing AWS shared-credentials profile instead of prompting",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Default region for auth and for --regions default "
        "(default: profile's configured region, or us-east-1 if unset)",
    )
    parser.add_argument("--yes", action="store_true", help="Skip CloudWatch confirmation prompts")
    args = parser.parse_args()

    if args.max_events_per_group is not None and args.max_events_per_group <= 0:
        sys.exit("--max-events-per-group must be > 0")
    if args.max_groups is not None and args.max_groups <= 0:
        sys.exit("--max-groups must be > 0")

    if args.profile:
        session = session_from_profile(args.profile, args.region)
    else:
        session = prompt_credentials(args.region)

    verify_identity(session)
    start, end = parse_time_range(args)
    regions = parse_regions(session, args.regions)

    out_dir = args.output_dir or f"aws_logs_export_{end.strftime('%Y%m%dT%H%M%SZ')}"
    secure_makedirs(out_dir)

    print(f"\nTime range: {start.isoformat()} -> {end.isoformat()}")
    print(f"Output directory: {os.path.abspath(out_dir)} (mode 0700)")
    print_runtime_warnings(regions, start, end, args.source)

    if not args.yes and not ask_yes_no("\nContinue?", True):
        sys.exit("Cancelled.")

    total = 0
    if args.source in ("cloudwatch", "both"):
        total += export_cloudwatch_logs(
            session,
            regions,
            start,
            end,
            out_dir,
            args.max_events_per_group,
            args.filter,
            args.max_groups,
            skip_confirm=args.yes,
        )
    if args.source in ("cloudtrail", "both"):
        total += export_cloudtrail_events(session, regions, start, end, out_dir)

    print(f"\nDone. {total} total events saved under {os.path.abspath(out_dir)}")
    print("Treat this directory as sensitive.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
