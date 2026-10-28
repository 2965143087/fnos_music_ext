"""QQ 音乐扫码登录。

流程与 y.qq.com 网页版一致，全程纯 HTTP（不需要浏览器、不保存密码）：

1. ``xlogin`` 拿 ``login_sig``（没有也能扫码，但带上可显著降低风控概率）；
2. ``ptqrshow`` 生成二维码并下发 ``qrsig`` cookie，``ptqrtoken = hash33(qrsig)``；
3. ``ptqrlogin`` 轮询扫码状态：66 未扫、67 已扫待确认、65 已过期、0 登录成功；
4. 成功后跟着 ``check_sig`` 跳转走完 cookie 握手，从 cookie 里取 ``qm_keyst``；
5. 少数情况下跳转链只给 ``p_skey``（未落 ``qm_keyst``），再用 ``p_skey`` 调
   ``music.login.LoginServer/Login`` 换 ``musickey``。

拿不到 ``qm_keyst`` 时上层还能用「手动粘贴 cookie」兜底，避免官方改版导致完全不可用。
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from qqmusic import FCG_URL, REFERER, UA, QQMusicClient, QQMusicError, Session, SessionStore

XLOGIN_URL = "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
QR_SHOW_URL = "https://ssl.ptlogin2.qq.com/ptqrshow"
QR_LOGIN_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin"

APPID = "716027609"
DAID = "383"
PT_3RD_AID = "100497308"  # QQ 音乐在 QQ 互联的 appid
LOGIN_JUMP = "https://graph.qq.com/oauth2.0/login_jump"

QR_TTL_S = 180.0          # 官方二维码约 2 分钟失效，给一点余量
MAX_SESSIONS = 8

# 与网易盒子一致的扫码状态码，前端/代理层可以共用一套轮询逻辑
CODE_EXPIRED = 800
CODE_WAITING = 801
CODE_SCANNED = 802
CODE_SUCCESS = 803

_PTUI_RE = re.compile(r"ptuiCB\((.*)\)", re.S)


class LoginError(QQMusicError):
    status = 502


def hash33(text: str) -> int:
    """ptqrtoken 算法：e += (e << 5) + ord(c)，按 32 位回绕后取低 31 位。"""
    value = 0
    for ch in text:
        value = ((value << 5) + value + ord(ch)) & 0xFFFFFFFF
    return value & 0x7FFFFFFF


def _parse_ptui(text: str) -> list[str]:
    match = _PTUI_RE.search(text or "")
    if not match:
        return []
    body = match.group(1)
    items = re.findall(r"'((?:[^'\\]|\\.)*)'", body)
    return [item.replace("\\/", "/").replace("\\'", "'") for item in items]


def _digits(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("o0"):
        text = text[2:]
    digits = re.sub(r"\D", "", text)
    return digits


def parse_cookie_string(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in str(raw or "").split(";"):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if not name:
            continue
        cookies[name] = value.strip()
    return cookies


@dataclass
class QRSession:
    key: str
    qrsig: str
    ptqrtoken: int
    image: bytes
    client: httpx.AsyncClient
    created: float = field(default_factory=time.time)
    status: int = CODE_WAITING
    message: str = ""

    @property
    def expired(self) -> bool:
        return time.time() - self.created > QR_TTL_S

    def public(self) -> dict:
        return {
            "key": self.key,
            "qrsig": self.qrsig,
            "ptqrtoken": self.ptqrtoken,
            "expires_in": max(0, int(QR_TTL_S - (time.time() - self.created))),
            "status": self.status,
            "message": self.message,
        }


class LoginManager:
    """进程内二维码会话表：一个 key 对应一次扫码，轮询到成功即销毁。"""

    def __init__(self, store: SessionStore, *, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self.transport = transport
        self._sessions: dict[str, QRSession] = {}
        self._lock = asyncio.Lock()

    # ---------- 二维码 ----------

    async def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=8.0),
            follow_redirects=True,
            transport=self.transport,
            headers={
                "User-Agent": UA,
                "Referer": "https://y.qq.com/",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )

    async def start(self) -> QRSession:
        self._gc()
        client = await self._new_client()
        try:
            # login_sig：xlogin 会下发，带上后扫码成功率更稳；失败不阻塞流程
            try:
                await client.get(XLOGIN_URL, params={
                    "appid": APPID,
                    "daid": DAID,
                    "style": "33",
                    "hide_title_bar": "1",
                    "low_login": "0",
                    "qlogin_auto_login": "1",
                    "no_verifyimg": "1",
                    "link_target": "blank",
                    "s_url": "https://y.qq.com/portal/profile.html",
                    "pt_3rd_aid": PT_3RD_AID,
                })
            except httpx.HTTPError:
                pass

            resp = await client.get(QR_SHOW_URL, params={
                "appid": APPID,
                "e": "2",
                "l": "M",
                "s": "3",
                "d": "72",
                "v": "4",
                "t": f"{time.time():.6f}",
                "daid": DAID,
                "pt_3rd_aid": PT_3RD_AID,
                "u1": LOGIN_JUMP,
            })
        except httpx.HTTPError as exc:
            await client.aclose()
            raise LoginError(f"获取二维码失败：{type(exc).__name__}") from exc

        qrsig = ""
        for cookie in client.cookies.jar:
            if cookie.name == "qrsig":
                qrsig = cookie.value
                break
        if not qrsig:
            await client.aclose()
            raise LoginError("官方未下发 qrsig，可能被风控拦截")
        if resp.status_code != 200 or not resp.content:
            await client.aclose()
            raise LoginError(f"二维码接口异常：HTTP {resp.status_code}")

        key = uuid.uuid4().hex
        session = QRSession(key=key, qrsig=qrsig, ptqrtoken=hash33(qrsig), image=resp.content, client=client)
        async with self._lock:
            self._sessions[key] = session
        return session

    def get(self, key: str) -> QRSession | None:
        session = self._sessions.get(str(key or ""))
        if session is None:
            return None
        if session.expired and session.status != CODE_SUCCESS:
            session.status = CODE_EXPIRED
            session.message = "二维码已过期"
        return session

    def _gc(self) -> None:
        now = time.time()
        stale = [k for k, s in self._sessions.items()
                 if s.status == CODE_SUCCESS or now - s.created > QR_TTL_S + 60]
        # 表满时按创建时间淘汰最老的，避免长时间无人轮询导致的内存堆积
        if len(self._sessions) >= MAX_SESSIONS:
            ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].created)
            stale.extend(k for k, _ in ordered[: max(1, len(ordered) - MAX_SESSIONS + 1)])
        for key in dict.fromkeys(stale):
            session = self._sessions.pop(key, None)
            if session is not None:
                asyncio.create_task(session.client.aclose())

    async def discard(self, key: str) -> None:
        async with self._lock:
            session = self._sessions.pop(str(key or ""), None)
        if session is not None:
            await session.client.aclose()

    # ---------- 轮询 ----------

    async def check(self, key: str) -> dict:
        session = self.get(key)
        if session is None:
            return {"code": CODE_EXPIRED, "message": "二维码会话不存在或已过期，请重新获取"}
        if session.expired and session.status != CODE_SUCCESS:
            return {"code": CODE_EXPIRED, "message": "二维码已过期"}
        if session.status == CODE_SUCCESS:
            return {"code": CODE_SUCCESS, "message": session.message, "session": self.store.load().public()}

        try:
            resp = await session.client.get(QR_LOGIN_URL, params={
                "u1": LOGIN_JUMP,
                "ptqrtoken": str(session.ptqrtoken),
                "ptredirect": "0",
                "h": "1",
                "t": "1",
                "g": "1",
                "from_ui": "1",
                "ptlang": "2052",
                "action": f"0-0-{int(time.time() * 1000)}",
                "js_ver": "20111021",
                "js_type": "1",
                "login_sig": session.client.cookies.get("login_sig") or "",
                "pt_uistyle": "40",
                "aid": APPID,
                "daid": DAID,
                "pt_3rd_aid": PT_3RD_AID,
                "has_onekey": "1",
            })
            fields = _parse_ptui(resp.text)
        except httpx.HTTPError as exc:
            return {"code": session.status, "message": f"轮询失败：{type(exc).__name__}"}

        if len(fields) < 5:
            return {"code": session.status, "message": session.message or "未知扫码状态"}

        code, _ret, url, _style, message = fields[0], fields[1], fields[2], fields[3], fields[4]
        if code == "66":
            session.status = CODE_WAITING
            session.message = message or "等待扫码"
            return {"code": CODE_WAITING, "message": session.message}
        if code == "67":
            session.status = CODE_SCANNED
            session.message = message or "已扫码，请在手机上确认"
            return {"code": CODE_SCANNED, "message": session.message}
        if code == "65":
            session.status = CODE_EXPIRED
            session.message = message or "二维码已过期"
            return {"code": CODE_EXPIRED, "message": session.message}
        if code != "0":
            session.status = CODE_EXPIRED
            session.message = message or f"登录失败（{code}）"
            return {"code": CODE_EXPIRED, "message": session.message}

        # 扫码成功：走完 cookie 握手
        try:
            logged = await self._finish_login(session, url)
        except QQMusicError as exc:
            session.message = exc.message
            return {"code": CODE_EXPIRED, "message": exc.message}

        session.status = CODE_SUCCESS
        session.message = logged.nick or "登录成功"
        await self.discard(session.key)
        return {"code": CODE_SUCCESS, "message": session.message, "session": logged.public()}

    async def _finish_login(self, session: QRSession, redirect_url: str) -> Session:
        """跟着 check_sig / login_jump 走完跳转，从 cookie 里提取音乐登录态。"""
        cookies: dict[str, str] = {}

        def collect(client: httpx.AsyncClient, response: httpx.Response | None = None) -> None:
            for cookie in client.cookies.jar:
                if cookie.value:
                    cookies[cookie.name] = cookie.value
            if response is not None:
                for name, value in response.cookies.items():
                    if value:
                        cookies[name] = value

        collect(session.client)
        if redirect_url.startswith("http"):
            try:
                follow = await session.client.get(redirect_url)
                collect(session.client, follow)
                # graph.qq.com 的 login_jump 之后还有一跳才落 y.qq.com cookie
                location = follow.headers.get("location") or ""
                hops = 0
                while location and hops < 3:
                    nxt = location if location.startswith("http") else str(follow.url.join(location))
                    follow = await session.client.get(nxt)
                    collect(session.client, follow)
                    location = follow.headers.get("location") or ""
                    hops += 1
            except httpx.HTTPError:
                pass

        uin = _digits(cookies.get("pt2gguin") or cookies.get("uin") or cookies.get("p_uin") or "")
        key = cookies.get("qm_keyst") or cookies.get("qqmusic_key") or cookies.get("music_key") or ""

        if not key:
            key, extra = await self._exchange_by_pskey(session.client, uin, cookies)
            if extra:
                cookies.update(extra)
                uin = _digits(cookies.get("uin") or uin)

        if not key or not uin:
            raise LoginError(
                "扫码已确认，但未取到 QQ 音乐登录态（官方跳转链未下发 qm_keyst）。"
                "可改用「粘贴 Cookie」方式登录。"
            )

        record = self.store.load()
        record.uin = uin
        record.musicid = str(cookies.get("musicid") or uin)
        record.musickey = key
        record.refresh_key = str(cookies.get("refresh_key") or "")
        record.access_token = str(cookies.get("access_token") or "")
        record.openid = str(cookies.get("openid") or cookies.get("psrf_qqopenid") or "")
        record.nick = str(cookies.get("qq_nick") or cookies.get("nick") or "")
        record.login_type = 2
        record.cookies = {k: v for k, v in cookies.items()
                          if k not in ("qm_keyst", "qqmusic_key", "uin", "pt2gguin")}
        self.store.save(record)
        return record

    async def _exchange_by_pskey(
        self, client: httpx.AsyncClient, uin: str, cookies: dict[str, str]
    ) -> tuple[str, dict[str, str]]:
        """跳转链只给 p_skey 时，用它换 musickey。失败返回空串（由调用方兜底提示）。"""
        p_skey = cookies.get("p_skey") or cookies.get("skey") or ""
        if not p_skey or not uin:
            return "", {}
        payload = {
            "comm": {"ct": 24, "cv": 0, "v": 0, "format": "json", "uin": int(uin)},
            "req_0": {
                "module": "music.login.LoginServer",
                "method": "Login",
                "param": {
                    "uin": uin,
                    "p_skey": p_skey,
                    "pt_fp": "",
                    "g_tk": "5381",
                    "tmeLoginType": 2,
                },
            },
        }
        try:
            resp = await client.post(FCG_URL, json=payload, headers={"Referer": REFERER})
        except httpx.HTTPError:
            return "", {}
        extra: dict[str, str] = {}
        for name, value in resp.cookies.items():
            if value:
                extra[name] = value
        try:
            data = resp.json()
        except ValueError:
            return extra.get("qm_keyst") or extra.get("qqmusic_key") or "", extra
        block = data.get("req_0") if isinstance(data, dict) else None
        inner = block.get("data") if isinstance(block, dict) and isinstance(block.get("data"), dict) else {}
        key = str(inner.get("musickey") or "")
        for name in ("musicid", "refresh_key", "access_token", "openid", "nick", "logo"):
            value = inner.get(name)
            if value:
                extra[name] = str(value)
        return key, extra

    # ---------- 手动 cookie ----------

    async def manual_login(self, raw_cookie: str) -> Session:
        """粘贴浏览器 cookie 登录（官方扫码链路改版时的可靠兜底）。"""
        cookies = parse_cookie_string(raw_cookie)
        if not cookies:
            raise LoginError("cookie 为空或格式不对（应为 k=v; k2=v2）")
        uin = _digits(cookies.get("uin") or cookies.get("pt2gguin") or cookies.get("p_uin") or "")
        key = cookies.get("qm_keyst") or cookies.get("qqmusic_key") or cookies.get("music_key") or ""
        if not key:
            raise LoginError("cookie 里没有 qm_keyst / qqmusic_key，请确认复制的是 y.qq.com 登录后的完整 cookie")
        if not uin:
            raise LoginError("cookie 里没有 uin，请确认复制的是 y.qq.com 登录后的完整 cookie")
        record = self.store.load()
        record.uin = uin
        record.musicid = str(cookies.get("musicid") or uin)
        record.musickey = key
        record.refresh_key = str(cookies.get("refresh_key") or "")
        record.access_token = str(cookies.get("access_token") or "")
        record.openid = str(cookies.get("openid") or cookies.get("psrf_qqopenid") or "")
        record.nick = str(cookies.get("nick") or "")
        record.login_type = 2
        record.cookies = {k: v for k, v in cookies.items()
                          if k not in ("qm_keyst", "qqmusic_key", "uin", "pt2gguin")}
        self.store.save(record)
        return record

    async def verify(self, client: QQMusicClient) -> dict[str, Any]:
        """登录态自检：用一次详情/取链侧接口判断 key 是否还有效。"""
        await client.refresh_session()
        record = client.session
        if not record.logged_in:
            return {"logged_in": False, "valid": False}
        try:
            await client.song_detail("0039MnYb0qxYhV")
            return {"logged_in": True, "valid": True, **record.public()}
        except QQMusicError:
            return {"logged_in": True, "valid": False, **record.public()}
