"""提议单生命周期测试。

这组测试守的是 C06 那条规则：

    Agent 提议只写 proposals，用户确认才写 activities

所以每个用例都要同时断言两件事——提议单的状态，和活动表的行数。
只断言前者会漏掉「界面显示待确认，其实已经偷偷落库」这种最严重的错误。
"""

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


def _make_proposal(user_id: str, items: list[dict], note: str = "") -> str:
    """直接走服务层建单，绕开模型——这组测试不关心建议是怎么产生的。"""
    from app import proposals
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        return proposals.create(db, user_id, items, note=note).id
    finally:
        db.close()


def _activity_count(user_id: str) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Activity

    db = SessionLocal()
    try:
        rows = db.scalars(select(Activity).where(Activity.user_id == user_id)).all()
        return len(rows)
    finally:
        db.close()


ITEMS = [
    {"activity_type": "walk", "title": "起身走动", "scheduled_at": "2026-10-30T10:30+08:00"},
    {"activity_type": "water", "title": "去接杯水", "scheduled_at": "2026-10-30T15:00+08:00"},
]


def test_no_pending_proposal_at_start(client, headers):
    response = client.get("/api/proposals/pending", headers=headers)
    assert response.status_code == 200
    # 没有待确认建议时返回 null，前端据此不渲染卡片
    assert response.json() is None


def test_pending_proposal_does_not_touch_activities(client, headers):
    proposal_id = _make_proposal("tester", ITEMS)

    body = client.get("/api/proposals/pending", headers=headers).json()
    assert body["id"] == proposal_id
    assert body["status"] == "pending"
    assert len(body["items"]) == 2
    assert body["items"][0]["title"] == "起身走动"

    # 关键断言：卡片上有建议，但活动表一行都没多
    assert _activity_count("tester") == 0


def test_confirm_creates_activities_and_closes_proposal(client, headers):
    proposal_id = _make_proposal("tester", ITEMS)

    response = client.post(
        f"/api/proposals/{proposal_id}/confirm",
        json={"items": [{"activity_type": i["activity_type"], "scheduled_at": i["scheduled_at"]} for i in ITEMS]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["proposal"]["status"] == "confirmed"
    assert len(payload["activities"]) == 2
    assert payload["activities"][0]["source"] == "agent"

    assert _activity_count("tester") == 2
    assert client.get("/api/proposals/pending", headers=headers).json() is None


def test_confirm_only_selected_subset(client, headers):
    proposal_id = _make_proposal("tester", ITEMS)

    response = client.post(
        f"/api/proposals/{proposal_id}/confirm",
        json={"items": [{"activity_type": "walk", "scheduled_at": ITEMS[0]["scheduled_at"]}]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["activities"]) == 1
    assert _activity_count("tester") == 1
    # 整张单子结案，没勾的那条不会留着继续挂着
    assert response.json()["proposal"]["status"] == "confirmed"


def test_confirm_rejects_quiet_hour_and_keeps_proposal_pending(client, headers):
    settings = client.get("/api/settings", headers=headers).json()
    settings["quiet_hours"] = [{"start": "14:30", "end": "15:30"}]
    assert client.put("/api/settings", json=settings, headers=headers).status_code == 200

    proposal_id = _make_proposal("tester", ITEMS)
    response = client.post(
        f"/api/proposals/{proposal_id}/confirm",
        json={"items": [{"activity_type": i["activity_type"], "scheduled_at": i["scheduled_at"]} for i in ITEMS]},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IN_QUIET_HOURS"
    # 校验没过不能把单子作废，用户改完设置还能再点一次
    assert _activity_count("tester") == 0
    pending = client.get("/api/proposals/pending", headers=headers).json()
    assert pending["id"] == proposal_id
    assert pending["status"] == "pending"


def test_discard_leaves_no_trace(client, headers):
    proposal_id = _make_proposal("tester", ITEMS)

    response = client.post(f"/api/proposals/{proposal_id}/discard", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "discarded"

    assert _activity_count("tester") == 0
    assert client.get("/api/proposals/pending", headers=headers).json() is None


def test_new_proposal_supersedes_old_one(client, headers):
    first = _make_proposal("tester", ITEMS)
    second = _make_proposal(
        "tester",
        [{"activity_type": "stretch", "title": "站立伸展", "scheduled_at": "2026-10-30T16:00+08:00"}],
    )

    pending = client.get("/api/proposals/pending", headers=headers).json()
    assert pending["id"] == second
    assert len(pending["items"]) == 1

    # 作废的那张不能再确认，否则用户会存进一批自己没看过的建议
    stale = client.post(
        f"/api/proposals/{first}/confirm",
        json={"items": [{"activity_type": "walk", "scheduled_at": ITEMS[0]["scheduled_at"]}]},
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "PROPOSAL_NOT_PENDING"
    assert _activity_count("tester") == 0


def test_cannot_confirm_other_users_proposal(client, headers):
    proposal_id = _make_proposal("someone-else", ITEMS)

    response = client.post(
        f"/api/proposals/{proposal_id}/confirm",
        json={"items": [{"activity_type": "walk", "scheduled_at": ITEMS[0]["scheduled_at"]}]},
        headers=headers,
    )
    assert response.status_code == 404
    assert _activity_count("someone-else") == 0


def test_double_confirm_returns_conflict(client, headers):
    proposal_id = _make_proposal("tester", ITEMS)
    body = {"items": [{"activity_type": "walk", "scheduled_at": ITEMS[0]["scheduled_at"]}]}

    assert client.post(f"/api/proposals/{proposal_id}/confirm", json=body, headers=headers).status_code == 200
    again = client.post(f"/api/proposals/{proposal_id}/confirm", json=body, headers=headers)
    assert again.status_code == 409
    assert _activity_count("tester") == 1


def test_propose_tool_writes_proposal_not_activity(client):
    """走真正的工具函数：确认前 activity 表必须是空的。"""
    from app.agent import tools

    raw = tools._propose_payload("tester", 2, "")
    import json

    payload = json.loads(raw)
    assert payload["proposal_id"]
    assert len(payload["proposals"]) == 2
    assert payload["must_confirm"] is True
    assert payload["remaining_capacity"] >= 2

    assert _activity_count("tester") == 0


def test_capacity_tool_agrees_with_propose(client):
    """容量说能排几次，就真的能排几条——两个数字必须来自同一套算法。"""
    import json

    from app.agent import tools

    capacity = json.loads(tools._capacity_payload("tester", ""))["can_fit"]
    payload = json.loads(tools._propose_payload("tester", 99, ""))

    assert capacity >= 1
    assert len(payload["proposals"]) == min(capacity, 5)  # 单次最多 5 条
