#!/bin/sh
# fnmusic-sources 容器入口：supervisor 起来后按 /repo/.env 只拉起所选音源（+WebUI）。
# 全部程序 autostart=false（supervisord.conf），实现按需加载：
# 未启用的音源进程不驻留内存；容器重启后本脚本按 .env 自愈恢复同样的进程集。
set -eu

SUP_CONF="${SUPERVISOR_CONF:-/etc/supervisor/supervisord.conf}"
. "${ENV_FLAG_LIB:-/usr/local/bin/env_flag.sh}"

log() { echo "[entrypoint] $*"; }

export_source_env

# 确保 lxserver 数据目录与初始配置就绪
init_lxserver_config() {
    _lx_data="${DATA_PATH:-/data/lxserver}"
    mkdir -p "${_lx_data}/backups" "${_lx_data}/users/source/_open"
    _cfg_file="${_lx_data}/config.js"
    if [ ! -f "${_cfg_file}" ] || (grep -q "frontend:" "${_cfg_file}" 2>/dev/null && ! grep -q "frontend.password" "${_cfg_file}" 2>/dev/null); then
        # 默认密码从环境变量获取，或缺省为 123456
        _admin_pw="${LXSERVER_ADMIN_PASSWORD:-123456}"
        _escaped_pw="$(printf '%s' "${_admin_pw}" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
        cat <<EOF > "${_cfg_file}"
module.exports = {
  serverName: "fnmusic-lxserver",
  bindIP: "127.0.0.1",
  port: 8005,
  "frontend.password": "${_escaped_pw}",
  "player.enableAuth": false,
  "subsonic.enable": false,
  "webdav.enable": false,
  "system.allowUnsafeVM": true
};
EOF
        log "已初始化/对齐 lxserver 配置: ${_cfg_file}"
    fi

    # 历史数据无损迁移：从旧 /data/lxmusic 迁移用户自定义源脚本到 lxserver
    _old_lx_data="${LX_DATA_DIR:-/data/lxmusic}"
    if [ -d "${_old_lx_data}/uploads" ]; then
        for _f in "${_old_lx_data}/uploads"/*.js; do
            if [ -f "${_f}" ]; then
                _base="$(basename "${_f}")"
                if [ ! -f "${_lx_data}/users/source/_open/${_base}" ]; then
                    cp -a "${_f}" "${_lx_data}/users/source/_open/"
                    log "已从旧版本迁移自定义源脚本: ${_base}"
                fi
            fi
        done
    fi
    if [ -f "${_old_lx_data}/source.js" ] && [ ! -f "${_lx_data}/users/source/_open/migrated_source.js" ]; then
        if [ ! "$(ls -A "${_lx_data}/users/source/_open" 2>/dev/null)" ]; then
            cp -a "${_old_lx_data}/source.js" "${_lx_data}/users/source/_open/migrated_source.js"
            log "已迁移旧版本激活的自定义源 source.js"
        fi
    fi
}

# 代码同步（issue: 升级后页面仍是旧版）
# /srv/* 里的服务代码是镜像烘焙的，而 /repo 是宿主机安装目录的 bind mount。
# 一旦镜像被复用（构建指纹未变、或构建失败回退旧镜像），就会出现
# 「VERSION/.env 是新版、页面和接口还是旧版」的错配——管理页显示已删除的
# 音源卡片、新入口缺失，且硬刷新无效（服务端返回的就是旧 HTML）。
# 这里在拉起任何进程之前把挂载仓库里的服务代码同步进运行目录，
# 保证容器里跑的永远是安装目录里那一份。
sync_repo_code() {
    _repo="${WEBUI_REPO_DIR:-/repo}"
    [ -d "${_repo}/webui-service" ] || return 0
    for _svc in musicbox-service qqmusic-service lxmusic-service webui-service; do
        [ -d "${_repo}/${_svc}" ] || continue
        mkdir -p "/srv/${_svc}" 2>/dev/null || {
            log "WARN: /srv/${_svc} 不可写，沿用镜像内版本"
            continue
        }
        # 用 cp -R 而不是 cp -a：-a 会尝试保留属主，非 root 下会报错，
        # 从而把「文件其实已经复制好了」误判成失败。
        if cp -R "${_repo}/${_svc}/." "/srv/${_svc}/" 2>/dev/null; then
            log "已同步 ${_svc} 代码 → /srv/${_svc}"
        else
            log "WARN: 同步 ${_svc} 失败，沿用镜像内版本"
        fi
    done
    # webui 运行期会 import 这些 proxy 模块。这里整目录同步而不是抄一份固定清单：
    # 之前按清单只同步了 6 个模块，新增/改名的模块会被静默漏掉，等于换个位置再犯
    # 一次「代码是旧的」的错。
    if [ -d "${_repo}/proxy" ] && [ -d /srv/proxy ]; then
        for _f in "${_repo}"/proxy/*.py; do
            [ -f "${_f}" ] || continue
            cp -f "${_f}" /srv/proxy/ 2>/dev/null || true
        done
        log "已同步 proxy 模块 → /srv/proxy"
    fi
}

sync_repo_code

init_lxserver_config

supervisord -c "$SUP_CONF" &
SUP_PID=$!

# 等 supervisor 控制套接字就绪（最多 10s）。
_i=0
until supervisorctl -c "$SUP_CONF" pid >/dev/null 2>&1; do
    _i=$((_i + 1))
    if [ "$_i" -ge 100 ]; then
        log "supervisor 套接字未就绪，继续等待（进程状态将由 healthcheck 反映）"
        break
    fi
    sleep 0.1
done

start_prog() {
    if supervisorctl -c "$SUP_CONF" start "$1" >/dev/null 2>&1; then
        log "started $1"
    else
        log "WARN: start $1 失败（supervisorctl status 查看原因）"
    fi
}

# 三选一音源：只启动 .env 选中的那个（多开视为异常，全部忽略只取第一个命中）
if [ "$(env_flag FNMUSIC_NETEASE_ENABLED)" = "true" ]; then
    start_prog musicbox
elif [ "$(env_flag FNMUSIC_QQMUSIC_ENABLED)" = "true" ]; then
    start_prog qqmusic
elif [ "$(env_flag FNMUSIC_LX_ENABLED)" = "true" ]; then
    start_prog lxserver
    start_prog lxmusic
else
    log "未配置任何音源（FNMUSIC_NETEASE_ENABLED/FNMUSIC_QQMUSIC_ENABLED/FNMUSIC_LX_ENABLED 均未启用）"
fi

# 网易云歌单常驻（issue #38）：当前音源不是 musicbox 但勾选常驻，且有网易
# 账号歌单（盘上有 cookie，登录启发）或网易公共歌单选择时，拉起 musicbox。
# 权威校验由 webui 开机对账完成。原生形态 entrypoint 无 XDG 环境变量，可能
# 找不到 cookie 而跳过——由 webui 开机对账兜底拉起。
musicbox_cookie_present() {
    for _f in "${XDG_DATA_HOME:-/nonexistent}/netease-musicbox/cookie.txt" \
              "${HOME:-/root}/.netease-musicbox/cookie.txt" \
              "${XDG_CONFIG_HOME:-/nonexistent}/netease-musicbox/cookie.txt"; do
        if [ -s "$_f" ]; then
            return 0
        fi
    done
    return 1
}

public_musicbox_selected() {
    [ "$(env_flag FNMUSIC_PUBLIC_PLAYLISTS_ENABLED)" = "true" ] || return 1
    python3 -c 'import json, os, sys
raw = os.environ.get("FNMUSIC_PUBLIC_PLAYLISTS_SELECTION", "")
try:
    rows = json.loads(raw) if raw.strip() else [{"provider": "musicbox", "id": "3778678"}]
    needed = isinstance(rows, list) and any(isinstance(x, dict) and x.get("provider") == "musicbox" and x.get("id") for x in rows)
except (ValueError, TypeError):
    needed = False
sys.exit(0 if needed else 1)'
}

if [ "$(env_flag FNMUSIC_NETEASE_ENABLED)" != "true" ] \
   && [ "$(env_flag FNMUSIC_NETEASE_PLAYLIST_RESIDENT)" = "true" ] \
   && { public_musicbox_selected \
        || { [ "$(env_flag FNMUSIC_NETEASE_MY_PLAYLISTS)" = "true" ] && musicbox_cookie_present; }; }; then
    start_prog musicbox
fi

# WebUI 默认随容器启动（--no-webui 安装会写 FNMUSIC_WEBUI_ENABLED=false）
if [ "$(env_flag FNMUSIC_WEBUI_ENABLED)" = "true" ]; then
    start_prog webui
fi

# 信号转发：docker stop → supervisorctl shutdown → 各程序优雅退出
trap 'supervisorctl -c "$SUP_CONF" shutdown >/dev/null 2>&1' TERM INT
log "fnmusic-sources ready (pid=$SUP_PID)"
wait "$SUP_PID"
