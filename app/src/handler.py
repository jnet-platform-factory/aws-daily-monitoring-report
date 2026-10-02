"""AWS daily monitoring report.

A scheduled Lambda that emails one AWS account's health -- alarms, cost, Lambda,
EventBridge and RDS -- via SES, and optionally publishes the same data as a JSON
snapshot to S3 for a dashboard to render.

Everything account-specific arrives through the environment, set from the stack's
parameters. Nothing here knows which accounts exist: the same artifact is
deployed into every one of them.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The region the report covers. Lambda sets AWS_REGION to its own region, so by
# default a stack reports on the region it is deployed in.
REGION = os.environ.get("MONITORING_REGION", "").strip() or os.environ.get("AWS_REGION", "us-east-1")

# Cost Explorer has a single endpoint, in us-east-1, whatever region it reports on.
COST_EXPLORER_REGION = "us-east-1"

cloudwatch = boto3.client("cloudwatch", region_name=REGION)
ce = boto3.client("ce", region_name=COST_EXPLORER_REGION)
ses = boto3.client("ses", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
rds = boto3.client("rds", region_name=REGION)
events = boto3.client("events", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)

RECIPIENT = os.environ.get("RECIPIENT_EMAIL", "").strip()
SENDER = os.environ.get("SENDER_EMAIL", "").strip()

# Friendly name for the report title. Empty means look it up: the IAM account
# alias, then the raw account id.
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME", "").strip()

# S3 bucket holding <prefix><slug>.json. Empty disables snapshot publishing,
# leaving the email path untouched.
SNAPSHOT_BUCKET = os.environ.get("SNAPSHOT_BUCKET", "").strip()
SNAPSHOT_PREFIX = os.environ.get("SNAPSHOT_PREFIX", "snapshots/").strip()

# Snapshot object key, minus the prefix and the .json suffix, and the key a
# dashboard looks the account up by. Explicit rather than derived from the
# account id: several deployments can share one account (one per stage), and a
# dashboard needs a name it chose, not an id it has to map.
SNAPSHOT_SLUG = os.environ.get("SNAPSHOT_SLUG", "").strip()

# The bus whose rules are sampled. Parameterised because a named bus varies per
# account and does not exist in all of them.
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "").strip() or "default"


def get_account_identity():
    """Resolve (account_id, friendly_name) for the account this Lambda runs in.

    The ACCOUNT_NAME parameter wins. Otherwise the IAM account alias, then the
    raw account id.
    """
    try:
        account_id = sts.get_caller_identity()["Account"]
    except Exception:
        return "unknown", ACCOUNT_NAME or "Unknown"

    if ACCOUNT_NAME:
        return account_id, ACCOUNT_NAME

    try:
        aliases = iam.list_account_aliases().get("AccountAliases", [])
        if aliases:
            return account_id, aliases[0]
    except Exception:
        pass

    return account_id, account_id


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    report_date = now.strftime("%B %d, %Y")
    account_id, account_name = get_account_identity()

    alarms = get_active_alarms()
    costs = get_cost_data(now)
    lambda_health = get_lambda_health(now)
    eventbridge_health = get_eventbridge_health(now)
    rds_health = get_rds_health(now)

    html = build_html_report(account_name, report_date, alarms, costs, lambda_health, eventbridge_health, rds_health)

    # Publish before sending: a snapshot failure must never block the email,
    # hence the try/except inside write_snapshot.
    snapshot = build_snapshot(now, account_id, account_name, alarms, costs, lambda_health, eventbridge_health, rds_health)
    snapshot_written = write_snapshot(snapshot)

    html = html.replace("</body>", snapshot_section(snapshot) + "</body>")
    send_email(account_name, report_date, html, snapshot)

    return {
        "statusCode": 200,
        "body": f"Report sent to {RECIPIENT}",
        "snapshotWritten": snapshot_written,
    }


# ---------------------------------------------------------------------------
# Data collectors
# ---------------------------------------------------------------------------

def get_active_alarms():
    alarms = []
    paginator = cloudwatch.get_paginator("describe_alarms")
    for page in paginator.paginate(StateValue="ALARM"):
        for a in page["MetricAlarms"]:
            alarms.append({
                "name": a["AlarmName"],
                "metric": a.get("MetricName", "N/A"),
                "namespace": a.get("Namespace", "N/A"),
                "reason": a.get("StateReason", "")[:120],
                "since": a.get("StateUpdatedTimestamp", "").isoformat() if hasattr(a.get("StateUpdatedTimestamp", ""), "isoformat") else str(a.get("StateUpdatedTimestamp", "")),
            })
    return alarms


# Cost window. Reported as one day (the latest complete day) against a
# 30-day picture: a week is too short to tell a spike from the shape of a
# billing cycle, and monthly spend is what actually gets reviewed.
COST_WINDOW_DAYS = 30

# Returned when Cost Explorer cannot be read at all. A member account of an
# AWS Organization gets "User not enabled for cost explorer access" until the
# management account turns on member-account access to billing data. Reported
# as an explicit unavailable rather than allowed to abort the run: the alarm,
# Lambda and RDS sections are still worth sending, and $0.00 with no marker
# would read as a free account rather than a blind one.
COSTS_UNAVAILABLE = {
    "available": False,
    "today": 0.0,
    "yesterday": 0.0,
    "avg_30d": 0.0,
    "total_30d": 0.0,
    "window_days": 0,
    "daily_totals": [],
    "top_services": [],
}


def get_cost_data(now):
    end = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=COST_WINDOW_DAYS)).strftime("%Y-%m-%d")

    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
    except Exception as exc:
        logger.error("Cost Explorer unavailable: %s", exc)
        return dict(COSTS_UNAVAILABLE)

    daily_totals = []
    service_totals = {}

    for result in resp["ResultsByTime"]:
        day = result["TimePeriod"]["Start"]
        day_total = 0.0
        for group in result["Groups"]:
            svc = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            service_totals[svc] = service_totals.get(svc, 0.0) + amount
            day_total += amount
        daily_totals.append({"date": day, "total": day_total})

    # Cost Explorer's end date is exclusive, so the last bucket is the most
    # recent COMPLETE day — not the calendar day this runs on.
    today_cost = daily_totals[-1]["total"] if daily_totals else 0
    yesterday_cost = daily_totals[-2]["total"] if len(daily_totals) >= 2 else 0
    total_30d = sum(d["total"] for d in daily_totals)
    avg_30d = total_30d / max(len(daily_totals), 1)

    top_services = sorted(service_totals.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "available": True,
        "today": today_cost,
        "yesterday": yesterday_cost,
        "avg_30d": avg_30d,
        "total_30d": total_30d,
        "window_days": len(daily_totals),
        "daily_totals": daily_totals,
        "top_services": top_services,
    }


def get_lambda_health(now):
    start = now - timedelta(hours=24)
    functions = []

    paginator = lambda_client.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page["Functions"]:
            functions.append(fn["FunctionName"])

    errors = []
    spikes = []
    throttles = []

    for fn_name in functions:
        # Errors
        try:
            resp = cloudwatch.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Errors",
                Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )
            error_count = sum(dp["Sum"] for dp in resp["Datapoints"])
            if error_count > 0:
                errors.append({"name": fn_name, "count": int(error_count)})
        except Exception:
            pass

        # Invocations (spikes)
        try:
            resp = cloudwatch.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Invocations",
                Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )
            inv_count = sum(dp["Sum"] for dp in resp["Datapoints"])
            if inv_count > 10000:
                spikes.append({"name": fn_name, "count": int(inv_count)})
        except Exception:
            pass

        # Throttles
        try:
            resp = cloudwatch.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName="Throttles",
                Dimensions=[{"Name": "FunctionName", "Value": fn_name}],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )
            throttle_count = sum(dp["Sum"] for dp in resp["Datapoints"])
            if throttle_count > 0:
                throttles.append({"name": fn_name, "count": int(throttle_count)})
        except Exception:
            pass

    errors.sort(key=lambda x: x["count"], reverse=True)
    spikes.sort(key=lambda x: x["count"], reverse=True)
    throttles.sort(key=lambda x: x["count"], reverse=True)

    # Concurrent executions (account-level)
    try:
        resp = cloudwatch.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="ConcurrentExecutions",
            StartTime=start,
            EndTime=now,
            Period=3600,
            Statistics=["Maximum"],
        )
        peak_concurrent = max((dp["Maximum"] for dp in resp["Datapoints"]), default=0)
    except Exception:
        peak_concurrent = 0

    return {
        "errors": errors[:10],
        "spikes": spikes[:10],
        "throttles": throttles[:10],
        "peak_concurrent": int(peak_concurrent),
        "total_functions": len(functions),
    }


def get_eventbridge_health(now):
    """Rule invocations on this account's event bus over the last 24h.

    Returns {"bus", "status", "rules"}. A named bus does not exist in every
    account, so a missing bus reports status="unavailable" rather than an empty
    rule list, which would otherwise read as a healthy-but-idle bus.
    """
    start = now - timedelta(hours=24)
    rules_data = []

    try:
        resp = events.list_rules(EventBusName=EVENT_BUS_NAME)
    except Exception as exc:
        logger.warning("EventBridge bus %s unavailable: %s", EVENT_BUS_NAME, exc)
        return {"bus": EVENT_BUS_NAME, "status": "unavailable", "rules": []}

    for rule in resp.get("Rules", []):
        try:
            metric_resp = cloudwatch.get_metric_statistics(
                Namespace="AWS/Events",
                MetricName="Invocations",
                Dimensions=[{"Name": "RuleName", "Value": rule["Name"]}],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )
            inv = sum(dp["Sum"] for dp in metric_resp["Datapoints"])
            if inv > 0:
                rules_data.append({"name": rule["Name"], "invocations": int(inv), "state": rule.get("State", "UNKNOWN")})
        except Exception:
            pass

    rules_data.sort(key=lambda x: x["invocations"], reverse=True)
    return {"bus": EVENT_BUS_NAME, "status": "ok", "rules": rules_data}


def get_rds_health(now):
    start = now - timedelta(hours=24)
    instances = []

    try:
        resp = rds.describe_db_instances()
        for db in resp["DBInstances"]:
            db_id = db["DBInstanceIdentifier"]
            db_class = db["DBInstanceClass"]
            status = db["DBInstanceStatus"]

            # CPU Utilization
            try:
                cpu_resp = cloudwatch.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                    StartTime=start,
                    EndTime=now,
                    Period=3600,
                    Statistics=["Average", "Maximum"],
                )
                avg_cpu = sum(dp["Average"] for dp in cpu_resp["Datapoints"]) / max(len(cpu_resp["Datapoints"]), 1)
                max_cpu = max((dp["Maximum"] for dp in cpu_resp["Datapoints"]), default=0)
            except Exception:
                avg_cpu = 0
                max_cpu = 0

            # Free Storage
            try:
                storage_resp = cloudwatch.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName="FreeStorageSpace",
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                    StartTime=start,
                    EndTime=now,
                    Period=86400,
                    Statistics=["Minimum"],
                )
                free_storage_gb = min((dp["Minimum"] for dp in storage_resp["Datapoints"]), default=0) / (1024 ** 3)
            except Exception:
                free_storage_gb = -1

            # Database Connections
            try:
                conn_resp = cloudwatch.get_metric_statistics(
                    Namespace="AWS/RDS",
                    MetricName="DatabaseConnections",
                    Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_id}],
                    StartTime=start,
                    EndTime=now,
                    Period=3600,
                    Statistics=["Maximum"],
                )
                max_connections = max((dp["Maximum"] for dp in conn_resp["Datapoints"]), default=0)
            except Exception:
                max_connections = 0

            instances.append({
                "id": db_id,
                "class": db_class,
                "status": status,
                "avg_cpu": round(avg_cpu, 1),
                "max_cpu": round(max_cpu, 1),
                "free_storage_gb": round(free_storage_gb, 1),
                "max_connections": int(max_connections),
            })
    except Exception:
        pass

    return instances


# ---------------------------------------------------------------------------
# Derived signals
# ---------------------------------------------------------------------------

def build_action_items(alarms, costs, lambda_health, rds_health):
    """Rule-based action items, shared by the email and the JSON snapshot.

    Single source of truth on purpose: a dashboard renders these verbatim
    rather than re-deriving them, so the two can't disagree.
    """
    items = []

    if alarms:
        items.append(("critical", f"{len(alarms)} CloudWatch alarm(s) in ALARM state — investigate immediately"))
    if lambda_health["throttles"]:
        items.append(("critical", f"{len(lambda_health['throttles'])} Lambda function(s) being throttled — consider increasing concurrency limits"))

    # Cost: report the single worst applicable band, not both. The >2x and
    # >1.3x thresholds overlap, and emitting both for one spike double-counts.
    # With no Cost Explorer reading the bands are meaningless — every figure is
    # a placeholder zero — so say that instead of comparing zeroes.
    if not costs.get("available", True):
        items.append(("warning", "Cost Explorer is not enabled for this account — daily spend is not being monitored"))
    elif costs["today"] > costs["avg_30d"] * 2:
        items.append(("critical", f"Latest day's cost (${costs['today']:.2f}) is more than 2x the 30-day average (${costs['avg_30d']:.2f}) — check for runaway resources"))
    elif costs["today"] > costs["avg_30d"] * 1.3:
        items.append(("warning", f"Latest day's cost (${costs['today']:.2f}) is 30%+ above the 30-day average (${costs['avg_30d']:.2f}) — monitor closely"))

    if any(r["max_cpu"] > 90 for r in rds_health):
        items.append(("critical", "RDS instance(s) with CPU > 90% — consider scaling up"))
    if any(0 < r["free_storage_gb"] < 5 for r in rds_health):
        items.append(("critical", "RDS instance(s) with less than 5 GB free storage — expand storage immediately"))
    if lambda_health["spikes"]:
        items.append(("warning", f"{len(lambda_health['spikes'])} Lambda function(s) with >10K invocations — verify this is expected"))

    return [{"level": level, "text": text} for level, text in items]


# ---------------------------------------------------------------------------
# JSON snapshot
# ---------------------------------------------------------------------------

def build_snapshot(now, account_id, account_name, alarms, costs, lambda_health, eventbridge_health, rds_health):
    """Serialise the same data the email renders into a stable JSON shape.

    `costs.today` is the most recent *complete* day Cost Explorer returned, not
    the calendar day this runs on — CE's end date is exclusive. A dashboard
    should label it accordingly.
    """
    return {
        "schema": 1,
        "generated_at": now.isoformat(),
        "account_id": account_id,
        "account_slug": SNAPSHOT_SLUG or account_id,
        "account_name": account_name,
        "region": REGION,
        "alarms": alarms,
        "costs": {
            # False when Cost Explorer could not be read at all — every figure
            # below is then a placeholder zero, not a measurement.
            "available": costs.get("available", True),
            "today": round(costs["today"], 2),
            "yesterday": round(costs["yesterday"], 2),
            "avg_30d": round(costs["avg_30d"], 2),
            "total_30d": round(costs["total_30d"], 2),
            "window_days": costs.get("window_days", 0),
            "daily_totals": [
                {"date": d["date"], "total": round(d["total"], 2)}
                for d in costs["daily_totals"]
            ],
            # Emitted as objects rather than the internal (name, total) tuples
            # so the JSON is self-describing.
            "top_services": [
                {"service": svc, "total": round(total, 2)}
                for svc, total in costs["top_services"]
            ],
        },
        "lambda": lambda_health,
        "eventbridge": eventbridge_health,
        "rds": rds_health,
        "action_items": build_action_items(alarms, costs, lambda_health, rds_health),
    }


def snapshot_key():
    """The S3 key the snapshot is written to, or None when it must not be written."""
    if not SNAPSHOT_BUCKET:
        return None
    if not SNAPSHOT_SLUG:
        # Refuse rather than fall back to the account id: two stacks in one
        # account would silently overwrite each other's object, and a dashboard
        # keyed by slug would never find it.
        return None
    return f"{SNAPSHOT_PREFIX}{SNAPSHOT_SLUG}.json"


def write_snapshot(snapshot):
    """Publish the snapshot to S3. Never raises — the email matters more."""
    if not SNAPSHOT_BUCKET:
        logger.info("SNAPSHOT_BUCKET unset; skipping snapshot publish.")
        return False

    key = snapshot_key()
    if key is None:
        logger.error("SNAPSHOT_BUCKET is set but SNAPSHOT_SLUG is empty; refusing to publish a snapshot.")
        return False

    try:
        s3.put_object(
            Bucket=SNAPSHOT_BUCKET,
            Key=key,
            Body=json.dumps(snapshot, default=str).encode("utf-8"),
            ContentType="application/json",
            CacheControl="max-age=300",
            # For a bucket in another account that still has ACLs enabled
            # (BucketOwnerPreferred), this is what hands the object to the
            # bucket owner; without it the bucket's own policy would not apply
            # to it. A bucket with ACLs disabled accepts it and ignores it.
            ACL="bucket-owner-full-control",
        )
        logger.info("Wrote snapshot to s3://%s/%s", SNAPSHOT_BUCKET, key)
        return True
    except Exception as exc:
        logger.error("Failed to write snapshot to s3://%s/%s: %s", SNAPSHOT_BUCKET, key, exc)
        return False


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def status_badge(level):
    colors = {"red": "#dc3545", "yellow": "#ffc107", "green": "#28a745"}
    labels = {"red": "CRITICAL", "yellow": "WARNING", "green": "OK"}
    c = colors[level]
    return f'<span style="background:{c};color:white;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:bold;">{labels[level]}</span>'


def cost_trend_arrow(today, yesterday):
    if today > yesterday * 1.2:
        return '<span style="color:#dc3545;">&#9650; UP</span>'
    elif today < yesterday * 0.8:
        return '<span style="color:#28a745;">&#9660; DOWN</span>'
    return '<span style="color:#6c757d;">&#9654; FLAT</span>'


def build_html_report(account_name, report_date, alarms, costs, lambda_health, eventbridge_health, rds_health):
    alarm_count = len(alarms)
    alarm_status = "red" if alarm_count > 0 else "green"
    cost_available = costs.get("available", True)
    # No reading is not the same as a good reading, so an unavailable Cost
    # Explorer is a WARNING rather than the green a $0.00 comparison would give.
    if not cost_available:
        cost_status = "yellow"
    else:
        cost_status = "red" if costs["today"] > costs["avg_30d"] * 2 else ("yellow" if costs["today"] > costs["avg_30d"] * 1.3 else "green")
    lambda_status = "red" if len(lambda_health["errors"]) > 5 or len(lambda_health["throttles"]) > 0 else ("yellow" if len(lambda_health["errors"]) > 0 else "green")
    rds_status = "red" if any(r["max_cpu"] > 90 for r in rds_health) else ("yellow" if any(r["max_cpu"] > 70 for r in rds_health) else "green")

    html = f"""
    <html>
    <head>
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 800px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); color: white; padding: 24px 32px; }}
        .header h1 {{ margin: 0; font-size: 22px; }}
        .header p {{ margin: 4px 0 0; opacity: 0.8; font-size: 14px; }}
        .section {{ padding: 24px 32px; border-bottom: 1px solid #eee; }}
        .section h2 {{ margin: 0 0 16px; font-size: 18px; color: #1a1a2e; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th {{ background: #f8f9fa; text-align: left; padding: 8px 12px; border-bottom: 2px solid #dee2e6; }}
        td {{ padding: 8px 12px; border-bottom: 1px solid #eee; }}
        tr:hover {{ background: #f8f9fa; }}
        .summary-grid {{ display: flex; gap: 16px; margin-bottom: 16px; }}
        .summary-card {{ flex: 1; background: #f8f9fa; border-radius: 8px; padding: 16px; text-align: center; }}
        .summary-card .value {{ font-size: 24px; font-weight: bold; color: #1a1a2e; }}
        .summary-card .label {{ font-size: 12px; color: #6c757d; margin-top: 4px; }}
        .action-item {{ background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px 16px; margin: 8px 0; border-radius: 0 4px 4px 0; font-size: 13px; }}
        .action-critical {{ background: #f8d7da; border-left-color: #dc3545; }}
        .footer {{ padding: 16px 32px; background: #f8f9fa; font-size: 12px; color: #6c757d; text-align: center; }}
      </style>
    </head>
    <body>
      <div class="container">
        <div class="header">
          <h1>{account_name} Daily AWS Monitoring Report</h1>
          <p>{report_date} &mdash; {REGION}</p>
        </div>

        <!-- Overview -->
        <div class="section">
          <h2>Overview</h2>
          <table>
            <tr><th>Area</th><th>Status</th><th>Details</th></tr>
            <tr><td>CloudWatch Alarms</td><td>{status_badge(alarm_status)}</td><td>{alarm_count} active alarm(s)</td></tr>
            <tr><td>Cost</td><td>{status_badge(cost_status)}</td><td>{f"${costs['today']:.2f} today {cost_trend_arrow(costs['today'], costs['yesterday'])}" if cost_available else "Cost Explorer unavailable"}</td></tr>
            <tr><td>Lambda Health</td><td>{status_badge(lambda_status)}</td><td>{len(lambda_health['errors'])} functions with errors, {len(lambda_health['throttles'])} throttled</td></tr>
            <tr><td>RDS Health</td><td>{status_badge(rds_status)}</td><td>{len(rds_health)} instance(s) monitored</td></tr>
          </table>
        </div>

        <!-- Alarms -->
        <div class="section">
          <h2>Active Alarms ({alarm_count})</h2>
    """

    if alarms:
        html += "<table><tr><th>Alarm</th><th>Metric</th><th>Since</th></tr>"
        for a in alarms:
            html += f"<tr><td><strong>{a['name']}</strong></td><td>{a['namespace']} / {a['metric']}</td><td>{a['since']}</td></tr>"
        html += "</table>"
    else:
        html += f'<p>{status_badge("green")} No active alarms.</p>'

    # Cost section
    if not cost_available:
        html += """
        </div>
        <div class="section">
          <h2>Cost Summary</h2>
          <div class="action-item">
            Cost Explorer returned no data for this account. In a member account
            of an AWS Organization, the management account has to enable
            member-account access to billing data before daily spend can be
            reported here.
          </div>
    """
    else:
        html += f"""
        </div>
        <div class="section">
          <h2>Cost Summary</h2>
          <div class="summary-grid">
            <div class="summary-card">
              <div class="value">${costs['today']:.2f}</div>
              <div class="label">Latest Full Day</div>
            </div>
            <div class="summary-card">
              <div class="value">${costs['total_30d']:.2f}</div>
              <div class="label">30-Day Total</div>
            </div>
            <div class="summary-card">
              <div class="value">${costs['avg_30d']:.2f}</div>
              <div class="label">30-Day Average</div>
            </div>
          </div>
          <table>
            <tr><th>Service</th><th>30-Day Total</th><th>Daily Avg</th></tr>
    """
        for svc, total in costs["top_services"]:
            if total > 0.01:
                html += f"<tr><td>{svc}</td><td>${total:.2f}</td><td>${total / max(costs.get('window_days', 30), 1):.2f}</td></tr>"
        html += "</table>"

        # Daily trend
        html += "<h3 style='margin-top:16px;font-size:14px;'>Daily Trend</h3><table><tr><th>Date</th><th>Total</th><th>vs Avg</th></tr>"
        for d in costs["daily_totals"]:
            diff = d["total"] - costs["avg_30d"]
            color = "#dc3545" if diff > 0 else "#28a745"
            html += f"<tr><td>{d['date']}</td><td>${d['total']:.2f}</td><td style='color:{color};'>{'+' if diff > 0 else ''}{diff:.2f}</td></tr>"
        html += "</table>"

    # Lambda Health
    html += f"""
        </div>
        <div class="section">
          <h2>Lambda Health</h2>
          <p><strong>{lambda_health['total_functions']}</strong> functions &mdash; Peak concurrent: <strong>{lambda_health['peak_concurrent']}</strong></p>
    """

    if lambda_health["errors"]:
        html += "<h3 style='font-size:14px;color:#dc3545;'>Top Errors (24h)</h3><table><tr><th>Function</th><th>Errors</th></tr>"
        for e in lambda_health["errors"]:
            html += f"<tr><td>{e['name']}</td><td style='color:#dc3545;font-weight:bold;'>{e['count']:,}</td></tr>"
        html += "</table>"

    if lambda_health["spikes"]:
        html += "<h3 style='font-size:14px;color:#ffc107;'>Invocation Spikes (&gt;10K in 24h)</h3><table><tr><th>Function</th><th>Invocations</th></tr>"
        for s in lambda_health["spikes"]:
            html += f"<tr><td>{s['name']}</td><td style='font-weight:bold;'>{s['count']:,}</td></tr>"
        html += "</table>"

    if lambda_health["throttles"]:
        html += "<h3 style='font-size:14px;color:#dc3545;'>Throttled Functions</h3><table><tr><th>Function</th><th>Throttles</th></tr>"
        for t in lambda_health["throttles"]:
            html += f"<tr><td>{t['name']}</td><td style='color:#dc3545;font-weight:bold;'>{t['count']:,}</td></tr>"
        html += "</table>"

    if not lambda_health["errors"] and not lambda_health["spikes"] and not lambda_health["throttles"]:
        html += f'<p>{status_badge("green")} All Lambda functions healthy.</p>'

    # EventBridge
    eb_rules = eventbridge_health.get("rules", [])
    eb_bus = eventbridge_health.get("bus", "unknown")
    html += f"""
        </div>
        <div class="section">
          <h2>EventBridge Health</h2>
          <p style="font-size:12px;color:#6c757d;margin:0 0 12px;">Bus: <code>{eb_bus}</code></p>
    """
    if eventbridge_health.get("status") != "ok":
        html += f'<p>{status_badge("yellow")} Bus <code>{eb_bus}</code> is not reachable from this account &mdash; no data collected.</p>'
    elif eb_rules:
        html += "<table><tr><th>Rule</th><th>Invocations (24h)</th><th>State</th></tr>"
        for r in eb_rules:
            html += f"<tr><td>{r['name']}</td><td>{r['invocations']:,}</td><td>{r['state']}</td></tr>"
        html += "</table>"
    else:
        html += f'<p>{status_badge("green")} No EventBridge activity in last 24h.</p>'

    # RDS Health
    html += """
        </div>
        <div class="section">
          <h2>RDS Health</h2>
    """
    if rds_health:
        html += "<table><tr><th>Instance</th><th>Class</th><th>Status</th><th>Avg CPU</th><th>Max CPU</th><th>Free Storage</th><th>Max Connections</th></tr>"
        for r in rds_health:
            cpu_color = "#dc3545" if r["max_cpu"] > 90 else ("#ffc107" if r["max_cpu"] > 70 else "#28a745")
            storage_warning = ' style="color:#dc3545;font-weight:bold;"' if 0 < r["free_storage_gb"] < 5 else ""
            html += f"""<tr>
                <td><strong>{r['id']}</strong></td>
                <td>{r['class']}</td>
                <td>{r['status']}</td>
                <td>{r['avg_cpu']}%</td>
                <td style="color:{cpu_color};font-weight:bold;">{r['max_cpu']}%</td>
                <td{storage_warning}>{r['free_storage_gb']} GB</td>
                <td>{r['max_connections']}</td>
            </tr>"""
        html += "</table>"
    else:
        html += "<p>No RDS instances found.</p>"

    # Action Items
    html += """
        </div>
        <div class="section">
          <h2>Action Items</h2>
    """

    action_items = build_action_items(alarms, costs, lambda_health, rds_health)

    if action_items:
        for item in action_items:
            css_class = "action-critical" if item["level"] == "critical" else "action-item"
            html += f'<div class="{css_class}">{item["text"]}</div>'
    else:
        html += f'<p>{status_badge("green")} No action items. Everything looks healthy!</p>'

    html += f"""
        </div>
        <div class="footer">
          Generated automatically by aws-daily-monitoring-report &mdash; {report_date}
        </div>
      </div>
    </body>
    </html>
    """
    return html


def snapshot_section(snapshot):
    """A copy-pasteable copy of the snapshot, appended to the email.

    The same JSON is attached to the message, but an attachment is awkward on
    a phone; this block lets you select it and paste it wherever it is needed.
    """
    blob = json.dumps(snapshot, indent=2, default=str)
    escaped = blob.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""
        <div class="section">
          <h2>Snapshot JSON</h2>
          <p style="font-size:12px;color:#6c757d;margin:0 0 10px;">
            Also attached as <code>{snapshot.get('account_slug', 'snapshot')}.json</code> &mdash;
            the same document the stack publishes to S3 when a snapshot bucket is set.
          </p>
          <pre style="background:#f8f9fa;border:1px solid #dee2e6;border-radius:6px;padding:12px;
                      font-size:11px;line-height:1.45;overflow-x:auto;white-space:pre;
                      font-family:ui-monospace,Menlo,Consolas,monospace;">{escaped}</pre>
        </div>
    """


def send_email(account_name, report_date, html_body, snapshot=None):
    subject = f"{account_name} Daily AWS Monitoring Report - {report_date}"

    if snapshot is None:
        ses.send_email(
            Source=SENDER,
            Destination={"ToAddresses": [RECIPIENT]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"}},
            },
        )
        return

    # Raw MIME so the snapshot can ride along as a real .json attachment.
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = SENDER
    msg["To"] = RECIPIENT

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alternative)

    slug = snapshot.get("account_slug", "snapshot")
    attachment = MIMEApplication(
        json.dumps(snapshot, indent=2, default=str).encode("utf-8"), _subtype="json"
    )
    attachment.add_header("Content-Disposition", "attachment", filename=f"{slug}.json")
    msg.attach(attachment)

    ses.send_raw_email(
        Source=SENDER,
        Destinations=[RECIPIENT],
        RawMessage={"Data": msg.as_bytes()},
    )
