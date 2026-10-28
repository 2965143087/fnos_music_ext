"""QQ 音乐音源接入测试（代理侧）。

不联网：QQ 音源服务用 httpx.MockTransport 假实现，验证
搜索归一化 / 取链钉链 / 详情歌词 / 来源标记 四条链路。
"""
from __future__ import annotations

import httpx
import pytest

from proxy import app as pa
from proxy.app import (
    CONF,
    _qq_pin_url,
    _QQ_URL_PIN,
    _SEARCH_CACHE,
    fetch_qq_search,
    musicbox_like_client,
    resolve_online_lyric,
    resolve_qq_url,
    source_display_prefix,
    strip_source_tag,
    _source_enabled,
    _online_info,
)


@pytest.fixture(autouse=True)
def setup_qq_env(tmp_path, monkeypatch):
    _SEARCH_CACHE.clear()
    _QQ_URL_PIN.clear()
    monkeypatch.setitem(CONF, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONF, "qq_enabled", True)
    monkeypatch.setitem(CONF, "qq_search_limit", 20)
    monkeypatch.setitem(CONF, "qq_quality", "lossless")
    monkeypatch.setitem(CONF, "search_debounce_s", 0.0)
    monkeypatch.setitem(CONF, "search_cache_ttl", 300.0)
    yield
    _QQ_URL_PIN.clear()


def anyio_backend():
    return "asyncio"


def qq_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://qq", transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- 来源与开关

def test_source_prefix_and_strip():
    assert source_display_prefix("qqmusic") == "[QQ] "
    assert strip_source_tag("[QQ] 晴天") == "晴天"


def test_source_enabled_follows_switch(monkeypatch):
    monkeypatch.setitem(CONF, "qq_enabled", True)
    assert _source_enabled("online:qqmusic:0039MnYb0qxYhV") is True
    monkeypatch.setitem(CONF, "qq_enabled", False)
    assert _source_enabled("online:qqmusic:0039MnYb0qxYhV") is False


# ---------------------------------------------------------------- 搜索

@pytest.mark.anyio
async def test_fetch_qq_search_maps_items():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        if request.url.path == "/api/v1/search":
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "0027oMO61wWi55", "song_name": "发如雪", "artist": "周杰伦",
                 "album_name": "十一月的萧邦", "duration": 299, "quality": "SQ",
                 "album_pic_url": "https://y.qq.com/music/photo_new/a.jpg"},
                {"song_id": "", "song_name": "无 id 的行", "artist": "x"},
            ]})
        if request.url.path == "/api/v1/songs/detail":
            # 音源服务的批量详情是扁平行（对齐 musicbox），代理用它补封面与无损判定
            return httpx.Response(200, json={"ok": True, "data": [
                {"song_id": "0027oMO61wWi55", "song_name": "发如雪", "artist": "周杰伦",
                 "album_name": "十一月的萧邦", "album_pic_url": "https://y.qq.com/music/photo_new/b.jpg",
                 "duration": 299, "has_sq": True, "has_hr": False},
            ]})
        return httpx.Response(404, json={"ok": False})

    async with qq_client(handler) as client:
        items = await fetch_qq_search(client, "发如雪", 20)

    assert seen["path"] in ("/api/v1/search", "/api/v1/songs/detail")
    assert items and len(items) == 1  # 无 id 的行被丢弃
    item = items[0]
    assert item["id"] == "qqmusic:0027oMO61wWi55"
    assert item["source"] == "qqmusic"
    assert item["title"] == "发如雪"
    assert item["artist"] == "周杰伦"
    assert item["ext"] == "flac"
    assert item["duration_s"] == 299.0
    # 详情返回的封面覆盖搜索行的封面（音质/封面以详情为准）
    assert item["cover_url"].endswith("b.jpg")


@pytest.mark.anyio
async def test_fetch_qq_search_handles_error():
    async with qq_client(lambda request: httpx.Response(500, text="boom")) as client:
        assert await fetch_qq_search(client, "x", 20) is None


# ---------------------------------------------------------------- 取链

@pytest.mark.anyio
async def test_resolve_qq_url_pins_result():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True, "data": {
            "code": 200, "url": "https://dl.stream.qqmusic.qq.com/x.flac",
            "type": "flac", "expi": 600,
        }})

    async with qq_client(handler) as client:
        first = await resolve_qq_url(client, "0027oMO61wWi55")
        second = await resolve_qq_url(client, "0027oMO61wWi55")

    assert first == second == "https://dl.stream.qqmusic.qq.com/x.flac"
    assert len(calls) == 1  # 第二次命中钉链，不再请求音源
    assert "quality=lossless" in calls[0]
    assert _qq_pin_url("0027oMO61wWi55", "lossless") is not None


@pytest.mark.anyio
async def test_resolve_qq_url_returns_none_when_not_logged_in():
    async with qq_client(lambda request: httpx.Response(401, json={"ok": False, "error": "not_logged_in"})) as client:
        assert await resolve_qq_url(client, "0027oMO61wWi55") is None
    # 未登录不钉负缓存：登录后立即可播
    assert _qq_pin_url("0027oMO61wWi55", "lossless") is None


@pytest.mark.anyio
async def test_resolve_qq_url_rejects_non_http():
    async with qq_client(lambda request: httpx.Response(200, json={"ok": True, "data": {"url": "javascript:x"}})) as client:
        assert await resolve_qq_url(client, "m1") is None


# ---------------------------------------------------------------- 详情 / 歌词

@pytest.mark.anyio
async def test_qq_online_info_and_lyric(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json={"ok": True, "data": {
                "id": "0027oMO61wWi55", "name": "发如雪",
                "ar": [{"name": "周杰伦"}],
                "al": {"name": "十一月的萧邦", "picUrl": "https://y.qq.com/music/photo_new/b.jpg"},
                "dt": 299000, "sq": {"size": 36226753}, "h": {"size": 1000},
            }})
        if request.url.path.endswith("/lyric"):
            return httpx.Response(200, json={"ok": True, "data": {"lyric": "[00:01.00]发如雪"}})
        return httpx.Response(404, json={"ok": False})

    client = qq_client(handler)
    monkeypatch.setattr(pa, "get_qq_client", lambda _app: client)
    monkeypatch.setattr(pa, "musicbox_like_client", lambda _app, src: client)

    class _Req:
        app = object()

    req = _Req()
    guid = "online:qqmusic:0027oMO61wWi55"
    info = await _online_info(req, guid)
    assert info["source"] == "qqmusic"
    assert info["id"] == "qqmusic:0027oMO61wWi55"
    assert info["title"] == "发如雪"
    assert info["artist"] == "周杰伦"
    assert info["cover_url"].endswith("b.jpg")
    assert info["ext"] == "flac"
    assert info["duration_s"] == 299.0

    lyric = await resolve_online_lyric(req, guid)
    assert lyric.startswith("[00:01.00]发如雪")
    await client.aclose()


def test_musicbox_like_client_routes_by_source(monkeypatch):
    sentinel_qq = object()
    sentinel_nm = object()
    monkeypatch.setattr(pa, "get_qq_client", lambda _app: sentinel_qq)
    monkeypatch.setattr(pa, "get_musicbox_client", lambda _app: sentinel_nm)
    assert musicbox_like_client(object(), "qqmusic") is sentinel_qq
    assert musicbox_like_client(object(), "netease") is sentinel_nm
