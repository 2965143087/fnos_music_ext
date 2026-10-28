"""fnmusic-qqmusic：QQ 音乐音源服务（HTTP 外壳）。

接口形态与 ``musicbox-service`` 对齐（``/api/v1/search``、``/api/v1/song/{id}/url``、
``/api/v1/auth/*`` 等），代理层的归一化逻辑可以复用；差别只在歌曲 id 是 songmid
字符串而不是数字，因此路径参数一律按字符串处理。

音频本身不落盘、不转码：本服务只负责"搜索 + 取直链 + 歌词 + 详情"，
缓存/转码/落库仍由代理层统一处理。
"""
from __future__ import annotations

import base64
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

import login as qqlogin
from qqmusic import (
    DEFAULT_QUALITY,
    NotLoggedIn,
    QQMusicClient,
    QQMusicError,
    SessionStore,
    TrackUnavailable,
    default_data_dir,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("qqmusic.app")
try:  # 与其它服务一致：本地按天滚动日志；无 proxy/ 上下文（独立调试）时仅 stdout
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    from proxy.fnlog import setup_logging as _setup_logging

    _setup_logging("qqmusic.app", "qqmusic")
except Exception:  # noqa: BLE001
    pass

QUALITY_WHITELIST = {"exhigh", "higher", "standard", "lossless", "hires"}

STORE = SessionStore(default_data_dir())
CLIENT = QQMusicClient(STORE)
LOGIN = qqlogin.LoginManager(STORE)


@asynccontextmanager
async def lifespan(application):
    try:
        yield
    finally:
        await CLIENT.aclose()


app = FastAPI(title="fnmusic-qqmusic", version="1.0.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"ok": False, "detail": exc.errors()})


@app.exception_handler(NotLoggedIn)
async def not_logged_in_handler(request, exc: NotLoggedIn):
    return JSONResponse(status_code=401, content={"ok": False, "error": "not_logged_in", "detail": exc.message})


@app.exception_handler(TrackUnavailable)
async def track_unavailable_handler(request, exc: TrackUnavailable):
    return JSONResponse(status_code=404, content={"ok": False, "error": "track_unavailable", "detail": exc.message})


@app.exception_handler(QQMusicError)
async def qqmusic_error_handler(request, exc: QQMusicError):
    status = getattr(exc, "status", 502) or 502
    if status in (401, 404):
        status = 502
    return JSONResponse(status_code=status, content={"ok": False, "error": "upstream_error",
                                                    "detail": exc.message, "code": exc.code})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "source": "qqmusic", "logged_in": CLIENT.session.logged_in}


# ---------------- 搜索 / 详情 / 歌词 ----------------

@app.get("/api/v1/search")
async def search(
    keyword: str = Query(...),
    type: str = Query("song"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    if not keyword.strip():
        raise HTTPException(status_code=400, detail="keyword cannot be empty")
    if type != "song":
        # QQ 音乐也有专辑/歌手/歌单搜索，但代理层目前只聚合歌曲，先明确拒绝
        return {"ok": True, "code": 200, "data": [], "unsupported_type": type}
    items = await CLIENT.search(keyword, limit=limit, offset=offset)
    return {"ok": True, "code": 200, "data": items}


@app.get("/api/v1/song/{song_id}/url")
async def song_url(
    song_id: str = Path(..., min_length=1),
    quality: str = Query(DEFAULT_QUALITY),
    fresh: int = Query(0),
):
    if quality not in QUALITY_WHITELIST:
        raise HTTPException(status_code=400, detail=f"Invalid quality {quality!r}")
    item = await CLIENT.song_url(song_id, quality)
    return {"ok": True, "data": item}


@app.get("/api/v1/song/{song_id}/info")
async def song_info(song_id: str = Path(..., min_length=1)):
    data = await CLIENT.song_detail_compat(song_id)
    return {"ok": True, "data": data}


@app.get("/api/v1/songs/detail")
async def songs_detail(ids: str = Query(None)):
    if not ids or not ids.strip():
        raise HTTPException(status_code=422, detail="ids parameter is required")
    mids = [token.strip() for token in ids.split(",") if token.strip()]
    if not 1 <= len(mids) <= 100:
        raise HTTPException(status_code=422, detail="ids count must be 1..100")
    # 扁平行（song_id/album_pic_url/has_sq…）：代理搜索靠它补封面与无损判定
    return {"ok": True, "data": await CLIENT.song_detail_rows(mids)}


@app.get("/api/v1/song/{song_id}/lyric")
async def song_lyric(song_id: str = Path(..., min_length=1)):
    data = await CLIENT.lyric(song_id)
    return {"ok": True, "data": data}


# ---------------- 榜单 / 歌单 / 推荐 ----------------

@app.get("/api/v1/toplist")
async def toplist(index: int = Query(-1), limit: int = Query(60, ge=1, le=100)):
    if index < 0:
        return {"ok": True, "data": await CLIENT.toplist_catalog()}
    rows = await CLIENT.toplist(index, limit=limit)
    return {"ok": True, "data": rows, "index": index}


@app.get("/api/v1/playlist/{playlist_id}")
async def playlist(playlist_id: int = Path(..., ge=1)):
    data = await CLIENT.playlist(playlist_id)
    return {"ok": True, "data": data}


@app.get("/api/v1/user/playlists")
async def user_playlists(limit: int = Query(100, ge=1, le=200)):
    rows = await CLIENT.user_playlists(limit)
    if rows is None:
        return {"ok": False, "error": "not_logged_in", "logged_in": False}
    return {"ok": True, "data": rows, "logged_in": True}


@app.get("/api/v1/recommend/songs")
async def recommend(limit: int = Query(30, ge=10, le=60)):
    rows = await CLIENT.recommend(limit)
    return {"ok": True, "data": rows, "logged_in": CLIENT.session.logged_in}


# ---------------- 登录 ----------------

@app.get("/api/v1/auth/status")
async def auth_status(verify: int = Query(0)):
    session = CLIENT.session
    payload = {"ok": True, "data": session.public()}
    if verify and session.logged_in:
        check = await LOGIN.verify(CLIENT)
        payload["data"] = {**session.public(), "valid": check.get("valid", False)}
    return payload


@app.post("/api/v1/auth/login")
async def auth_login():
    session = await LOGIN.start()
    data = session.public()
    # 与网易盒子一致：unikey 就是轮询凭据；qr_url 给"用手机 QQ 扫码"以外的入口展示
    data["unikey"] = session.key
    data["qr_url"] = "https://y.qq.com/"
    data["image_base64"] = base64.b64encode(session.image).decode("ascii")
    data["image"] = f"data:image/png;base64,{data['image_base64']}"
    return {"ok": True, "data": data}


@app.get("/api/v1/auth/login/check")
async def auth_login_check(unikey: str = Query(...)):
    if not unikey.strip():
        raise HTTPException(status_code=400, detail="unikey cannot be empty")
    result = await LOGIN.check(unikey)
    if result.get("code") == qqlogin.CODE_SUCCESS:
        # 扫码成功后常驻实例必须重读盘，否则取链仍按旧登录态走
        await CLIENT.refresh_session()
        logger.info("qqmusic qr login success: uin=%s", CLIENT.session.uin)
    return {"ok": True, "data": result}


@app.get("/api/v1/auth/login/qr.png")
async def auth_login_qr_png(unikey: str = Query("")):
    session = LOGIN.get(unikey) if unikey else None
    if session is None:
        session = await LOGIN.start()
    return Response(content=session.image, media_type="image/png",
                    headers={"Cache-Control": "no-store", "X-QR-Key": session.key})


@app.post("/api/v1/auth/cookie")
async def auth_cookie(request: Request):
    """兜底登录：直接粘贴浏览器里 y.qq.com 的 cookie。"""
    body: Any = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    raw = ""
    if isinstance(body, dict):
        raw = str(body.get("cookie") or body.get("cookies") or "")
    if not raw:
        raw = request.query_params.get("cookie", "")
    record = await LOGIN.manual_login(raw)
    await CLIENT.refresh_session()
    return {"ok": True, "data": record.public()}


@app.post("/api/v1/auth/logout")
async def auth_logout():
    STORE.clear()
    await CLIENT.refresh_session()
    return {"ok": True, "data": CLIENT.session.public()}
