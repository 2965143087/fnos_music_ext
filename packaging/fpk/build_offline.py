#!/usr/bin/env python3
"""跨平台 fpk 打包器（不依赖 rsync / fnpack）。

`build.sh` 依赖 rsync + 飞牛官方 `fnpack`（Linux 二进制），在 Windows/macOS 上
无法直接出包。本脚本按 fnpack 的实际产物格式复刻打包过程，可在任意装有
Python 3.9+ 的机器上生成同构的 `.fpk`：

    fpk = gzip(tar)
      ├─ app.tgz                  # gzip(tar)，内含 repo/ 与 ui/
      ├─ cmd/ ...                 # 生命周期脚本（0744）
      ├─ config/{privilege,resource}
      ├─ ICON.PNG / ICON_256.PNG
      ├─ manifest                 # @VERSION@ 已替换，尾部带 checksum = md5(app.tgz)
      └─ wizard/install

结构依据 fnmusic-ext 2.10.2 官方 release 包反推：
- 外层 tar 首个条目是 app.tgz，其余条目按名字排序；
- manifest 的 `checksum` 就是 app.tgz 的 md5（已用官方包验证）；
- repo 内文件统一 0644、目录 0755（与官方包一致，执行位由安装脚本自己 chmod）。

用法:
    python packaging/fpk/build_offline.py                     # 完整出包到 dist/
    python packaging/fpk/build_offline.py --stage-only DIR     # 只组装目录
    python packaging/fpk/build_offline.py --artifact-dir DIR   # 指定 lxserver 预编译包目录
    python packaging/fpk/build_offline.py --version 2.11.0     # 覆盖 VERSION（调试用）
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

FPK_DIR = Path(__file__).resolve().parent
REPO_ROOT = FPK_DIR.parent.parent

# ---- 与 build.sh 的 rsync 排除规则保持一致 ----------------------------------
# 精确文件名（含通配）排除
EXCLUDE_NAME_PATTERNS = (
    ".env",
    ".env.*",          # 但 .env.example 在下面显式放行
    ".git*",
    ".DS_Store",
    "*.pyc",
    "*.log",
    "*.logzip",
    "*.part",
    "*.fpk",
    "*.part",
    "test_*.py",
    "test_*.js",
    "conftest.py",
    "smoke_test.sh",
    "run_tests.py",
    "pytest.ini",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "online_favorites.json",
    "album_registry.json",
    "bind_registry.json",
)
EXCLUDE_DIRS = {
    ".git", ".github", ".zcode", ".cursor", ".agents", ".tasks_archive", ".tools",
    "packaging", "dist", "tests", "docs",
    "__pycache__", ".pytest_cache",
    "logs", "cache", "sources-data", "backup", "musicbox-data", "musicdl_outputs",
    "recommend_cache", "public_playlists_cache", "play_history", "online_favorites",
    "playlist_tracks",
    "node_modules", ".venv", ".venv-sources", ".venv-proxy",
}
# 显式放行的模板文件（.env.* 规则会把它排除）
ALLOW_FILES = {".env.example"}

DIR_MODE = 0o755
FILE_MODE = 0o644
CMD_MODE = 0o755


def log(msg: str) -> None:
    print(f"[fpk] {msg}", flush=True)


_BAD_CHARS = ("\uf03a", ":")


def _suspicious(name: str) -> bool:
    """Windows 上测试残留的伪路径（含冒号/私有区冒号）或超长条目：不进包。"""
    return any(ch in name for ch in _BAD_CHARS) or len(name) > 120


def excluded(rel: Path) -> bool:
    """按 rsync 规则判断相对路径是否排除（rel 为 POSIX 分段）。"""
    name = rel.name
    if any(_suspicious(part) for part in rel.parts):
        return True
    if name in ALLOW_FILES:
        return False
    for part in rel.parts[:-1]:
        if part in EXCLUDE_DIRS or fnmatch.fnmatch(part, ".venv*"):
            return True
    if any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_NAME_PATTERNS):
        return True
    return False


def iter_repo_files(root: Path):
    """产出 (相对路径, 绝对路径, 是否目录)，跳过排除项，按名字排序保证可复现。"""
    for current, dirnames, filenames in os.walk(root):
        cur = Path(current)
        rel_dir = cur.relative_to(root)
        # 目录整体排除
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDE_DIRS
            and not fnmatch.fnmatch(d, ".venv*")
            and not excluded(rel_dir / d)
        )
        if rel_dir.parts:
            yield rel_dir, cur, True
        for name in sorted(filenames):
            rel = rel_dir / name
            if excluded(rel):
                continue
            yield rel, cur / name, False


def add_entry(tar: tarfile.TarFile, name: str, *, is_dir: bool, mode: int,
              data: bytes | None = None, mtime: float = 0.0) -> None:
    info = tarfile.TarInfo(name)
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = int(mtime)
    if is_dir:
        info.type = tarfile.DIRTYPE
        info.size = 0
        tar.addfile(info)
        return
    payload = data if data is not None else b""
    info.size = len(payload)
    tar.addfile(info, io.BytesIO(payload))


def stage_app(repo_root: Path, stage: Path) -> int:
    """把仓库与桌面入口铺到 stage/app（repo/ 与 ui/）。"""
    app_dir = stage / "app"
    repo_dir = app_dir / "repo"
    ui_dir = app_dir / "ui"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)
    ui_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for rel, src, is_dir in iter_repo_files(repo_root):
        dest = repo_dir / rel
        if is_dir:
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            count += 1

    # 根目录的 config/（privilege/resource）在官方包里同时存在于 app.tgz，
    # 运行期按 app/repo 相对路径读取，因此这里复制进 app/ 一起打包。
    src_cfg = FPK_DIR / "config"
    if src_cfg.is_dir():
        dest_cfg = app_dir / "config"
        if dest_cfg.exists():
            shutil.rmtree(dest_cfg)
        shutil.copytree(src_cfg, dest_cfg)

    # 桌面入口（ui/config + icons）来自 packaging/fpk/app/ui
    src_ui = FPK_DIR / "app" / "ui"
    for item in sorted(src_ui.rglob("*")):
        dest = ui_dir / item.relative_to(src_ui)
        if item.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, dest)
    return count


TEXT_EXTS = {
    ".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".ts", ".html", ".htm", ".css",
    ".json", ".md", ".txt", ".yml", ".yaml", ".in", ".service", ".conf", ".ini",
    ".cfg", ".example", ".version", ".toml", ".xml", ".csv", ".env", ".sql",
}
TEXT_BASENAMES = {"Dockerfile", ".dockerignore", ".gitignore", ".zcodeignore", "manifest", "install"}


def _is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTS:
        return True
    if path.name in TEXT_BASENAMES or path.name.startswith("."):
        return True
    return False


def normalize_text_tree(root: Path) -> int:
    """把 staging 目录里的文本文件统一成 LF。

    Windows 产物最常见的翻车点：脚本以 CRLF 进包，飞牛上执行报
    `set: pipefail: invalid option`，应用中心直接判定安装失败。这里按内容
    兜底（含 NUL 字节一律当二进制跳过），不依赖打包机的 git 配置。
    """
    fixed = 0
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        if item.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp",
                                   ".woff", ".woff2", ".ttf", ".zip", ".tgz", ".fpk", ".gz"}:
            continue
        if not _is_text_file(item):
            continue
        try:
            data = item.read_bytes()
        except OSError:
            continue
        if b"\r\n" not in data or b"\x00" in data:
            continue
        item.write_bytes(data.replace(b"\r\n", b"\n"))
        fixed += 1
    if fixed:
        log(f"已归一化 LF：{fixed} 个文本文件")
    return fixed


def pack_app_tgz(stage: Path, mtime: float) -> Path:
    """把 stage/app 压成 app.tgz（内层路径 repo/... 与 ui/...）。

    必须在注入离线预编译包之后调用，否则 app.tgz 里没有 lxserver 产物。
    """
    app_dir = stage / "app"
    app_tgz = stage / "app.tgz"
    entries: list[Path] = []
    for item in sorted(app_dir.rglob("*")):
        entries.append(item)
    with open(app_tgz, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for item in entries:
                name = item.relative_to(app_dir).as_posix()
                if item.is_dir():
                    add_entry(tar, name, is_dir=True, mode=DIR_MODE, mtime=mtime)
                else:
                    add_entry(tar, name, is_dir=False, mode=FILE_MODE,
                              data=item.read_bytes(), mtime=mtime)
    files = sum(1 for p in entries if p.is_file())
    log(f"app.tgz 已生成：{files} 个文件，{app_tgz.stat().st_size / 1048576:.1f} MB")
    return app_tgz


def inject_lxserver_artifacts(stage: Path, artifact_dir: Path | None,
                              allow_missing: bool = False) -> list[str]:
    """把 lxserver 预编译包塞进 repo/container/lxserver-artifact（离线安装用）。

    校验 SHA256 与 container/lxserver.version 一致，避免打进损坏/错版产物。
    """
    version_file = FPK_DIR.parent.parent / "container" / "lxserver.version"
    meta: dict[str, str] = {}
    if version_file.is_file():
        for line in version_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            meta[key.strip()] = value.strip()

    zip_name = meta.get("LXSERVER_ZIP_NAME", "")
    native_ver = meta.get("LXSERVER_NATIVE_VERSION", "")
    native_name = f"music-tag-native-linux-arm64-gnu-{native_ver}.tgz" if native_ver else ""

    candidates = []
    if artifact_dir is not None:
        candidates.append(artifact_dir)
    candidates.append(REPO_ROOT / "container" / "lxserver-artifact")
    candidates.append(FPK_DIR / ".build" / "cache")

    target = stage / "app" / "repo" / "container" / "lxserver-artifact"
    target.mkdir(parents=True, exist_ok=True)
    injected: list[str] = []

    def sha256_of(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    for name, expect_key in ((zip_name, "LXSERVER_SHA256"), (native_name, "LXSERVER_NATIVE_ARM64_SHA256")):
        if not name:
            continue
        found = next((c / name for c in candidates if (c / name).is_file()), None)
        if found is None:
            if allow_missing:
                log(f"WARN: 缺少 {name}，本次打包不含该离线产物（仅适合结构验证）")
                continue
            raise SystemExit(
                f"ERROR: 缺少 lxserver 预编译包 {name}；请先运行 scripts/update_lxserver.sh "
                f"或 --artifact-dir 指向已缓存的目录"
            )
        expect = meta.get(expect_key, "")
        actual = sha256_of(found)
        if expect and actual != expect:
            raise SystemExit(f"ERROR: {name} 校验和不符（{actual} != {expect}）")
        shutil.copyfile(found, target / name)
        injected.append(name)

    (target / ".placeholder").write_bytes(b"")
    log("已注入离线预编译包：" + "，".join(injected))
    return injected


def render_manifest(version: str, app_tgz_md5: str) -> str:
    template = (FPK_DIR / "manifest.in").read_text(encoding="utf-8")
    base = ".".join(str(version).split(".")[:3])
    text = template.replace("@VERSION@", base)
    if not text.endswith("\n"):
        text += "\n"
    return text + f"checksum              = {app_tgz_md5}\n"


def build_fpk(stage: Path, out_dir: Path, version: str, mtime: float) -> Path:
    app_tgz = stage / "app.tgz"
    md5 = hashlib.md5(app_tgz.read_bytes()).hexdigest()

    manifest_path = stage / "manifest"
    # newline="\n"：Windows 上 write_text 默认把 \n 转成 \r\n，manifest 的每个值
    # 都会带 \r（应用中心解析版本号/校验和时会取到脏值）
    manifest_path.write_text(render_manifest(version, md5), encoding="utf-8", newline="\n")

    # 外层条目：app.tgz 在前，其后按名字排序（与官方包一致）
    entries: list[Path] = []
    for name in ("cmd", "config", "wizard"):
        base = stage / name
        if base.is_dir():
            entries.append(base)  # 目录自身条目（与官方包一致）
            for item in sorted(base.rglob("*")):
                entries.append(item)
    for name in ("ICON.PNG", "ICON_256.PNG", "manifest"):
        item = stage / name
        if item.is_file():
            entries.append(item)
    entries.sort(key=lambda p: p.relative_to(stage).as_posix())

    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"fnmusic-ext-{version}.fpk"
    with open(final, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            add_entry(tar, "app.tgz", is_dir=False, mode=FILE_MODE,
                      data=app_tgz.read_bytes(), mtime=mtime)
            for item in entries:
                rel = item.relative_to(stage).as_posix()
                if item.is_dir():
                    add_entry(tar, rel, is_dir=True, mode=DIR_MODE, mtime=mtime)
                else:
                    mode = CMD_MODE if rel.startswith("cmd/") else FILE_MODE
                    add_entry(tar, rel, is_dir=False, mode=mode,
                              data=item.read_bytes(), mtime=mtime)

    digest = hashlib.sha256(final.read_bytes()).hexdigest()
    (out_dir / f"{final.name}.sha256").write_text(f"{digest}  {final.name}\n", encoding="utf-8")
    log(f"打包完成：{final}（{final.stat().st_size / 1048576:.1f} MB）")
    log(f"sha256：{digest}")
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨平台 fpk 打包器", allow_abbrev=False)
    parser.add_argument("--stage-only", metavar="DIR", help="只组装打包目录，不出包")
    parser.add_argument("--artifact-dir", metavar="DIR", help="lxserver 预编译包目录")
    parser.add_argument("--out", metavar="DIR", default=str(REPO_ROOT / "dist"), help="输出目录")
    parser.add_argument("--version", help="覆盖 VERSION 文件内容（调试用）")
    parser.add_argument("--stage", metavar="DIR", help="组装目录（默认 packaging/fpk/.build/fpk-offline）")
    parser.add_argument("--allow-missing-artifacts", action="store_true",
                        help="缺少 lxserver 预编译包时不中断（只用于结构验证/测试）")
    parser.add_argument("--alipay-png", metavar="FILE",
                        help="打赏二维码（CI 由 scripts/gen_alipay_qr.py 生成；缺省则不打进包）")
    parser.add_argument("--source-date-epoch", type=int, default=0, help="固定 mtime（可复现构建）")
    args = parser.parse_args(argv)

    version = (args.version or (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip().splitlines()[0]).strip()
    if not version:
        print("ERROR: 无法确定版本号", file=sys.stderr)
        return 1
    mtime = float(args.source_date_epoch or time.time())

    if args.stage_only:
        stage = Path(args.stage_only)
    elif args.stage:
        stage = Path(args.stage)
    else:
        stage = FPK_DIR / ".build" / "fpk-offline"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    copied = stage_app(REPO_ROOT, stage)
    log(f"仓库文件已铺入 stage：{copied} 个")
    alipay = Path(args.alipay_png) if args.alipay_png else (REPO_ROOT / "webui-service" / "static" / "alipay.png")
    if alipay.is_file():
        dest = stage / "app" / "repo" / "webui-service" / "static" / "alipay.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(alipay, dest)
        log(f"已注入打赏二维码：{alipay}")
    inject_lxserver_artifacts(stage, Path(args.artifact_dir) if args.artifact_dir else None,
                              allow_missing=args.allow_missing_artifacts)

    # 应用骨架
    for name in ("cmd", "config", "wizard"):
        src = FPK_DIR / name
        if src.is_dir():
            shutil.copytree(src, stage / name)
    for name in ("ICON.PNG", "ICON_256.PNG"):
        shutil.copyfile(FPK_DIR / name, stage / name)

    # 行尾归一化必须在压 app.tgz 之前：保证包内脚本是 LF
    normalize_text_tree(stage / "app")
    for name in ("cmd", "config", "wizard"):
        target = stage / name
        if target.is_dir():
            normalize_text_tree(target)

    pack_app_tgz(stage, mtime)

    os.chmod(stage / "cmd", DIR_MODE)
    for item in (stage / "cmd").iterdir():
        os.chmod(item, CMD_MODE)

    log(f"组装完成：{stage}（version={version}）")
    if args.stage_only:
        return 0

    build_fpk(stage, Path(args.out), version, mtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
