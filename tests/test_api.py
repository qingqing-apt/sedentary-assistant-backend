"""接口级测试：把「设置 → 生成 → 操作 → 统计」整条链路跑一遍。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

TZ = ZoneInfo("Asia/Shanghai")
FROZEN = datetime(2026, 10, 30, 9, 0, tzinfo=TZ)


@pytest.fixture()
def client():
    from app import services
    from app.db import Base, engine
    from app.main import app

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    services.set_clock(lambda: FROZEN)

    with TestClient(app) as test_client:
        yield test_client

    services.set_clock(None)


@pytest.fixture()
def headers(client):
    response = client.post("/api/auth/login", json={"username": "tester"})
    return {"Authorization": f"Bearer {response.json()['token']}"}


DEFAULT_SETTINGS = {
    "work_start": "09:00",
    "work_end": "18:00",
    "morning_start": "10:00",
    "morning_end": "12:00",
    "afternoon_start": "14:00",
    "afternoon_end": "17:30",
    "quiet_hours": [],
    "daily_count": 3,
    "reminders_enabled": True,
    "allow_during_work": True,
    "postpone_strategy": "next_slot",
}


def test_requires_login(client):
    assert client.get("/api/settings").status_code == 401


def test_settings_roundtrip(client, headers):
    assert client.put("/api/settings", json=DEFAULT_SETTINGS, headers=headers).status_code == 200
    body = client.get("/api/settings", headers=headers).json()
    assert body["morning_start"] == "10:00"
    assert body["daily_count"] == 3


def test_settings_rejects_window_outside_work_hours(client, headers):
    bad = {**DEFAULT_SETTINGS, "morning_start": "08:00"}
    response = client.put("/api/settings", json=bad, headers=headers)
    assert response.status_code == 400
    assert response.json()["code"] == "WINDOW_OUTSIDE_WORK_HOURS"


def test_settings_rejects_bad_daily_count(client, headers):
    bad = {**DEFAULT_SETTINGS, "daily_count": 0}
    response = client.put("/api/settings", json=bad, headers=headers)
    assert response.json()["code"] == "INVALID_DAILY_COUNT"


def test_generate_then_complete(client, headers):
    client.put("/api/settings", json=DEFAULT_SETTINGS, headers=headers)

    generated = client.post("/api/activities/generate", headers=headers).json()
    assert generated["created"] == 3
    assert [item["scheduled_at"][11:16] for item in generated["activities"]] == [
        "10:55",
        "14:45",
        "16:35",
    ]

    again = client.post("/api/activities/generate", headers=headers).json()
    assert again["created"] == 0
    assert "已经生成过" in again["message"]

    target = generated["activities"][0]["id"]
    done = client.patch(f"/api/activities/{target}/complete", headers=headers).json()
    assert done["status"] == "completed"

    duplicate = client.patch(f"/api/activities/{target}/complete", headers=headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "INVALID_TRANSITION"

    weekly = client.get("/api/stats/weekly", headers=headers).json()
    assert weekly["total"] == 3
    assert weekly["completed"] == 1
    assert weekly["pending"] == 2


def test_postpone_chain_runs_out_and_reports_clearly(client, headers):
    client.put("/api/settings", json=DEFAULT_SETTINGS, headers=headers)
    generated = client.post("/api/activities/generate", headers=headers).json()

    current = generated["activities"][-1]
    assert current["scheduled_at"][11:16] == "16:35"

    response = client.patch(f"/api/activities/{current['id']}/postpone", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["previous_id"] == current["id"]
    assert payload["activity"]["postponed_from"] == current["id"]
    assert payload["activity"]["scheduled_at"][11:16] == "17:05"
    current = payload["activity"]

    exhausted = client.patch(f"/api/activities/{current['id']}/postpone", headers=headers)
    assert exhausted.status_code == 409
    assert exhausted.json()["code"] == "NO_AVAILABLE_SLOT"
    assert "没有更晚" in exhausted.json()["message"]

    weekly = client.get("/api/stats/weekly", headers=headers).json()
    assert weekly["postponed"] == 1


def test_skip_is_terminal(client, headers):
    client.put("/api/settings", json=DEFAULT_SETTINGS, headers=headers)
    generated = client.post("/api/activities/generate", headers=headers).json()
    target = generated["activities"][1]["id"]

    skipped = client.patch(f"/api/activities/{target}/skip", headers=headers).json()
    assert skipped["status"] == "skipped"

    again = client.patch(f"/api/activities/{target}/skip", headers=headers)
    assert again.status_code == 409


def test_activity_of_other_user_is_not_found(client, headers):
    client.put("/api/settings", json=DEFAULT_SETTINGS, headers=headers)
    generated = client.post("/api/activities/generate", headers=headers).json()
    target = generated["activities"][0]["id"]

    other = {"Authorization": "Bearer demo.someone-else"}
    assert client.patch(f"/api/activities/{target}/complete", headers=other).status_code == 404


def test_confirm_rejects_quiet_hour_suggestion(client, headers):
    settings = {**DEFAULT_SETTINGS, "quiet_hours": [{"start": "15:00", "end": "15:30"}]}
    client.put("/api/settings", json=settings, headers=headers)

    response = client.post(
        "/api/activities/confirm",
        headers=headers,
        json={"items": [{"activity_type": "walk", "scheduled_at": "2026-10-30T15:10:00+08:00"}]},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IN_QUIET_HOURS"

    accepted = client.post(
        "/api/activities/confirm",
        headers=headers,
        json={"items": [{"activity_type": "water", "scheduled_at": "2026-10-30T16:00:00+08:00"}]},
    )
    assert accepted.status_code == 200
    assert accepted.json()[0]["source"] == "agent"


def test_agent_suggest_falls_back_to_rules(client, headers):
    client.put("/api/settings", json=DEFAULT_SETTINGS, headers=headers)
    body = client.post("/api/agent/suggest", json={"message": "今天只想安排两次"}, headers=headers).json()

    assert body["mode"] == "rule"
    assert len(body["suggestions"]) == 2
    for item in body["suggestions"]:
        assert item["scheduled_at"].startswith("2026-10-30T")


def test_agent_suggest_when_day_is_full(client, headers):
    client.put("/api/settings", json=DEFAULT_SETTINGS, headers=headers)
    client.post("/api/activities/generate", headers=headers)

    body = client.post("/api/agent/suggest", json={"message": ""}, headers=headers).json()
    assert body["suggestions"] == []
    assert "排满" in body["note"]


def test_quiet_hours_are_respected_in_generated_plan(client, headers):
    settings = {
        **DEFAULT_SETTINGS,
        "quiet_hours": [{"start": "12:00", "end": "13:30"}],
        "daily_count": 5,
    }
    client.put("/api/settings", json=settings, headers=headers)
    generated = client.post("/api/activities/generate", headers=headers).json()

    for item in generated["activities"]:
        clock = item["scheduled_at"][11:16]
        assert not ("12:00" <= clock < "13:30"), item
