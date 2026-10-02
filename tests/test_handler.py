"""The handler's decisions that matter when it is deployed into someone else's account."""
from datetime import datetime, timezone

import pytest

from src import handler

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)

HEALTHY_LAMBDA = {"errors": [], "spikes": [], "throttles": [], "peak_concurrent": 0, "total_functions": 0}
IDLE_BUS = {"bus": "default", "status": "ok", "rules": []}


class Stub:
    """A boto3 client stand-in: each attribute is the callable you give it."""

    def __init__(self, **methods):
        self.__dict__.update(methods)


def raise_(exc):
    def call(*args, **kwargs):
        raise exc
    return call


# --- snapshot key -------------------------------------------------------------

def test_no_bucket_means_no_snapshot(monkeypatch):
    monkeypatch.setattr(handler, "SNAPSHOT_BUCKET", "")
    monkeypatch.setattr(handler, "SNAPSHOT_SLUG", "production")
    assert handler.snapshot_key() is None
    assert handler.write_snapshot({}) is False


def test_bucket_without_slug_refuses_rather_than_falling_back_to_account_id(monkeypatch):
    calls = []
    monkeypatch.setattr(handler, "SNAPSHOT_BUCKET", "my-bucket")
    monkeypatch.setattr(handler, "SNAPSHOT_SLUG", "")
    monkeypatch.setattr(handler, "s3", Stub(put_object=lambda **kw: calls.append(kw)))
    assert handler.snapshot_key() is None
    assert handler.write_snapshot({}) is False
    assert calls == [], "a snapshot was written with no slug"


def test_snapshot_key_uses_prefix_and_slug(monkeypatch):
    calls = []
    monkeypatch.setattr(handler, "SNAPSHOT_BUCKET", "my-bucket")
    monkeypatch.setattr(handler, "SNAPSHOT_PREFIX", "snapshots/")
    monkeypatch.setattr(handler, "SNAPSHOT_SLUG", "production")
    monkeypatch.setattr(handler, "s3", Stub(put_object=lambda **kw: calls.append(kw)))
    assert handler.write_snapshot({"schema": 1}) is True
    assert calls[0]["Bucket"] == "my-bucket"
    assert calls[0]["Key"] == "snapshots/production.json"
    assert calls[0]["ACL"] == "bucket-owner-full-control"


def test_snapshot_write_failure_never_raises(monkeypatch):
    monkeypatch.setattr(handler, "SNAPSHOT_BUCKET", "my-bucket")
    monkeypatch.setattr(handler, "SNAPSHOT_SLUG", "production")
    monkeypatch.setattr(handler, "s3", Stub(put_object=raise_(RuntimeError("AccessDenied"))))
    assert handler.write_snapshot({}) is False


# --- account identity ---------------------------------------------------------

def test_account_name_parameter_wins(monkeypatch):
    monkeypatch.setattr(handler, "ACCOUNT_NAME", "Production")
    monkeypatch.setattr(handler, "sts", Stub(get_caller_identity=lambda: {"Account": "123456789012"}))
    monkeypatch.setattr(handler, "iam", Stub(list_account_aliases=lambda: {"AccountAliases": ["acme-prod"]}))
    assert handler.get_account_identity() == ("123456789012", "Production")


def test_alias_then_account_id(monkeypatch):
    monkeypatch.setattr(handler, "ACCOUNT_NAME", "")
    monkeypatch.setattr(handler, "sts", Stub(get_caller_identity=lambda: {"Account": "123456789012"}))
    monkeypatch.setattr(handler, "iam", Stub(list_account_aliases=lambda: {"AccountAliases": ["acme-prod"]}))
    assert handler.get_account_identity() == ("123456789012", "acme-prod")

    monkeypatch.setattr(handler, "iam", Stub(list_account_aliases=raise_(RuntimeError("denied"))))
    assert handler.get_account_identity() == ("123456789012", "123456789012")


# --- cost explorer ------------------------------------------------------------

def test_cost_explorer_unavailable_degrades_instead_of_failing(monkeypatch):
    monkeypatch.setattr(handler, "ce", Stub(get_cost_and_usage=raise_(RuntimeError("User not enabled for cost explorer access"))))
    costs = handler.get_cost_data(NOW)
    assert costs["available"] is False
    assert costs["today"] == 0.0

    items = handler.build_action_items([], costs, HEALTHY_LAMBDA, [])
    assert [i["level"] for i in items] == ["warning"]
    assert "Cost Explorer is not enabled" in items[0]["text"]

    html = handler.build_html_report("Acme", "September 30, 2026", [], costs, HEALTHY_LAMBDA, IDLE_BUS, [])
    assert "Cost Explorer unavailable" in html
    assert "$0.00" not in html, "an unavailable reading must not render as a free account"


def test_cost_bands_report_only_the_worst():
    costs = {"available": True, "today": 300.0, "yesterday": 100.0, "avg_30d": 100.0,
             "total_30d": 3000.0, "window_days": 30, "daily_totals": [], "top_services": []}
    items = handler.build_action_items([], costs, HEALTHY_LAMBDA, [])
    assert len(items) == 1
    assert items[0]["level"] == "critical"


def test_cost_data_uses_last_complete_day(monkeypatch):
    def results(**kwargs):
        return {"ResultsByTime": [
            {"TimePeriod": {"Start": "2026-09-28"}, "Groups": [
                {"Keys": ["AWS Lambda"], "Metrics": {"UnblendedCost": {"Amount": "1.00"}}}]},
            {"TimePeriod": {"Start": "2026-09-29"}, "Groups": [
                {"Keys": ["AWS Lambda"], "Metrics": {"UnblendedCost": {"Amount": "3.00"}}}]},
        ]}
    monkeypatch.setattr(handler, "ce", Stub(get_cost_and_usage=results))
    costs = handler.get_cost_data(NOW)
    assert costs["available"] is True
    assert costs["today"] == 3.0
    assert costs["yesterday"] == 1.0
    assert costs["top_services"] == [("AWS Lambda", 4.0)]


# --- eventbridge --------------------------------------------------------------

def test_missing_bus_is_unavailable_not_idle(monkeypatch):
    monkeypatch.setattr(handler, "events", Stub(list_rules=raise_(RuntimeError("ResourceNotFoundException"))))
    assert handler.get_eventbridge_health(NOW)["status"] == "unavailable"


# --- snapshot shape -----------------------------------------------------------

def test_snapshot_slug_and_region(monkeypatch):
    monkeypatch.setattr(handler, "SNAPSHOT_SLUG", "production")
    costs = dict(handler.COSTS_UNAVAILABLE)
    snap = handler.build_snapshot(NOW, "123456789012", "Production", [], costs, HEALTHY_LAMBDA, IDLE_BUS, [])
    assert snap["schema"] == 1
    assert snap["account_slug"] == "production"
    assert snap["region"] == handler.REGION
    assert snap["costs"]["available"] is False


@pytest.mark.parametrize("name", ["RECIPIENT", "SENDER"])
def test_no_personal_default_addresses(name, monkeypatch):
    # The defaults come from the stack's required parameters, never the code.
    import importlib
    monkeypatch.delenv(f"{name}_EMAIL", raising=False)
    fresh = importlib.reload(handler)
    try:
        assert getattr(fresh, name) == ""
    finally:
        monkeypatch.undo()
        importlib.reload(handler)
