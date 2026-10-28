"""QQ 音乐 API 客户端（官方 web 接口，不依赖第三方 SDK）。

协议说明：
- 业务接口统一走 ``https://u.y.qq.com/cgi-bin/musicu.fcg``（musicu 协议），
  一个请求可携带多个 module，响应按 ``req_N`` 分段返回；
- 搜索/歌词走老式 CGI（``c.y.qq.com``），字段更全、匿名可用；
- 登录态核心是 ``qm_keyst``（部分场景叫 ``qqmusic_key``），与 y.qq.com 网页版一致：
  带上它才能拿到真实播放地址，匿名只返回 30 秒试听片段（本项目不交付试听片段）。

接口之外的东西（登录、持久化）见 ``login.py``，HTTP 外壳见 ``app.py``。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REFERER = "https://y.qq.com/"

FCG_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
LYRIC_URL = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
COVER_URL = "https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg"
SINGER_COVER_URL = "https://y.qq.com/music/photo_new/T001R300x300M000{singer_mid}.jpg"

# 播放音质：沿用网易盒子的档位命名，便于代理层共用一套音质阶梯与前端选项。
# 前缀是 QQ 音乐 CDN 的文件名前缀：M500=128k mp3，M800=320k mp3，
# C400=m4a，F000=flac，A000=ape，RS01=臻品母带（hires）。
QUALITY_LADDER: dict[str, list[tuple[str, str]]] = {
    "hires": [("RS01", "flac"), ("F000", "flac"), ("M800", "mp3"), ("M500", "mp3")],
    "lossless": [("F000", "flac"), ("M800", "mp3"), ("M500", "mp3")],
    "exhigh": [("M800", "mp3"), ("M500", "mp3")],
    "higher": [("M800", "mp3"), ("M500", "mp3")],
    "standard": [("M500", "mp3")],
}
DEFAULT_QUALITY = "lossless"

# 搜索/详情里 file 字段 -> 展示用音质标记（与前端"SQ/HR/320/128"习惯对齐）
_SEARCH_FEE_VIP = 1


class QQMusicError(Exception):
    """接口层错误基类。status 供 HTTP 外壳映射状态码。"""

    status = 502

    def __init__(self, message: str, *, code: Any = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        if status is not None:
            self.status = status


class NotLoggedIn(QQMusicError):
    status = 401

    def __init__(self, message: str = "not_logged_in"):
        super().__init__(message, status=401)


class TrackUnavailable(QQMusicError):
    """登录了但该曲目当前账号拿不到直链（VIP/下架/版权限制）。"""

    status = 404


def song_cover(album_mid: str) -> str:
    mid = str(album_mid or "").strip()
    return COVER_URL.format(album_mid=mid) if mid else ""


def singer_names(singers: Any) -> str:
    if not isinstance(singers, list):
        return str(singers or "")
    names: list[str] = []
    for s in singers:
        if isinstance(s, dict):
            name = s.get("name") or s.get("title") or ""
        else:
            name = str(s or "")
        name = str(name).strip()
        if name:
            names.append(name)
    return " / ".join(names)


@dataclass
class Session:
    """登录态。cookies 保留登录跳转过程中的全部 cookie，便于透传给官方接口。"""

    uin: str = ""          # QQ 号（纯数字）
    musicid: str = ""      # QQ 音乐账号 id
    musickey: str = ""     # qm_keyst / qqmusic_key
    refresh_key: str = ""
    access_token: str = ""
    openid: str = ""
    nick: str = ""
    avatar: str = ""
    vip: bool = False
    login_type: int = 2    # 2=QQ 扫码，1=微信扫码
    guid: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    updated_at: float = 0.0

    @property
    def logged_in(self) -> bool:
        return bool(self.uin and self.musickey)

    def cookie_header(self) -> str:
        """官方接口认的 cookie 串：uin 必须是 o0 前缀形式，key 两个名字都给。"""
        parts: list[str] = []
        uin = str(self.uin or "").strip()
        if uin:
            parts.append(f"uin=o0{uin}" if not uin.startswith("o0") else f"uin={uin}")
        key = str(self.musickey or "").strip()
        if key:
            parts.append(f"qm_keyst={key}")
            parts.append(f"qqmusic_key={key}")
        for name, value in (self.cookies or {}).items():
            if not value or name in ("uin", "qm_keyst", "qqmusic_key"):
                continue
            parts.append(f"{name}={value}")
        return "; ".join(parts)

    def public(self) -> dict:
        """对外展示用的登录态（不含密钥）。"""
        return {
            "logged_in": self.logged_in,
            "uin": self.uin,
            "musicid": self.musicid,
            "nick": self.nick,
            "avatar": self.avatar,
            "vip": self.vip,
            "login_type": self.login_type,
            "updated_at": self.updated_at,
        }


class SessionStore:
    """登录态落盘（0600 原子写），容器内固定在 /data/qqmusic。"""

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or default_data_dir())
        self.path = self.data_dir / "session.json"

    def load(self) -> Session:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Session(guid=self._ensure_guid())
        except OSError:
            return Session(guid=self._ensure_guid())
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return Session(guid=self._ensure_guid())
        if not isinstance(data, dict):
            return Session(guid=self._ensure_guid())
        known = {f for f in Session.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        cookies = payload.get("cookies")
        if not isinstance(cookies, dict):
            payload["cookies"] = {}
        session = Session(**payload)
        if not session.guid:
            session.guid = self._ensure_guid()
        return session

    def save(self, session: Session) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not session.guid:
            # guid 是取链的设备标识：一次生成后必须跟着登录态一起落盘，
            # 否则每次重启都会换一个，vkey 请求的设备指纹随之漂移。
            session.guid = self.store_guid()
        session.updated_at = time.time()
        tmp = self.path.with_suffix(".json.tmp")
        data = asdict(session)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def clear(self) -> Session:
        guid = self.store_guid()
        fresh = Session(guid=guid)
        self.save(fresh)
        return fresh

    def store_guid(self) -> str:
        """guid 是取链与风控的设备标识，稳定即可（重启不变）。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            guid = str((data or {}).get("guid") or "").strip()
            if guid:
                return guid
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return self._ensure_guid()

    def _ensure_guid(self) -> str:
        return str(random.randint(10**9, 10**10 - 1))


def default_data_dir() -> Path:
    """数据目录优先级：FNMUSIC_QQ_DATA_DIR > /data/qqmusic（可用时）> 包内 data/。

    与 musicbox 的 XDG 目录策略保持一致：容器/原生部署都挂在 /data，
    本地源码调试（无 /data 写权限）退到包目录，避免起不来。
    """
    explicit = os.environ.get("FNMUSIC_QQ_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit)
    candidate = Path("/data/qqmusic")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".writable"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return candidate
    except OSError:
        return Path(__file__).resolve().parent / "data"


class QQMusicClient:
    """一次实例 = 一个登录态 + 一组 HTTP 连接。线程/协程内并发安全（httpx 自身保证）。"""

    def __init__(
        self,
        store: SessionStore | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 12.0,
    ):
        self.store = store or SessionStore()
        self.session = self.store.load()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout
        self._lock = asyncio.Lock()

    # ---------- 基础设施 ----------

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=8.0),
                follow_redirects=True,
                transport=self._transport,
                headers={"User-Agent": UA, "Referer": REFERER, "Accept": "application/json, text/plain, */*"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def refresh_session(self) -> None:
        """登录态写盘后（扫码/粘贴 cookie）重新读盘，长驻实例不必重启。"""
        async with self._lock:
            self.session = self.store.load()

    def _uin_int(self) -> int:
        try:
            return int(str(self.session.uin or "0").strip() or 0)
        except ValueError:
            return 0

    def _comm(self) -> dict:
        comm: dict[str, Any] = {"ct": 24, "cv": 0, "v": 0, "format": "json", "uin": self._uin_int()}
        if self.session.musickey:
            comm["authst"] = self.session.musickey
            comm["tmeLoginType"] = self.session.login_type
        return comm

    async def _fcg(self, modules: dict[str, dict], *, need_login: bool = False) -> dict:
        """musicu 批量调用：modules = {"req_0": {"module":..., "method":..., "param":...}}。"""
        if need_login and not self.session.logged_in:
            raise NotLoggedIn()
        payload: dict[str, Any] = {"comm": self._comm()}
        payload.update({k: v for k, v in modules.items()})
        client = await self.client()
        headers = {}
        cookie = self.session.cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        try:
            resp = await client.post(FCG_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise QQMusicError(f"qqmusic request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise QQMusicError(f"qqmusic http {resp.status_code}", status=502)
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise QQMusicError("qqmusic returned non-json") from exc
        if not isinstance(data, dict):
            raise QQMusicError("qqmusic returned unexpected payload")
        return data

    @staticmethod
    def _module_data(data: dict, key: str = "req_0") -> dict:
        block = data.get(key)
        if not isinstance(block, dict):
            raise QQMusicError("qqmusic missing module block", code=None)
        return block

    # ---------- 搜索 ----------

    async def search(self, keyword: str, limit: int = 20, offset: int = 0) -> list[dict]:
        """歌曲搜索。老式 CGI + new_json=1：拿到 file 体积，用于判断有无无损/320k。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        page = offset // max(limit, 1) + 1
        params = {
            "p": page,
            "n": max(1, min(int(limit), 100)),
            "w": keyword,
            "format": "json",
            "cr": 1,
            "new_json": 1,
            "t": 0,
        }
        client = await self.client()
        headers = {}
        cookie = self.session.cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        try:
            resp = await client.get(SEARCH_URL, params=params, headers=headers)
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise QQMusicError(f"qqmusic search failed: {type(exc).__name__}") from exc
        song_block = (((data or {}).get("data") or {}).get("song") or {})
        rows = song_block.get("list") if isinstance(song_block, dict) else None
        if not isinstance(rows, list):
            return []
        return [self._normalize_search_row(row) for row in rows if isinstance(row, dict)]

    def _normalize_search_row(self, row: dict) -> dict:
        album = row.get("album") if isinstance(row.get("album"), dict) else {}
        file_info = row.get("file") if isinstance(row.get("file"), dict) else {}
        pay = row.get("pay") if isinstance(row.get("pay"), dict) else {}
        sizes = {
            "flac": int(file_info.get("size_flac") or 0),
            "hires": int(file_info.get("size_hires") or 0),
            "320": int(file_info.get("size_320mp3") or 0) or int(file_info.get("size_320") or 0),
            "128": int(file_info.get("size_128mp3") or 0) or int(file_info.get("size_128") or 0),
            "aac": int(file_info.get("size_aac") or 0) or int(file_info.get("size_96aac") or 0),
        }
        if sizes["hires"]:
            quality, ext, size = "HR", "flac", sizes["hires"]
        elif sizes["flac"]:
            quality, ext, size = "SQ", "flac", sizes["flac"]
        elif sizes["320"]:
            quality, ext, size = "320", "mp3", sizes["320"]
        else:
            quality, ext, size = "128", "mp3", sizes["128"] or sizes["aac"]
        album_mid = str(album.get("mid") or row.get("albummid") or "")
        mid = str(row.get("mid") or row.get("songmid") or "")
        return {
            "song_id": mid,
            "songmid": mid,
            "song_name": str(row.get("name") or row.get("title") or row.get("songname") or ""),
            "artist": singer_names(row.get("singer")),
            "album_name": str(album.get("name") or row.get("albumname") or ""),
            "album_id": album_mid,
            "album_pic_url": song_cover(album_mid),
            "duration": int(row.get("interval") or 0),
            "quality": quality,
            "ext": ext,
            "file_size": size,
            # pay_play=1 表示该曲目需要付费/会员权益：无权益账号取链会失败，
            # 上层据此在歌名前打 [VIP] 标记，让用户点播前就有预期。
            "fee": _SEARCH_FEE_VIP if int(pay.get("pay_play") or 0) else 0,
            "version": str(row.get("subtitle") or row.get("version") or ""),
            "media_mid": str(file_info.get("media_mid") or file_info.get("strMediaMid") or mid),
        }

    # ---------- 详情 / 歌词 ----------

    async def song_detail(self, songmid: str) -> dict:
        """原始详情：pf_song_detail_svr。找不到抛 TrackUnavailable。"""
        data = await self._fcg({
            "req_0": {
                "module": "music.pf_song_detail_svr",
                "method": "get_song_detail",
                "param": {"song_mid": str(songmid)},
            }
        })
        block = self._module_data(data)
        info = (block.get("data") or {}).get("track_info") if isinstance(block.get("data"), dict) else None
        if not isinstance(info, dict) or not info.get("mid"):
            raise TrackUnavailable(f"song not found: {songmid}", status=404)
        return info

    async def song_detail_compat(self, songmid: str) -> dict:
        """网易盒子形态的详情（name/ar/al/dt/sq/hr），代理层详情/歌词路径可直接复用。"""
        info = await self.song_detail(songmid)
        album = info.get("album") if isinstance(info.get("album"), dict) else {}
        file_info = info.get("file") if isinstance(info.get("file"), dict) else {}
        album_mid = str(album.get("mid") or "")
        has_flac = bool(int(file_info.get("size_flac") or 0))
        has_hires = bool(int(file_info.get("size_hires") or 0))
        size_320 = int(file_info.get("size_320mp3") or file_info.get("size_320") or 0)
        size_128 = int(file_info.get("size_128mp3") or file_info.get("size_128") or 0)
        pay = info.get("pay") if isinstance(info.get("pay"), dict) else {}
        interval = int(info.get("interval") or 0)
        return {
            "id": str(info.get("mid") or songmid),
            "name": str(info.get("name") or info.get("title") or ""),
            "ar": [{"name": str(s.get("name") or "")} for s in (info.get("singer") or []) if isinstance(s, dict)],
            "al": {"name": str(album.get("name") or ""), "picUrl": song_cover(album_mid)},
            "dt": interval * 1000,
            "sq": {"size": int(file_info.get("size_flac") or 0)} if has_flac else None,
            "hr": {"size": int(file_info.get("size_hires") or 0)} if has_hires else None,
            "h": {"size": size_320} if size_320 else {"size": size_128},
            "fee": _SEARCH_FEE_VIP if int(pay.get("pay_play") or 0) else 0,
            "source": "qq",
        }

    async def batch_song_details(self, songmids: list[str], chunk: int = 20) -> dict[str, dict]:
        """批量详情：一个 musicu 请求里挂多个 module（每 20 条一批）。

        搜索一次要补 20~50 首的封面与音质，逐首串行发请求会吃掉整段搜索预算；
        官方 musicu 协议本身支持 req_0..req_N 多模块并发，这里按批合并。
        """
        mids = [str(m).strip() for m in songmids if str(m or "").strip()]
        mids = list(dict.fromkeys(mids))
        out: dict[str, dict] = {}
        for start in range(0, len(mids), max(1, chunk)):
            group = mids[start:start + max(1, chunk)]
            modules = {
                f"req_{i}": {
                    "module": "music.pf_song_detail_svr",
                    "method": "get_song_detail",
                    "param": {"song_mid": mid},
                }
                for i, mid in enumerate(group)
            }
            try:
                data = await self._fcg(modules)
            except QQMusicError:
                continue
            for i, mid in enumerate(group):
                block = data.get(f"req_{i}") if isinstance(data.get(f"req_{i}"), dict) else {}
                inner = block.get("data") if isinstance(block.get("data"), dict) else {}
                info = inner.get("track_info") if isinstance(inner.get("track_info"), dict) else {}
                if info.get("mid"):
                    out[mid] = info
        return out

    async def song_detail_rows(self, songmids: list[str]) -> list[dict]:
        """批量详情（扁平行，对齐 musicbox 的 /songs/detail 输出）。

        代理搜索用这套字段补封面与无损判定：``song_id`` / ``album_pic_url`` /
        ``has_sq`` / ``has_hr``。单曲失败跳过，不影响整批。
        """
        wanted = [str(m).strip() for m in songmids if str(m or "").strip()]
        details = await self.batch_song_details(wanted)
        rows: list[dict] = []
        for mid in dict.fromkeys(wanted):
            info = details.get(mid)
            if not isinstance(info, dict):
                continue
            album = info.get("album") if isinstance(info.get("album"), dict) else {}
            file_info = info.get("file") if isinstance(info.get("file"), dict) else {}
            rows.append({
                "song_id": str(info.get("mid") or mid),
                "song_name": str(info.get("name") or info.get("title") or ""),
                "artist": singer_names(info.get("singer")),
                "album_name": str(album.get("name") or ""),
                "album_pic_url": song_cover(str(album.get("mid") or "")),
                "duration": int(info.get("interval") or 0),
                "has_sq": bool(int(file_info.get("size_flac") or 0)),
                "has_hr": bool(int(file_info.get("size_hires") or 0)),
            })
        return rows

    async def lyric(self, songmid: str) -> dict:
        client = await self.client()
        params = {"songmid": str(songmid), "format": "json", "nobase64": 1, "g_tk": 5381, "loginUin": 0, "hostUin": 0}
        try:
            resp = await client.get(LYRIC_URL, params=params, headers={"Referer": "https://y.qq.com/portal/player.html"})
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise QQMusicError(f"qqmusic lyric failed: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise QQMusicError("qqmusic lyric returned unexpected payload")
        return {
            "lyric": str(data.get("lyric") or "").strip(),
            "tlyric": str(data.get("trans") or "").strip(),
        }

    # ---------- 取链 ----------

    async def _vkey(self, songmid: str, prefix: str, ext: str) -> str:
        filename = f"{prefix}{songmid}.{ext}"
        params = {
            "guid": str(self.session.guid or "10000"),
            "songmid": [songmid],
            "songtype": [0],
            "uin": str(self.session.uin or "0") if self.session.logged_in else "0",
            "loginflag": 1,
            "platform": "20",
            "filename": [filename],
        }
        data = await self._fcg({
            "req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey", "param": params}
        })
        block = self._module_data(data)
        inner = block.get("data") if isinstance(block.get("data"), dict) else {}
        infos = inner.get("midurlinfo") if isinstance(inner.get("midurlinfo"), list) else []
        purl = ""
        for item in infos:
            if isinstance(item, dict) and str(item.get("filename") or "") == filename:
                purl = str(item.get("purl") or "").strip()
                break
        if not purl and infos and isinstance(infos[0], dict):
            purl = str(infos[0].get("purl") or "").strip()
        if not purl:
            return ""
        sip = inner.get("sip") if isinstance(inner.get("sip"), list) else []
        host = ""
        for candidate in sip:
            text = str(candidate or "").strip()
            if text.startswith("http"):
                host = text.rstrip("/") + "/"
                break
        return f"{host}{purl}" if host else purl

    async def song_url(self, songmid: str, quality: str = DEFAULT_QUALITY) -> dict:
        """按音质阶梯逐档尝试，返回首个可用直链。未登录直接拒绝（不交付试听片段）。"""
        quality = str(quality or DEFAULT_QUALITY).strip().lower()
        if quality not in QUALITY_LADDER:
            quality = DEFAULT_QUALITY
        if not self.session.logged_in:
            raise NotLoggedIn()
        last_error: str = ""
        for prefix, ext in QUALITY_LADDER[quality]:
            url = await self._vkey(str(songmid), prefix, ext)
            if url:
                return {
                    "code": 200,
                    "url": url,
                    "type": ext,
                    "ext": ext,
                    "quality": prefix,
                    # QQ 直链签名有效期通常数小时；保守给 20 分钟，过期由上层 refresh 重解析
                    "expi": 1200,
                    "size": 0,
                    "br": {"M500": 128000, "M800": 320000, "F000": 999000, "RS01": 1400000}.get(prefix, 0),
                }
            last_error = prefix
        raise TrackUnavailable(
            f"no playable url for {songmid} (tried {last_error}); VIP/版权限制或登录态失效",
            code=404,
            status=404,
        )

    # ---------- 榜单 / 歌单 ----------

    async def toplist_catalog(self) -> list[dict]:
        """常用榜单目录（官方榜单 id 长期稳定）。"""
        return [
            {"id": 26, "name": "热歌榜", "cover": ""},
            {"id": 4, "name": "新歌榜", "cover": ""},
            {"id": 62, "name": "飙升榜", "cover": ""},
            {"id": 27, "name": "流行指数榜", "cover": ""},
            {"id": 3, "name": "欧美榜", "cover": ""},
            {"id": 5, "name": "日韩榜", "cover": ""},
            {"id": 6, "name": "内地榜", "cover": ""},
            {"id": 16, "name": "韩国榜", "cover": ""},
            {"id": 17, "name": "日本榜", "cover": ""},
            {"id": 28, "name": "DJ 舞曲榜", "cover": ""},
            {"id": 23, "name": "说唱榜", "cover": ""},
            {"id": 19, "name": "摇滚榜", "cover": ""},
        ]

    async def toplist(self, top_id: int, limit: int = 60) -> list[dict]:
        data = await self._fcg({
            "req_0": {
                "module": "musicToplist.ToplistInfoServer",
                "method": "GetDetail",
                "param": {"topId": int(top_id), "offset": 0, "num": max(1, min(int(limit), 100)), "period": ""},
            }
        })
        block = self._module_data(data)
        # 曲目在 req_0.data.songInfoList（完整 track 结构，带 file 体积用于音质判定）；
        # req_0.data.data.song 是精简榜单行（有 title/singerName 但没有 songmid），
        # 取链需要 songmid，所以只在前者缺失时用它做展示兜底。
        inner = block.get("data") if isinstance(block.get("data"), dict) else {}
        rows = inner.get("songInfoList") if isinstance(inner.get("songInfoList"), list) else []
        if not rows:
            nested = inner.get("data") if isinstance(inner.get("data"), dict) else {}
            rows = nested.get("song") if isinstance(nested.get("song"), list) else []
            return [self._normalize_rank_row(row) for row in rows if isinstance(row, dict)]
        return [self._normalize_search_row(row) for row in rows if isinstance(row, dict)]

    def _normalize_rank_row(self, row: dict) -> dict:
        """精简榜单行（无 songmid）兜底：只用于展示，取链会因缺 id 失败。"""
        return {
            "song_id": "",
            "songmid": "",
            "song_name": str(row.get("title") or row.get("name") or ""),
            "artist": str(row.get("singerName") or ""),
            "album_name": "",
            "album_id": str(row.get("albumMid") or ""),
            "album_pic_url": str(row.get("cover") or ""),
            "duration": 0,
            "quality": "320",
            "ext": "mp3",
            "file_size": 0,
            "fee": 0,
            "version": "",
        }

    async def playlist(self, disstid: int) -> dict:
        data = await self._fcg({
            "req_0": {
                "module": "music.srfDissInfo.DissInfo",
                "method": "CgiGetDiss",
                "param": {"disstid": int(disstid), "dirid": 0, "tag": 1, "song_begin": 0, "song_num": 100, "userinfo": 1},
            }
        })
        block = self._module_data(data)
        inner = block.get("data") if isinstance(block.get("data"), dict) else {}
        info = inner.get("dirinfo") if isinstance(inner.get("dirinfo"), dict) else {}
        rows = inner.get("songlist") if isinstance(inner.get("songlist"), list) else []
        # 歌单曲目与搜索结果同构（mid/name/singer/album/file），直接复用归一化
        items = [self._normalize_search_row(row) for row in rows if isinstance(row, dict)]
        return {
            "id": int(disstid),
            "name": str(info.get("title") or info.get("diss_name") or ""),
            "cover_url": str(info.get("headurl") or info.get("picurl") or ""),
            "description": str(info.get("desc") or ""),
            "creator": str((info.get("creator") or {}).get("nick") if isinstance(info.get("creator"), dict)
                           else info.get("host_nick") or ""),
            "tracks": items,
        }

    async def user_playlists(self, limit: int = 100) -> list[dict] | None:
        """账号自建 + 收藏歌单。未登录返回 None；接口异常返回 None（上层展示"未登录"）。"""
        if not self.session.logged_in:
            return None
        try:
            data = await self._fcg({
                "req_0": {
                    "module": "music.musicasset.PlaylistBaseRead",
                    "method": "GetPlaylistByUin",
                    "param": {"uin": self._uin_int(), "sin": 0, "size": max(1, min(int(limit), 200))},
                }
            })
        except QQMusicError:
            return None
        block = data.get("req_0") if isinstance(data, dict) else None
        if not isinstance(block, dict) or int(block.get("code") or 0) != 0:
            return None
        inner = block.get("data") if isinstance(block.get("data"), dict) else {}
        rows = inner.get("v_playlist") if isinstance(inner.get("v_playlist"), list) else []
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            result.append({
                "id": int(row.get("dissid") or row.get("tid") or 0),
                "name": str(row.get("diss_name") or row.get("dirName") or ""),
                "cover_url": str(row.get("diss_cover") or ""),
                "track_count": int(row.get("song_cnt") or row.get("songnum") or 0),
                "creator": "self" if int(row.get("diruin") or 0) == self._uin_int() else "collected",
            })
        return result

    async def recommend(self, limit: int = 30) -> list[dict]:
        """每日/猜你喜欢推荐（需登录）。匿名或接口不可用时返回空列表，由上层兜底。"""
        if not self.session.logged_in:
            return []
        try:
            data = await self._fcg({
                "req_0": {
                    "module": "music.recommend.RecommendFeed",
                    "method": "GetRecommendFeed",
                    "param": {"direction": 0, "page": 0, "v_cache_ts": 0},
                }
            })
        except QQMusicError:
            return []
        items: list[dict] = []
        block = data.get("req_0") if isinstance(data, dict) else None
        if not isinstance(block, dict) or int(block.get("code") or 0) != 0:
            return []
        for key in ("vecRecommendFeed", "vecFeed", "list"):
            rows = (block.get("data") or {}).get(key) if isinstance(block.get("data"), dict) else None
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                content = row.get("content") if isinstance(row.get("content"), dict) else row
                mid = str(content.get("mid") or content.get("songmid") or "")
                if not mid:
                    continue
                album = content.get("album") if isinstance(content.get("album"), dict) else {}
                items.append({
                    "song_id": mid,
                    "songmid": mid,
                    "song_name": str(content.get("name") or content.get("title") or ""),
                    "artist": singer_names(content.get("singer")),
                    "album_name": str(album.get("name") or ""),
                    "album_id": str(album.get("mid") or ""),
                    "album_pic_url": song_cover(str(album.get("mid") or "")),
                    "duration": int(content.get("interval") or 0),
                    "quality": "320",
                    "ext": "mp3",
                    "file_size": 0,
                    "fee": 0,
                    "version": "",
                })
            if items:
                break
        return items[: max(1, min(int(limit), 100))]
