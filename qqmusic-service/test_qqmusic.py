"""qqmusic-service 单元测试：全部走 httpx.MockTransport，不发真实请求。

覆盖点：
- 扫码登录算法（hash33 / ptuiCB 解析 / cookie 解析）与状态码映射
- 搜索条目归一化（音质判定、VIP 标记、封面直链）
- 取链音质阶梯回退与"未登录不交付试听片段"
- 登录态落盘/读取与 cookie 头拼装
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import login as qqlogin
from qqmusic import (
    NotLoggedIn,
    QQMusicClient,
    QQMusicError,
    Session,
    SessionStore,
    TrackUnavailable,
    singer_names,
    song_cover,
)


# ---------------------------------------------------------------- 工具

def run(coro):
    return asyncio.run(coro)


def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "data")


def search_payload(rows: list[dict]) -> dict:
    return {"code": 0, "data": {"song": {"list": rows, "curnum": len(rows), "totalnum": len(rows)}}}


def song_row(mid: str = "0039MnYb0qxYhV", **over) -> dict:
    row = {
        "mid": mid,
        "name": "晴天",
        "title": "晴天",
        "singer": [{"name": "周杰伦"}, {"name": "某合唱"}],
        "album": {"mid": "000MkMni19ClKG", "name": "叶惠美"},
        "interval": 269,
        "pay": {"pay_play": 1},
        "file": {
            "size_flac": 55397039,
            "size_320mp3": 10792943,
            "size_128mp3": 4317292,
            "media_mid": "003Qui1q2u1Zho",
        },
        "subtitle": "",
    }
    row.update(over)
    return row


# ---------------------------------------------------------------- 纯函数

def test_hash33_matches_official_sample():
    # 与官方 ptqrtoken 算法一致：32 位回绕后取低 31 位
    assert qqlogin.hash33("") == 0
    assert qqlogin.hash33("a") == 97
    token = qqlogin.hash33("QFIdmRZQxhsCJcWCBhTsNCBDJBLCJEIF")
    assert 0 <= token <= 0x7FFFFFFF
    # 同输入必须稳定（同一二维码可反复轮询）
    assert token == qqlogin.hash33("QFIdmRZQxhsCJcWCBhTsNCBDJBLCJEIF")


def test_parse_ptui_extracts_fields():
    text = "ptuiCB('67','0','','0','二维码认证中。', '')"
    fields = qqlogin._parse_ptui(text)
    assert fields[0] == "67"
    assert fields[4] == "二维码认证中。"


def test_parse_cookie_string_and_digits():
    cookies = qqlogin.parse_cookie_string("uin=o0123456789; qm_keyst=Q_H_L_abc ; p_skey=xyz")
    assert cookies["uin"] == "o0123456789"
    assert cookies["qm_keyst"] == "Q_H_L_abc"
    assert cookies["p_skey"] == "xyz"
    assert qqlogin._digits("o0123456789") == "123456789"
    assert qqlogin._digits("00123456") == "00123456"


def test_singer_names_and_cover():
    assert singer_names([{"name": "A"}, {"name": "B"}]) == "A / B"
    assert singer_names("C") == "C"
    assert singer_names(None) == ""
    assert song_cover("000MkMni19ClKG").endswith("T002R300x300M000000MkMni19ClKG.jpg")
    assert song_cover("") == ""


# ---------------------------------------------------------------- 登录态

def test_session_store_roundtrip_and_cookie_header(tmp_path):
    st = store(tmp_path)
    record = Session(uin="123456789", musicid="123456789", musickey="KEY", nick="阿杰",
                     cookies={"p_skey": "ps", "segment": "s"})
    st.save(record)
    loaded = st.load()
    assert loaded.uin == "123456789"
    assert loaded.musickey == "KEY"
    assert loaded.logged_in is True
    header = loaded.cookie_header()
    assert "uin=o0123456789" in header
    assert "qm_keyst=KEY" in header
    assert "qqmusic_key=KEY" in header
    assert "p_skey=ps" in header
    # guid 落盘后必须稳定（取链风控依赖它）
    assert loaded.guid == st.load().guid

    public = loaded.public()
    assert "musickey" not in public and public["nick"] == "阿杰"

    cleared = st.clear()
    assert cleared.logged_in is False
    assert st.load().logged_in is False


def test_login_manager_check_without_session(tmp_path):
    async def case():
        manager = qqlogin.LoginManager(store(tmp_path))
        return await manager.check("not-exist")

    result = run(case())
    assert result["code"] == qqlogin.CODE_EXPIRED


# ---------------------------------------------------------------- 搜索

def test_search_normalizes_row(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=search_payload([song_row()]))

    async def case():
        client = QQMusicClient(store(tmp_path), transport=httpx.MockTransport(handler))
        rows = await client.search("晴天", limit=5, offset=10)
        await client.aclose()
        return rows

    rows = run(case())
    assert captured["params"]["w"] == "晴天"
    assert captured["params"]["n"] == "5"
    assert captured["params"]["p"] == "3"  # offset 10 / limit 5 + 1
    assert captured["params"]["new_json"] == "1"
    assert rows[0]["song_id"] == "0039MnYb0qxYhV"
    assert rows[0]["artist"] == "周杰伦 / 某合唱"
    assert rows[0]["album_pic_url"].endswith("000MkMni19ClKG.jpg")
    assert rows[0]["duration"] == 269
    assert rows[0]["quality"] == "SQ" and rows[0]["ext"] == "flac"
    assert rows[0]["file_size"] == 55397039
    assert rows[0]["fee"] == 1


def test_search_quality_fallback_without_flac(tmp_path):
    row = song_row(file={"size_320mp3": 10792943, "size_128mp3": 100, "media_mid": "m"}, pay={})

    async def case():
        client = QQMusicClient(store(tmp_path), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=search_payload([row]))))
        rows = await client.search("x")
        await client.aclose()
        return rows

    rows = run(case())
    assert rows[0]["quality"] == "320"
    assert rows[0]["ext"] == "mp3"
    assert rows[0]["fee"] == 0


def test_search_http_error_raises(tmp_path):
    async def case():
        client = QQMusicClient(store(tmp_path), transport=httpx.MockTransport(
            lambda request: httpx.Response(502, text="bad gateway")))
        with pytest.raises(QQMusicError):
            await client.search("x")
        await client.aclose()

    run(case())


# ---------------------------------------------------------------- 取链

def test_song_url_requires_login(tmp_path):
    async def case():
        client = QQMusicClient(store(tmp_path))
        with pytest.raises(NotLoggedIn):
            await client.song_url("0039MnYb0qxYhV", "lossless")
        await client.aclose()

    run(case())


def test_song_url_falls_back_through_ladder(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        filename = body["req_0"]["param"]["filename"][0]
        calls.append(filename)
        purl = "" if filename.startswith("F000") else f"M800/{filename}?vkey=k"
        return httpx.Response(200, json={"req_0": {"code": 0, "data": {
            "sip": ["http://dl.stream.qqmusic.qq.com/"],
            "midurlinfo": [{"filename": filename, "purl": purl}],
        }}})

    async def case():
        st = store(tmp_path)
        st.save(Session(uin="1", musickey="K"))
        client = QQMusicClient(st, transport=httpx.MockTransport(handler))
        item = await client.song_url("0039MnYb0qxYhV", "lossless")
        await client.aclose()
        return item

    item = run(case())
    assert calls == ["F0000039MnYb0qxYhV.flac", "M8000039MnYb0qxYhV.mp3"]
    assert item["url"] == "http://dl.stream.qqmusic.qq.com/M800/M8000039MnYb0qxYhV.mp3?vkey=k"
    assert item["type"] == "mp3"
    assert item["code"] == 200


def test_song_url_vip_track_raises_unavailable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"req_0": {"code": 0, "data": {
            "sip": ["http://dl.stream.qqmusic.qq.com/"],
            "midurlinfo": [{"filename": "", "purl": ""}],
        }}})

    async def case():
        st = store(tmp_path)
        st.save(Session(uin="1", musickey="K"))
        client = QQMusicClient(st, transport=httpx.MockTransport(handler))
        with pytest.raises(TrackUnavailable):
            await client.song_url("0039MnYb0qxYhV", "lossless")
        await client.aclose()

    run(case())


# ---------------------------------------------------------------- 详情 / 歌词

def test_song_detail_compat_shape(tmp_path):
    payload = {"req_0": {"code": 0, "data": {"track_info": {
        "mid": "0039MnYb0qxYhV",
        "name": "晴天",
        "singer": [{"name": "周杰伦"}],
        "album": {"mid": "000MkMni19ClKG", "name": "叶惠美"},
        "interval": 269,
        "pay": {"pay_play": 0},
        "file": {"size_flac": 0, "size_320mp3": 10792943, "size_128mp3": 100},
    }}}}

    async def case():
        client = QQMusicClient(store(tmp_path), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)))
        info = await client.song_detail_compat("0039MnYb0qxYhV")
        await client.aclose()
        return info

    info = run(case())
    assert info["name"] == "晴天"
    assert info["ar"] == [{"name": "周杰伦"}]
    assert info["al"]["picUrl"].endswith("000MkMni19ClKG.jpg")
    assert info["dt"] == 269000
    assert info["sq"] is None
    assert info["h"]["size"] == 10792943
    assert info["fee"] == 0


def test_song_detail_missing_track_raises(tmp_path):
    async def case():
        client = QQMusicClient(store(tmp_path), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"req_0": {"code": 0, "data": {}}})))
        with pytest.raises(TrackUnavailable):
            await client.song_detail("nope")
        await client.aclose()

    run(case())


def test_lyric_returns_text_and_translation(tmp_path):
    body = {"retcode": 0, "lyric": "[00:01.00]晴天", "trans": "translated"}

    async def case():
        client = QQMusicClient(store(tmp_path), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body)))
        data = await client.lyric("0039MnYb0qxYhV")
        await client.aclose()
        return data

    data = run(case())
    assert data["lyric"] == "[00:01.00]晴天"
    assert data["tlyric"] == "translated"


# ---------------------------------------------------------------- 歌单 / 榜单

def test_toplist_catalog_and_detail(tmp_path):
    # 真实结构：曲目在 req_0.data.songInfoList（block 级），与搜索结果同构
    payload = {"req_0": {"code": 0, "data": {
        "data": {"title": "热歌榜"},
        "songInfoList": [{
            "mid": "0039MnYb0qxYhV",
            "name": "晴天",
            "singer": [{"name": "周杰伦"}],
            "album": {"mid": "000MkMni19ClKG", "name": "叶惠美"},
            "interval": 269,
            "pay": {"pay_play": 0},
            "file": {"size_flac": 100, "size_320mp3": 50},
        }],
    }}}

    async def case():
        client = QQMusicClient(store(tmp_path), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)))
        catalog = await client.toplist_catalog()
        rows = await client.toplist(26, limit=10)
        await client.aclose()
        return catalog, rows

    catalog, rows = run(case())
    assert any(item["id"] == 26 for item in catalog)
    assert rows[0]["song_id"] == "0039MnYb0qxYhV"
    assert rows[0]["artist"] == "周杰伦"
    assert rows[0]["ext"] == "flac"
    assert rows[0]["duration"] == 269


def test_playlist_uses_same_track_shape(tmp_path):
    payload = {"req_0": {"code": 0, "data": {
        "dirinfo": {"title": "我的歌单", "desc": "描述", "headurl": "https://y.qq.com/cover.jpg",
                    "creator": {"nick": "阿杰"}},
        "songlist": [{
            "mid": "001zSAWj3lw9zp",
            "name": "山海不说话",
            "singer": [{"name": "某歌手"}],
            "album": {"mid": "000MkMni19ClKG", "name": "专辑"},
            "interval": 210,
            "pay": {"pay_play": 1},
            "file": {"size_320mp3": 123, "size_128mp3": 45},
        }],
    }}}

    async def case():
        client = QQMusicClient(store(tmp_path), transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)))
        data = await client.playlist(7011264344)
        await client.aclose()
        return data

    data = run(case())
    assert data["name"] == "我的歌单"
    assert data["creator"] == "阿杰"
    assert data["tracks"][0]["song_id"] == "001zSAWj3lw9zp"
    assert data["tracks"][0]["quality"] == "320"
    assert data["tracks"][0]["fee"] == 1


def test_batch_song_details_single_request(tmp_path):
    """批量详情必须合并成一个 musicu 请求（req_0..req_N），否则搜索预算被吃掉。"""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        requests.append(body)
        modules = {k: v for k, v in body.items() if k.startswith("req_")}
        return httpx.Response(200, json={
            key: {"code": 0, "data": {"track_info": {
                "mid": mod["param"]["song_mid"],
                "name": f"歌-{mod['param']['song_mid']}",
                "singer": [{"name": "A"}],
                "album": {"mid": "000MkMni19ClKG", "name": "专辑"},
                "interval": 200,
                "file": {"size_flac": 10, "size_hires": 20},
            }}}
            for key, mod in modules.items()
        })

    async def case():
        client = QQMusicClient(store(tmp_path), transport=httpx.MockTransport(handler))
        rows = await client.song_detail_rows(["m1", "m2", "m3"])
        await client.aclose()
        return rows

    rows = run(case())
    assert len(requests) == 1  # 3 首只发一次
    assert len([k for k in requests[0] if k.startswith("req_")]) == 3
    assert [r["song_id"] for r in rows] == ["m1", "m2", "m3"]
    assert rows[0]["has_sq"] is True and rows[0]["has_hr"] is True
