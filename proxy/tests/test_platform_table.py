"""install.sh 内嵌 SOURCE_PLATFORM_TABLE 的一致性校验（音源三选一：musicbox / qqmusic / lx），
以及交互菜单精选平台与 ★ 行的一致性。"""
import re
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]

INSTALL_ROW = re.compile(
    r'^(\d+)\|([a-z]+)\|([a-z0-9]+)\|([A-Za-z0-9]+)\|(.+)\|([01])$'
)


def _install_table():
    text = (BASE / 'install.sh').read_text(encoding='utf-8')
    m = re.search(r"SOURCE_PLATFORM_TABLE='\n(.*?)'\n", text, re.S)
    assert m, 'install.sh 中未找到 SOURCE_PLATFORM_TABLE'
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        mm = INSTALL_ROW.match(line)
        assert mm, f'install.sh 平台表行格式异常: {line!r}'
        rows.append((
            int(mm.group(1)),
            mm.group(2),
            mm.group(3),
            mm.group(4),
            mm.group(5),
            int(mm.group(6)),
        ))
    return rows


def test_table_invariants():
    rows = _install_table()
    ids = [no for no, *_rest in rows]
    # 编号从 1 起、严格递增、唯一
    assert ids[0] == 1, '首行编号须为 1（musicbox）'
    assert ids == sorted(ids), '编号须递增'
    assert len(set(ids)) == len(ids), '编号不得重复'
    providers = {p for _n, p, *_rest in rows}
    assert providers == {'musicbox', 'lx'}
    pairs = [(p, s) for _n, p, s, *_rest in rows]
    assert len(set(pairs)) == len(pairs), '同一提供者下短名不得重复'
    lx = [r for r in rows if r[1] == 'lx']
    box = [r for r in rows if r[1] == 'musicbox']
    assert box == [(1, 'musicbox', 'netease', 'musicbox', '网易云音乐', 1)]
    for _no, _p, short, full, _label, _star in lx:
        assert short == full
    # lx 平台固定占 59–63（qqmusic 走整源名字 token，不进编号表）
    assert [n for n, *_r in lx] == list(range(59, 64))


def test_menu_curated_matches_star_rows():
    """向导列出的编号必须与表中 ★ 行的全局 ID 一致（不再重映射）。"""
    starred = [(n, p, s) for n, p, s, _f, _l, star in _install_table() if star]
    assert starred, '缺少精选标记'

    text = (BASE / 'install.sh').read_text(encoding='utf-8')
    start = text.index("SOURCE_PLATFORM_TABLE='")
    end = text.index('while [ $# -gt 0 ]')
    block = text[start:end]
    result = subprocess.run(
        ['bash', '-c', 'set -euo pipefail\n' + block + 'print_featured_source_menu\n'],
        text=True, capture_output=True, check=True)
    menu_ids = [int(n) for n in re.findall(r'^\s*(\d+)\)', result.stdout, re.M)]
    assert menu_ids == [n for n, _p, _s in starred], '菜单精选编号须与 ★ 行全局 ID 一致'
    assert menu_ids[0] == 1
    assert 59 in menu_ids and 62 in menu_ids
    assert 63 not in menu_ids   # tx 仅搜索、非精选
