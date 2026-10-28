"""build_offline.py 结构回归：不依赖 rsync / fnpack，直接校验产出的 .fpk。

官方包（2.10.2 release）反推出来的契约，任何一条破坏都会导致应用中心拒绝安装：
- 外层是 gzip(tar)，首条目为 app.tgz；
- manifest 的 version 取 VERSION、checksum 必须等于 md5(app.tgz)；
- app.tgz 内含 repo/ 与 ui/，且带一份 config/（运行期权限声明）；
- cmd 生命周期脚本可执行、wizard 初始音源含四个可选值；
- 不泄漏 .env / .git / tests / docs / packaging / test_*.py。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FPK_DIR = REPO_ROOT / "packaging" / "fpk"
APPNAME = "fnmusic-ext"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """跑一次完整打包（允许缺少 lxserver 离线产物），返回 (fpk 路径, 解包目录)。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_offline", FPK_DIR / "build_offline.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    # 组装目录与输出目录都指到临时目录，避免污染仓库 .build/
    out_dir = tmp_path_factory.mktemp("fpk-out")
    stage = tmp_path_factory.mktemp("fpk-stage") / "stage"
    rc = module.main(["--stage", str(stage), "--out", str(out_dir), "--allow-missing-artifacts"])
    assert rc == 0
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    fpk = out_dir / f"{APPNAME}-{version}.fpk"
    assert fpk.is_file(), list(out_dir.iterdir())

    unpacked = tmp_path_factory.mktemp("fpk-unpacked")
    with tarfile.open(fpk, "r:gz") as tar:
        names = tar.getnames()
        tar.extractall(unpacked)
    return fpk, unpacked, names


def test_outer_layout_and_checksum(built):
    fpk, unpacked, names = built
    assert names[0] == "app.tgz"
    for required in ("manifest", "cmd", "cmd/main", "config", "config/privilege",
                     "ICON.PNG", "ICON_256.PNG", "wizard", "wizard/install"):
        assert required in names, f"缺少 {required}"

    manifest = (unpacked / "manifest").read_text(encoding="utf-8")
    fields = {}
    for line in manifest.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert fields["appname"] == APPNAME
    assert fields["version"] == version
    assert fields["desktop_applaunchname"] == f"{APPNAME}.main"

    md5 = hashlib.md5((unpacked / "app.tgz").read_bytes()).hexdigest()
    assert fields["checksum"] == md5, "checksum 必须是 app.tgz 的 md5（官方包契约）"


def test_app_tgz_contents(built):
    _, unpacked, _ = built
    with gzip.open(unpacked / "app.tgz", "rb") as raw:
        with tarfile.open(fileobj=io.BytesIO(raw.read()), mode="r:") as tar:
            names = tar.getnames()
            modes = {m.name: m.mode for m in tar.getmembers()}

    assert "repo/VERSION" in names
    assert "ui/config" in names
    assert "config/privilege" in names, "官方包在 app.tgz 内也带一份 config/"
    # QQ 音源必须随包分发（运行期由 supervisord 拉起）
    for path in ("repo/qqmusic-service/app.py", "repo/qqmusic-service/qqmusic.py",
                 "repo/qqmusic-service/login.py", "repo/qqmusic-service/requirements.txt"):
        assert path in names, f"缺少 {path}"

    # 不泄漏开发文件与本地秘密
    for name in names:
        assert not name.endswith("/.env"), name
        assert "/.git/" not in name, name
        assert "/tests/" not in name, name
        assert "/docs/" not in name, name
        assert "/packaging/" not in name, name
        assert not name.split("/")[-1].startswith("test_"), name
        assert name != "repo/pytest.ini", name
    # 目录 0755、普通文件 0644
    assert modes["repo"] & 0o777 == 0o755
    assert modes["repo/install.sh"] & 0o777 == 0o644


def test_cmd_and_wizard(built):
    fpk, unpacked, _ = built
    # Windows 解包不保留执行位，权限必须直接读 tar 成员元数据
    with tarfile.open(fpk, "r:gz") as tar:
        modes = {m.name: m.mode for m in tar.getmembers() if m.isfile()}
    for name in ("_common", "install_init", "install_callback",
                 "upgrade_init", "upgrade_callback", "uninstall_init", "uninstall_callback",
                 "config_init", "config_callback", "main"):
        key = f"cmd/{name}"
        assert key in modes, name
        assert modes[key] & 0o111, f"{name} 需要执行位"

    callback = (unpacked / "cmd" / "install_callback").read_text(encoding="utf-8")
    assert "qqmusic" in callback, "安装回调白名单必须接受 qqmusic"

    steps = json.loads((unpacked / "wizard" / "install").read_text(encoding="utf-8"))
    options = []
    for item in steps[0]["items"]:
        if item.get("field") == "wizard_sources":
            assert item["label"] == "初始音源（四选一）"
            options = [o["value"] for o in item["options"]]
    assert options == ["musicdl", "musicbox", "lxmusic", "qqmusic"]


def test_sha256_sidecar(built):
    fpk, unpacked, _ = built
    sidecar = fpk.with_name(fpk.name + ".sha256")
    assert sidecar.is_file()
    expected = hashlib.sha256(fpk.read_bytes()).hexdigest()
    assert expected in sidecar.read_text(encoding="utf-8")
