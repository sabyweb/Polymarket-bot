"""Lightweight tests for dashboard API endpoints."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_cohort_for_is_deterministic():
    from api.queries import cohort_for
    assert cohort_for("0xabc", 3) == cohort_for("0xabc", 3)
    assert cohort_for("0xabc", 1) == 0


def test_health_endpoint():
    with patch(
        "api.queries.get_health",
        return_value={
            "safety_state": "OK",
            "safety_reason": "",
            "safety_since": "1m ago",
            "last_fill": "30s ago",
            "last_order": "1m ago",
            "last_cycle": "20s ago",
            "last_agent": "5m ago",
            "active_orders": 10,
            "active_dumps": 0,
            "db_size_mb": 12.3,
            "db_updated": "10s ago",
            "heartbeats": {"farmer": "15s ago", "oversight": "1m ago"},
        },
    ):
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["safety_state"] == "OK"


def test_cohort_latest_endpoint():
    with patch(
        "api.queries.get_cohort_latest",
        return_value=[
            {
                "cohort": 0,
                "reward_earned": 1.0,
                "unwind_pnl": -2.0,
                "net_pnl": -1.0,
                "fill_count": 5,
                "deployed_markets": 10,
                "target_capital": 100.0,
                "return_pct": -1.0,
            }
        ],
    ):
        resp = client.get("/api/ab-cohorts/latest")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["cohort"] == 0


def test_logs_rejects_unknown_service():
    resp = client.get("/api/logs?service=unknown")
    assert resp.status_code == 400
