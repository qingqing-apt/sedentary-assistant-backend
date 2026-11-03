"""每日总结的模型生成：成功用模型，任何一种失败都必须降级成规则版。

这组测试要守住的核心不是「模型写得好不好」，而是：
**模型出任何问题，用户看到的总结都不能缺、不能是半成品、接口不能 500。**
"""

from __future__ import annotations

import asyncio

import pytest

from app import digest
from app.config import AppConfig
from app.schemas import DailyStats

STATS = DailyStats(
    date="2026-11-02",
    total=3,
    completed=1,
    postponed=1,
    skipped=0,
    pending=1,
    completion_rate=33,
)


def _cfg(enabled: bool) -> AppConfig:
    return AppConfig(
        llm_base_url="https://example.invalid/v1" if enabled else "",
        llm_api_key="sk-test" if enabled else "",
        llm_model="test-model",
        llm_timeout=1.0,
    )


class _FakeResponse:
    def __init__(self, content: str | None = None, status: int = 200):
        self._content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _patch_client(monkeypatch, *, content: str | None = None, raises: Exception | None = None):
    """替换 httpx.AsyncClient，避免真联网。"""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            if raises:
                raise raises
            return _FakeResponse(content)

    monkeypatch.setattr(digest.httpx, "AsyncClient", FakeClient)


def test_no_key_falls_back_to_rule(monkeypatch):
    monkeypatch.setattr(digest, "get_config", lambda: _cfg(False))
    headline, points, source = asyncio.run(digest.compose(STATS))
    assert source == "rule"
    assert "完成" in "".join(points)


def test_model_output_is_used(monkeypatch):
    monkeypatch.setattr(digest, "get_config", lambda: _cfg(True))
    _patch_client(
        monkeypatch,
        content='{"headline": "完成了一条，节奏还行", "points": ["计划 3 条完成 1 条。"]}',
    )
    headline, points, source = asyncio.run(digest.compose(STATS))
    assert source == "agent"
    assert headline == "完成了一条，节奏还行"
    assert points == ["计划 3 条完成 1 条。"]


def test_code_fence_is_stripped(monkeypatch):
    monkeypatch.setattr(digest, "get_config", lambda: _cfg(True))
    _patch_client(
        monkeypatch,
        content='```json\n{"headline": "还行", "points": ["一条"]}\n```',
    )
    headline, _, source = asyncio.run(digest.compose(STATS))
    assert (headline, source) == ("还行", "agent")


@pytest.mark.parametrize(
    "content",
    [
        "不是 JSON",
        '{"points": ["有 points 没 headline"]}',
        '{"headline": "有 headline 没 points"}',
        '{"headline": "很长的标题" , "points": []}',
        '{"headline": "' + "长" * 60 + '", "points": ["x"]}',
        '{"headline": "正常", "points": [""]}',
        '{"headline": "正常", "points": ["' + "长" * 200 + '"]}',
    ],
)
def test_dirty_output_falls_back(monkeypatch, content):
    """任何一种不合规输出都必须降级，不能把半成品落库。"""
    monkeypatch.setattr(digest, "get_config", lambda: _cfg(True))
    _patch_client(monkeypatch, content=content)
    headline, points, source = asyncio.run(digest.compose(STATS))
    assert source == "rule"
    assert headline and points


@pytest.mark.parametrize(
    "exc",
    [TimeoutError("timeout"), RuntimeError("HTTP 500"), ValueError("bad json")],
)
def test_transport_errors_fall_back(monkeypatch, exc):
    monkeypatch.setattr(digest, "get_config", lambda: _cfg(True))
    _patch_client(monkeypatch, raises=exc)
    headline, points, source = asyncio.run(digest.compose(STATS))
    assert source == "rule"
    assert headline and points


def test_points_are_capped(monkeypatch):
    """模型给太多条就截断，别把一整段文案怼到界面上。"""
    monkeypatch.setattr(digest, "get_config", lambda: _cfg(True))
    _patch_client(
        monkeypatch,
        content='{"headline": "标题", "points": ["a", "b", "c", "d", "e"]}',
    )
    _, points, source = asyncio.run(digest.compose(STATS))
    assert source == "agent"
    assert len(points) == digest.MAX_POINTS


# ---------------------------------------------------------------- 落库与接口


def _api_client():
    """复用 test_summary 的方式建一个干净的库和 TestClient。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from fastapi.testclient import TestClient

    from app import services
    from app.db import Base, engine
    from app.main import app

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    services.set_clock(lambda: datetime(2026, 10, 30, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    return TestClient(app), services


def test_api_persists_agent_source(monkeypatch):
    """模型成功时，source 要如实落成 agent，前端和测试都靠它区分。"""
    monkeypatch.setattr(digest, "get_config", lambda: _cfg(True))
    _patch_client(
        monkeypatch,
        content='{"headline": "模型写的结论", "points": ["模型写的要点"]}',
    )

    client, services = _api_client()
    with client:
        token = client.post("/api/auth/login", json={"username": "tester"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        body = client.post(
            "/api/summary/daily?date=2026-10-29", headers=headers
        ).json()

    assert body["summary"]["source"] == "agent"
    assert body["summary"]["headline"] == "模型写的结论"
    # 再读一次确认真的落库了，不是只在响应里
    with client:
        again = client.get("/api/summary/daily?date=2026-10-29", headers=headers).json()
    assert again["summary"]["source"] == "agent"
    services.set_clock(None)


def test_api_falls_back_when_model_breaks(monkeypatch):
    """模型挂了也必须 200，source 降成 rule，不能把接口搞成 500。"""
    monkeypatch.setattr(digest, "get_config", lambda: _cfg(True))
    _patch_client(monkeypatch, raises=TimeoutError("timeout"))

    client, services = _api_client()
    with client:
        token = client.post("/api/auth/login", json={"username": "tester"}).json()["token"]
        response = client.post(
            "/api/summary/daily?date=2026-10-29",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["source"] == "rule"
    assert body["summary"]["headline"]
    services.set_clock(None)
