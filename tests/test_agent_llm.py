"""Agent 层容错测试。

这里不联网：把 `_call_llm` 换掉，专门验证「模型返回脏数据」和「模型挂掉」两种情况下
后端是否守得住。真实 API 的稳定性靠上线前手工验，逻辑正确性靠这里兜住。
"""

from datetime import datetime
from types import SimpleNamespace
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


@pytest.fixture()
def llm_on(monkeypatch):
    """把配置伪装成「模型已就绪」，但不会真发请求。"""
    from app import advisor

    cfg = SimpleNamespace(
        llm_enabled=True,
        llm_base_url="http://fake",
        llm_api_key="fake",
        llm_model="fake",
        llm_timeout=5.0,
    )
    monkeypatch.setattr(advisor, "get_config", lambda: cfg)
    return cfg


def _set_quiet(client, headers, start="15:00", end="15:30"):
    settings = client.get("/api/settings", headers=headers).json()
    settings["quiet_hours"] = [{"start": start, "end": end}]
    assert client.put("/api/settings", json=settings, headers=headers).status_code == 200


def test_dirty_suggestions_are_rejected_one_by_one(client, headers, llm_on, monkeypatch):
    """模型给 6 条烂建议，只能留下合法的那 1 条，其余逐条标注原因。"""
    from app import advisor

    async def fake_call(cfg, context, message):
        return {
            "note": "故意返回各种不合规的时间",
            "suggestions": [
                {"activity_type": "walk", "scheduled_at": "2026-10-30T15:10:00+08:00"},
                {"activity_type": "walk", "scheduled_at": "2026-10-30T08:00:00+08:00"},
                {"activity_type": "dance", "scheduled_at": "2026-10-30T16:00:00+08:00"},
                {"activity_type": "water", "scheduled_at": "2026-10-30T16:30:00+08:00"},
                {"activity_type": "stretch", "scheduled_at": "2026-10-31T10:00:00+08:00"},
                {"activity_type": "walk", "scheduled_at": "下午四点"},
            ],
        }

    monkeypatch.setattr(advisor, "_call_llm", fake_call)
    _set_quiet(client, headers)

    payload = client.post("/api/agent/suggest", json={"message": "排一下"}, headers=headers).json()

    assert payload["mode"] == "llm"
    assert [item["scheduled_at"] for item in payload["suggestions"]] == ["2026-10-30T16:30+08:00"]
    assert {item["reason"] for item in payload["rejected"]} == {
        "IN_QUIET_HOURS",
        "IN_PAST",
        "INVALID_ACTIVITY_TYPE",
        "NOT_TODAY",
        "INVALID_TIME",
    }


def test_model_failure_falls_back_to_rule(client, headers, llm_on, monkeypatch):
    """模型挂了不能把整个功能带下水，必须降级并给出可用的建议。"""
    from app import advisor

    async def boom(cfg, context, message):
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(advisor, "_call_llm", boom)

    response = client.post("/api/agent/suggest", json={"message": "排一下"}, headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "rule"
    assert "降级" in payload["note"]
    assert payload["suggestions"]


def test_empty_suggestions_falls_back_to_rule(client, headers, llm_on, monkeypatch):
    """模型一条都没给、也没说明理由时，不能就这么把空列表甩给用户。"""
    from app import advisor

    async def nothing(cfg, context, message):
        return {"note": "我想不出来", "suggestions": []}

    monkeypatch.setattr(advisor, "_call_llm", nothing)

    payload = client.post("/api/agent/suggest", json={"message": "排一下"}, headers=headers).json()

    assert payload["mode"] == "rule"
    assert payload["suggestions"]


def test_confirm_rechecks_after_settings_change(client, headers):
    """建议通过校验后用户改了设置，确认时必须再拦一次。"""
    _set_quiet(client, headers, "15:00", "15:30")

    response = client.post(
        "/api/activities/confirm",
        json={"items": [{"activity_type": "walk", "scheduled_at": "2026-10-30T15:10:00+08:00"}]},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "IN_QUIET_HOURS"


def test_confirm_rejects_duplicate_close_times(client, headers):
    """两条建议隔不到 10 分钟，第二条必须被拒。"""
    first = client.post(
        "/api/activities/confirm",
        json={"items": [{"activity_type": "walk", "scheduled_at": "2026-10-30T16:00:00+08:00"}]},
        headers=headers,
    )
    assert first.status_code == 200

    second = client.post(
        "/api/activities/confirm",
        json={"items": [{"activity_type": "water", "scheduled_at": "2026-10-30T16:05:00+08:00"}]},
        headers=headers,
    )
    assert second.status_code == 400
    assert second.json()["code"] == "TOO_CLOSE"


def test_parse_content_strips_code_fence():
    from app import advisor

    fenced = '```json\n{"note": "ok", "suggestions": []}\n```'
    assert advisor.parse_content(fenced)["note"] == "ok"
    assert advisor.parse_content('{"note": "raw"}')["note"] == "raw"


def test_parse_content_rejects_broken_json():
    from app import advisor

    with pytest.raises(ValueError):
        advisor.parse_content("模型今天不想输出 JSON")
