# qqmusic-service · QQ 音乐音源

飞牛音乐扩展的 QQ 音乐音源：直连 QQ 音乐官方 web 接口，提供**搜索 / 取播放直链 / 歌词 / 详情 / 榜单 / 歌单**，支持**手机 QQ 扫码登录**，登录态落盘重启不丢。

不依赖任何第三方音乐 SDK，也不托管音频：本服务只把官方直链交给代理层，缓存、转码、落库仍由 `proxy/` 统一处理。

## 接口

接口形态刻意与 `musicbox-service`（网易盒子）对齐，代理层的归一化与取链骨架可复用；差别只在歌曲 id 是 **songmid 字符串**而不是数字。

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/healthz` | 健康检查，含登录态 |
| GET | `/api/v1/search?keyword=&limit=&offset=` | 歌曲搜索（`new_json=1`，带体积用于判定有无无损） |
| GET | `/api/v1/song/{songmid}/url?quality=` | 取播放直链，音质阶梯逐档回退 |
| GET | `/api/v1/song/{songmid}/info` | 单曲详情（网易盒子形态：`name/ar/al/dt/sq/hr`） |
| GET | `/api/v1/songs/detail?ids=a,b,c` | 批量详情（扁平行：`song_id/album_pic_url/has_sq`） |
| GET | `/api/v1/song/{songmid}/lyric` | 歌词（含翻译） |
| GET | `/api/v1/toplist?index=` | 不带 index 返回榜单目录，带 index 返回该榜单曲目 |
| GET | `/api/v1/playlist/{disstid}` | 歌单元信息 + 前 100 首 |
| GET | `/api/v1/user/playlists` | 账号歌单（需登录） |
| GET | `/api/v1/recommend/songs` | 推荐（需登录，官方接口不可用时返回空数组） |
| GET | `/api/v1/auth/status` | 登录态 |
| POST | `/api/v1/auth/login` | 生成扫码会话：返回 `unikey`、二维码 `image`（data URL） |
| GET | `/api/v1/auth/login/check?unikey=` | 轮询扫码状态（状态码见下） |
| GET | `/api/v1/auth/login/qr.png?unikey=` | 二维码 PNG |
| POST | `/api/v1/auth/cookie` | 兜底登录：`{"cookie": "uin=o0...; qm_keyst=..."}` |
| POST | `/api/v1/auth/logout` | 清除登录态 |

扫码状态码沿用网易盒子口径，便于前端复用同一套轮询：

| 码 | 含义 |
| :--- | :--- |
| 801 | 等待扫码 |
| 802 | 已扫码，待手机确认 |
| 803 | 登录成功 |
| 800 | 二维码过期 / 登录失败 |

## 登录原理

与 y.qq.com 网页版同一套流程，纯 HTTP，不需要浏览器，也不保存账号密码：

```
xlogin(取 login_sig) → ptqrshow(出码 + qrsig)
   → ptqrtoken = hash33(qrsig) → ptqrlogin 轮询
   → 66 未扫 / 67 已扫待确认 / 65 过期 / 0 成功
   → 跟 check_sig 跳转走完 cookie 握手 → 取 qm_keyst
   → （少数情况只给 p_skey）music.login.LoginServer/Login 换 musickey
```

已知风险：QQ 侧改跳转链路时，扫码可能确认成功但拿不到 `qm_keyst`。此时服务会返回明确的失败提示，请改用管理页的**「Cookie 登录」**：浏览器登录 y.qq.com → F12 → Application → Cookies，复制包含 `uin` 与 `qm_keyst`（或 `qqmusic_key`）的整串 cookie 粘贴即可。

## 关于"未登录只能搜索"

QQ 音乐匿名请求只返回 30 秒试听片段（`testfile2g`），拿不到 `purl` 播放直链。本项目**不交付试听片段**：未登录时取链接口直接回 401 `not_logged_in`，客户端表现为"该曲目暂不可播"，不会出现"播到 30 秒跳歌"的困惑。

登录后能否播放、能拿到什么音质，取决于**账号权益**：无会员时部分曲目仍拿不到直链（接口回 404 `track_unavailable`），会员曲目按账号权益给到 320k / 无损。

## 数据与配置

| 项 | 说明 |
| :--- | :--- |
| 登录态 | `/data/qqmusic/session.json`（0600，容器内）或 `FNMUSIC_QQ_DATA_DIR` 指定目录 |
| `FNMUSIC_QQ_QUALITY` | `standard` / `higher` / `exhigh` / `lossless`（默认）/ `hires` |
| `FNMUSIC_QQ_SEARCH_LIMIT` | 单次搜索条数（默认 20），深分页按此步长翻页 |

音质阶梯（自动逐档回退到有直链的档位）：

| 档位 | 文件名前缀 | 说明 |
| :--- | :--- | :--- |
| `hires` | `RS01` → `F000` → `M800` → `M500` | 臻品母带优先 |
| `lossless` | `F000` → `M800` → `M500` | 无损优先（默认） |
| `exhigh` / `higher` | `M800` → `M500` | 320k 优先 |
| `standard` | `M500` | 128k |

## 本地调试

```bash
cd qqmusic-service
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8776

curl -s 'http://127.0.0.1:8776/api/v1/search?keyword=晴天&limit=3'
curl -s -X POST 'http://127.0.0.1:8776/api/v1/auth/login'   # 拿 unikey + 二维码
```

单元测试（不联网，全部走 mock transport）：

```bash
python -m pytest qqmusic-service -q
```

## 边界

- 只做技术研究与个人使用；不提供会员权限绕过，音质与可播性以账号权益和版权状态为准。
- 官方接口可能随版本调整，`recommend` / `user_playlists` 已做降级（失败返回空），搜索/取链/歌词/详情为强依赖，失效会在日志中留明确原因码。
