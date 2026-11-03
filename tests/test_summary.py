"""每日总结：重点是「没生成过」必须能被表达出来。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

TZ = ZoneInfo("Asia/Shanghai")
FROZEN = datetime(2026, 10, 30, 9, 0, tzinfo=TZ)
YESTERDAY = "2026-10-29"


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


def seed(user_id: str, day: str, statuses: list[str]) -> None:
    """直接写库造历史数据——接口只能造今天的，日报要看昨天。

    推迟是「派生新记录」，两条记录共享同一天，所以这里可以出现两条 postponed。
    """
    from app.db import SessionLocal
    from app.models import Activity

    with SessionLocal() as db:
        for index, status in enumerate(statuses):
            db.add(
                Activity(
                    user_id=user_id,
                    day=day,
                    activity_type="walk",
                    title=f"起身走动 {index + 1}",
                    description="站起来走两分钟",
                    scheduled_at=f"{day}T{10 + index:02d}:00+08:00",
                    status=status,
                    source="user",
                )
            )
        db.commit()


def _uid(client, headers) -> str:
    """当前测试用户的 user_id。

    settings 行是「首次访问时按默认值建」的，所以先 GET 一次确保它存在，
    否则直接查库会拿到 None。
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ActivitySettings

    client.get("/api/settings", headers=headers)

    with SessionLocal() as db:
        row = db.scalars(select(ActivitySettings)).first()
        return row.user_id


def test_defaults_to_yesterday_and_is_not_generated(client, headers):
    """没有生成过就是 not_generated，summary 必须为 null。"""
    body = client.get("/api/summary/daily", headers=headers).json()

    assert body["date"] == YESTERDAY
    assert body["state"] == "not_generated"
    assert body["summary"] is None
    # 没数据也要给出真实统计，而不是编一个数字
    assert body["stats"]["total"] == 0


def test_stats_are_real_before_generation(client, headers):
    uid = _uid(client, headers)
    seed(uid, YESTERDAY, ["completed", "completed", "postponed", "skipped"])

    body = client.get(f"/api/summary/daily?date={YESTERDAY}", headers=headers).json()

    assert body["state"] == "not_generated"
    assert body["summary"] is None
    assert body["stats"] == {
        "date": YESTERDAY,
        "total": 4,
        "completed": 2,
        "postponed": 1,
        "skipped": 1,
        "pending": 0,
        "completion_rate": 50,
    }


def test_generate_then_read(client, headers):
    uid = _uid(client, headers)
    seed(uid, YESTERDAY, ["completed", "completed", "completed"])

    generated = client.post(f"/api/summary/daily?date={YESTERDAY}", headers=headers).json()
    assert generated["state"] == "generated"
    assert generated["summary"]["source"] == "rule"
    assert generated["summary"]["headline"] == "全部完成，节奏保持住了"
    assert generated["summary"]["points"]

    reread = client.get(f"/api/summary/daily?date={YESTERDAY}", headers=headers).json()
    assert reread["state"] == "generated"
    assert reread["summary"]["headline"] == generated["summary"]["headline"]


def test_regenerate_overwrites(client, headers):
    uid = _uid(client, headers)
    seed(uid, YESTERDAY, ["completed", "skipped", "skipped"])

    first = client.post(f"/api/summary/daily?date={YESTERDAY}", headers=headers).json()
    assert first["stats"]["completion_rate"] == 33

    # 补记两条完成后再生成，文案要跟着真实数据变，不能还停在旧结论上
    seed(uid, YESTERDAY, ["completed", "completed"])
    second = client.post(f"/api/summary/daily?date={YESTERDAY}", headers=headers).json()

    assert second["stats"]["completed"] == 3
    assert second["stats"]["completion_rate"] == 60
    # 33% → 60% 跨过了「过半」这条线，标题必须换
    assert first["summary"]["headline"] == "完成了一部分，明天可以少排一点"
    assert second["summary"]["headline"] == "完成过半，整体节奏还在"


def test_empty_day_can_still_be_generated(client, headers):
    body = client.post(f"/api/summary/daily?date={YESTERDAY}", headers=headers).json()

    assert body["state"] == "generated"
    assert body["summary"]["headline"] == "这天没有安排任何活动"


def test_rejects_future_date(client, headers):
    response = client.get("/api/summary/daily?date=2026-11-01", headers=headers)

    assert response.status_code == 400
    assert response.json()["code"] == "FUTURE_DATE"


def test_rejects_bad_date(client, headers):
    response = client.get("/api/summary/daily?date=2026/10/29", headers=headers)

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_DATE"


def test_requires_login(client):
    assert client.get("/api/summary/daily").status_code == 401
    assert client.post("/api/summary/daily").status_code == 401


def test_scoped_to_user(client, headers):
    """别人的日报不能混进来。"""
    from app.db import SessionLocal
    from app.models import Activity

    with SessionLocal() as db:
        db.add(
            Activity(
                user_id="someone-else",
                day=YESTERDAY,
                activity_type="walk",
                title="别人的活动",
                description="",
                scheduled_at=f"{YESTERDAY}T10:00+08:00",
                status="completed",
                source="user",
            )
        )
        db.commit()

    body = client.get(f"/api/summary/daily?date={YESTERDAY}", headers=headers).json()
    assert body["stats"]["total"] == 0
