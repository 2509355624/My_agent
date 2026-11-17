# -*- coding: utf-8 -*-
"""从 state-guide.md 生成 state_rules.py。

这份脚本只做一件事：把原书按章节/档位切成**完整原文块**，不压缩、不改写。
生成后的 `state_rules.py` 是运行时唯一读到的原文表；`state-guide.md` 仍是
人类维护的源文件。测试会校验两边一致，防止以后只改一边。

用法：
    python tools/build_state_rules.py
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "skills" / "writing" / "00-state-guide" / "state-guide.md"
OUT = ROOT / "app" / "features" / "persona" / "state_rules.py"

SECTION_RE = re.compile(r"^##\s+(\d+)\.\s*(.+)$", re.M)
BLOCK_RE = re.compile(r"^###\s+(\d+)\.(\d+)\s*(.*)$", re.M)


def _clean(text: str) -> str:
    return text.strip("\n").rstrip() + "\n"


def _sections(md: str):
    marks = list(SECTION_RE.finditer(md))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(md)
        yield int(m.group(1)), m.group(2).strip(), md[m.start():end]


def _blocks(section_text: str):
    marks = list(BLOCK_RE.finditer(section_text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(section_text)
        yield int(m.group(1)), int(m.group(2)), m.group(3).strip(), section_text[m.start():end]


def _title(md_block: str) -> str:
    return md_block.splitlines()[0].lstrip("#").strip()


def _dev_parts(section8: str):
    """8.4 末尾的五处差异，按部位拆成独立原文行。"""
    marker = "**五处的差异**"
    pos = section8.find(marker)
    if pos < 0:
        return {}
    tail = section8[pos:]
    out = {}
    labels = {"口": "mouth", "胸": "nipple", "小穴": "pussy",
              "后庭": "anus", "皮肤": "skin"}
    for line in tail.splitlines():
        m = re.match(r"^\s*-\s*(口|胸|小穴|后庭|皮肤)[：:]\s*(.+)$", line)
        if m:
            out[labels[m.group(1)]] = line.rstrip() + "\n"
    return out


def _table_rows(text: str):
    """把 12.x 的 Markdown 表格行拆出来；保留每行原文。"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 2:
            continue
        if cells[0] in ("条件", "---") or set(cells[0]) <= set("-: "):
            continue
        rows.append((cells[0], line + "\n"))
    return rows


def build():
    md = SRC.read_text(encoding="utf-8")
    rows = []
    seen = set()

    def add(axis, key, lo, hi, text):
        if key in seen:
            raise SystemExit("重复 block key: " + key)
        seen.add(key)
        rows.append((axis, key, lo, hi, _clean(text)))

    for num, sec_title, sec in _sections(md):
        if num in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11):
            # 1/3-7/9-11 是数值档；2 是感情档；10/11 是枚举。
            for _, sub, sub_title, body in _blocks(sec):
                axis = {
                    1: "value", 2: "emotion_level", 3: "suspicion",
                    4: "arousal", 5: "excite", 6: "corrupt",
                    7: "submit", 9: "gauge", 10: "phase", 11: "contact",
                }[num]
                key = f"{axis}.{sub}"
                text = _title(body)
                # 档位上下限从标题里取，例如 `1.1 随时会走（-50 ~ -31）`。
                m = re.search(r"[（(]\s*(-?\d+)\s*~\s*(-?\d+)\s*[）)]", text)
                if m:
                    lo, hi = int(m.group(1)), int(m.group(2))
                else:
                    # 原书 6.1 / 7.1 写的是单点「（0）」，不是区间。
                    one = re.search(r"[（(]\s*(-?\d+)\s*[）)]", text)
                    lo = hi = int(one.group(1)) if one else None
                add(axis, key, lo, hi, body)

        elif num == 8:
            # 8.1-8.4 是四档公共行为；五处差异单独成行。
            for _, sub, sub_title, body in _blocks(sec):
                # 8.4 的正文里挂着「五处的差异」；那五行单独成块，
                # 不能再混进公共档，否则同一行会被注入两次。
                if sub == 4:
                    body = body.split("**五处的差异**", 1)[0].rstrip() + "\n"
                text = _title(body)
                m = re.search(r"[（(]\s*(-?\d+)\s*~\s*(-?\d+)\s*[）)]", text)
                lo = int(m.group(1)) if m else None
                hi = int(m.group(2)) if m else None
                add("dev", f"dev.{sub}", lo, hi, body)
            for part, line in _dev_parts(sec).items():
                add("dev.part", f"dev.part.{part}", None, None, line)

        elif num == 12:
            # 组合规则：每个表格行单独成块；12.7 的 bullet 整段保留。
            for _, sub, sub_title, body in _blocks(sec):
                if sub == 7:
                    add("combo", "combo.7", None, None, body)
                    continue
                for idx, (cond, line) in enumerate(_table_rows(body), start=1):
                    add("combo", f"combo.{sub}.{idx}", None, None, line)
        # 0/13 是用法说明，不进每轮注入；原书仍在。

    lines = [
        "# -*- coding: utf-8 -*-",
        '"""状态值全书的结构化原文表（由 tools/build_state_rules.py 生成）。',
        "",
        "**不要手改这里**：改原文请改 `state-guide.md`，再重新生成。",
        "每个 StateBlock 的 text 都是原书的一段完整原文；没有摘要、没有截断。",
        "选择哪些块由 `state_policy.py` 决定；这里只存事实。",
        '"""',
        "",
        "from dataclasses import dataclass",
        "from typing import Optional",
        "",
        "",
        "@dataclass(frozen=True)",
        "class StateBlock:",
        '    axis: str',
        '    key: str',
        '    lo: Optional[int]',
        '    hi: Optional[int]',
        '    text: str',
        "",
        "",
        "BLOCKS: tuple[StateBlock, ...] = (",
    ]
    for axis, key, lo, hi, text in rows:
        lines.append(
            "    StateBlock(%r, %r, %r, %r, %r),"
            % (axis, key, lo, hi, text)
        )
    lines += [")", "", "", "def by_key(key: str) -> StateBlock | None:", 
              "    for b in BLOCKS:", "        if b.key == key:", "            return b",
              "    return None", ""]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", OUT, "blocks=", len(rows))


if __name__ == "__main__":
    build()
