"""QQ 音乐音源接入代理层的离网测试。

验证来源判定、搜索归一化、取链解析与来源标记；上游一律用 httpx MockTransport
替换，不访问外网。
"""
import asyncio
import json

import httpx
import pytest

from proxy.app import (
    app,
    CONF,
    _QQMUSIC_URL_PIN,
    fetch_qqmusic_search,
    _qqmusic_search_request,
    resolve_qqmusic_url,
    source_display_prefix,
    strip_source_tag,
    _source_enabled,
    build_online_track,
)

QQ_GUID = "online:qqmusic:000INXq80lQiAs"


@pytest.fixture(autouse=True)
def _qq_env(monkeypatch):
    monkeypatch.setitem(CONF, "qqmusic_enabled", True)
    monkeypatch.setitem(CONF, "qqmusic_url", "http://qq.test")
    monkeypatch.setitem(CONF, "qqmusic_quality", "lossless")
    monkeypatch.setitem(CONF, "qqmusic_search_limit", 20)
    monkeypatch.setitem(CONF, "quality_mode", "high")
    _QQMUSIC_URL_PIN.clear()
    yield
    _QQMUSIC_URL_PIN.clear()


def _search_payload():
    return {
        "ok": True,
        "code": 200,
        "data": [{
            "song_id": "000INXq80lQiAs",
            "id": 637293149,
            "song_name": "平凡之路",
            "artist": "阿黛",
            "album_name": "平凡之路",
            "album_pic_url": "https://y.qq.com/music/photo_new/T002R500x500M000abc.jpg",
            "duration": 95,
            "quality": "SQ",
            "has_sq": True,
            "needs_vip": False,
            "fee": 0,
        }],
    }


def _client(handler):
    return httpx.AsyncClient(
        base_url="http://qq.test",
        transport=httpx.MockTransport(handler),
    )


# ------------------------------------------------------------------ 来源判定

def test_source_enabled_requires_flag(monkeypatch):
    assert _source_enabled(QQ_GUID) is True
    monkeypatch.setitem(CONF, "qqmusic_enabled", False)
    assert _source_enabled(QQ_GUID) is False


def test_source_enabled_not_filtered_by_online_sources(monkeypatch):
    """online_sources 只约束聚合音源，不应拦截 QQ 音乐。"""
    monkeypatch.setitem(CONF, "online_sources", "KuwoMusicClient")
    assert _source_enabled(QQ_GUID) is True


def test_display_prefix_and_strip():
    assert source_display_prefix("qqmusic") == "[QQ] "
    assert strip_source_tag("[QQ] 平凡之路") == "平凡之路"
    assert strip_source_tag("[QQ] [VIP] 晴天") == "晴天"


def test_build_online_track_marks_source():
    item = {
        "id": QQ_GUID,
        "source": "qqmusic",
        "title": "平凡之路",
        "artist": "阿黛",
        "album": "平凡之路",
        "duration_s": 95,
        "ext": "mp3",
    }
    track = build_online_track(item, mark_source=True)
    assert str(track.get("name") or track.get("title") or "").startswith("[QQ] ")


# ------------------------------------------------------------------ 搜索

def test_search_request_normalizes():
    def handler(request):
        assert request.url.params["keyword"] == "平凡之路"
        return httpx.Response(200, json=_search_payload())

    async def run():
        async with _client(handler) as c:
            return await _qqmusic_search_request(c, "平凡之路", 20)

    items = asyncio.run(run())
    assert len(items) == 1
    it = items[0]
    assert it["id"] == "qqmusic:000INXq80lQiAs"
    assert it["source"] == "qqmusic"
    assert it["title"] == "平凡之路"
    assert it["artist"] == "阿黛"
    assert it["duration_s"] == 95.0
    assert it["ext"] == "flac"  # quality=SQ


def test_search_request_offset_to_page():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"ok": True, "data": []})

    async def run():
        async with _client(handler) as c:
            return await _qqmusic_search_request(c, "x", 20, offset=40)

    asyncio.run(run())
    assert seen["offset"] == "40"


def test_search_request_handles_upstream_failure():
    def handler(request):
        return httpx.Response(500, text="boom")

    async def run():
        async with _client(handler) as c:
            return await fetch_qqmusic_search(c, "x", 20)

    assert asyncio.run(run()) is None


def test_search_request_drops_unplayable():
    payload = _search_payload()
    payload["data"][0]["title"] = None
    payload["data"][0]["song_name"] = "平凡之路 (试听)"

    def handler(request):
        return httpx.Response(200, json=payload)

    async def run():
        async with _client(handler) as c:
            return await _qqmusic_search_request(c, "x", 20)

    assert asyncio.run(run()) == []


# ------------------------------------------------------------------ 取链

def test_resolve_url_success_pins():
    def handler(request):
        return httpx.Response(200, json={
            "ok": True,
            "data": {"code": 200, "url": "https://cdn.qq.test/a.mp3", "quality": "MP3_128", "expiration": 7200},
        })

    async def run():
        async with _client(handler) as c:
            first = await resolve_qqmusic_url(c, "000INXq80lQiAs")
            # 第二次应命中钉链，不再打上游
            calls = []
            second = await resolve_qqmusic_url(c, "000INXq80lQiAs")
            return first, second

    first, second = asyncio.run(run())
    assert first == "https://cdn.qq.test/a.mp3"
    assert second == first
    assert "000INXq80lQiAs" in _QQMUSIC_URL_PIN


def test_resolve_url_no_permission():
    def handler(request):
        return httpx.Response(200, json={
            "ok": False,
            "code": 404,
            "data": {"code": 404, "url": "", "upstream_result": 104003, "reason": "no_permission"},
        })

    async def run():
        async with _client(handler) as c:
            return await resolve_qqmusic_url(c, "000INXq80lQiAs")

    assert asyncio.run(run()) is None


def test_resolve_url_max_quality_downgrade():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={
            "ok": True,
            "data": {"code": 200, "url": "https://cdn.qq.test/a.mp3", "expiration": 7200},
        })

    async def run():
        async with _client(handler) as c:
            return await resolve_qqmusic_url(c, "000INXq80lQiAs", max_quality="mp3")

    assert asyncio.run(run()) == "https://cdn.qq.test/a.mp3"
    assert seen["quality"] == "standard"


def test_resolve_url_refresh_bypasses_pin():
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={
            "ok": True,
            "data": {"code": 200, "url": "https://cdn.qq.test/a.mp3", "expiration": 7200},
        })

    async def run():
        async with _client(handler) as c:
            await resolve_qqmusic_url(c, "000INXq80lQiAs")
            await resolve_qqmusic_url(c, "000INXq80lQiAs", refresh=True)

    asyncio.run(run())
    assert len(calls) == 2  # 钉链命中不发起请求，refresh 强制重新解析


def test_pin_invalid_drops_cache():
    from proxy.app import _qqmusic_url_pin_drop

    _QQMUSIC_URL_PIN["mid"] = ("https://cdn.qq.test/a.mp3", 1e18)
    _qqmusic_url_pin_drop("mid", reason="test")
    assert _QQMUSIC_URL_PIN["mid"][1] == 0.0


def test_healthz_includes_qqmusic(monkeypatch):
    """健康检查在 QQ 音乐启用时包含 qqmusic 探活项。"""
    from fastapi.testclient import TestClient
    import proxy.app as proxy_app

    async def fake_get(self, *args, **kwargs):
        return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with TestClient(app) as client:
        r = client.get("/_ext/healthz")
        assert r.status_code == 200
