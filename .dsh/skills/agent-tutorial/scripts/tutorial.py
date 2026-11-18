#!/usr/bin/env python3
"""agent-tutorial 断点续跑 CLI（仅标准库，零第三方依赖）。

用法：
  python scripts/tutorial.py init            # 建骨架（幂等，绝不覆盖已有文件）
  python scripts/tutorial.py status          # 进度总览 + 异常清单
  python scripts/tutorial.py next            # 打印下一节的任务卡
  python scripts/tutorial.py start 3.4       # 打印指定小节的任务卡
  python scripts/tutorial.py set 3.4 done    # 更新状态并同步 00-index.md 进度表
  python scripts/tutorial.py path 3.4        # 只打印该节应写入的文件路径
  python scripts/tutorial.py verify          # 一致性检查（有错误退出码 1）

全局：--root <dir> 覆盖自动定位的仓库根。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TOC_PATH = SKILL_DIR / "assets" / "toc.json"

STATUSES = ("planned", "drafted", "verified", "done")
GLYPH = {"planned": "○", "drafted": "◐", "verified": "◑", "done": "●"}
BEGIN = "<!-- PROGRESS:START -->"
END = "<!-- PROGRESS:END -->"
INDEX_NAME = "00-index.md"
APPENDIX = {
    "glossary.md": "# 术语表\n\n| 中文（English） | 一句话定义 | 出现小节 |\n|---|---|---|\n",
    "errors.md": "# 报错对照表\n\n| 报错原文 | 人话翻译 | 先查哪里 | 出现小节 |\n|---|---|---|---|\n",
    "versions.md": "# 版本矩阵\n\n| 小节 | 验证过的版本组合 | 验证日期 |\n|---|---|---|\n",
}


# ---------- 基础工具 ----------


def dwidth(text: str) -> int:
    """按终端显示宽度估算：CJK 字符占 2 列。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - dwidth(text))


def find_root(explicit: str | None) -> Path:
    """向上查找仓库根：优先含 tutorial/00-index.md，其次含 tutorial/ 或 .dsh/。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "tutorial" / INDEX_NAME).is_file():
            return candidate
    for candidate in (here, *here.parents):
        if (candidate / "tutorial").is_dir() or (candidate / ".dsh").is_dir():
            return candidate
    return here


def load_toc() -> dict:
    return json.loads(TOC_PATH.read_text(encoding="utf-8"))


def tutorial_dir(root: Path) -> Path:
    return root / "tutorial"


def state_path(root: Path) -> Path:
    return tutorial_dir(root) / ".state" / "progress.json"


def load_state(root: Path) -> dict:
    path = state_path(root)
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        data = {"version": 1, "updated": None, "sections": {}}
    if not isinstance(data.get("sections"), dict):
        data["sections"] = {}
    return data


def save_state(root: Path, state: dict) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    state["version"] = state.get("version", 1)
    state["updated"] = stamp
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def status_of(state: dict, section_id: str) -> str:
    entry = state["sections"].get(section_id) or {}
    value = str(entry.get("status", "planned"))
    return value if value in STATUSES else "planned"


def set_status(state: dict, section_id: str, value: str) -> None:
    entry = state["sections"].setdefault(section_id, {})
    entry["status"] = value
    entry["updated"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def section_path(root: Path, section: dict) -> Path:
    return tutorial_dir(root) / section["file"]


def read_frontmatter_status(path: Path) -> str | None:
    """读取章节文件 frontmatter 里的 status；读不到返回 None。"""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "status":
            cleaned = value.strip().strip("\"'")
            return cleaned or None
    return None


# ---------- 索引渲染 ----------


def render_table(toc: dict, state: dict) -> str:
    lines = [
        "| 编号 | 小节 | 状态 | 难度 | 时长 | 里程碑 | 文件 |",
        "|---|---|---|---|---|---|---|",
    ]
    for section in toc["sections"]:
        sid = section["id"]
        status = status_of(state, sid)
        stars = "★" * section["difficulty"] + "☆" * (5 - section["difficulty"])
        name = Path(section["file"]).name
        lines.append(
            f"| {sid} | {section['title']} | {GLYPH[status]} {status} | {stars} "
            f"| {section['minutes']}min | {section['milestone']} | [{name}]({section['file']}) |"
        )
    return "\n".join(lines)


def refresh_index(root: Path, toc: dict, state: dict) -> bool:
    """只替换标记区内的进度表；文件不存在或缺标记时返回 False。"""
    path = tutorial_dir(root) / INDEX_NAME
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8-sig")
    if BEGIN not in text or END not in text:
        return False
    head, _, rest = text.partition(BEGIN)
    _, _, tail = rest.partition(END)
    path.write_text(f"{head}{BEGIN}\n{render_table(toc, state)}\n{END}{tail}", encoding="utf-8")
    return True


INDEX_TEMPLATE = """# {title}

> 人读入口就是本文件；机器读入口是 `.state/progress.json`。
> 标记区内的进度表由脚本同步，**不要手改**，手改会在下次 `set` 时被覆盖。

## 怎么续跑

```bash
python .dsh/skills/agent-tutorial/scripts/tutorial.py status      # 看进度与下一节
python .dsh/skills/agent-tutorial/scripts/tutorial.py start 3.4   # 摘出某节任务卡
python .dsh/skills/agent-tutorial/scripts/tutorial.py set 3.4 done
```

## 进度

{begin}
{end}

## 状态含义

| 状态 | 含义 |
|---|---|
| ○ planned | 只有标题，没有正文 |
| ◐ drafted | 正文写完，但代码没跑通 / 验收没验过 |
| ◑ verified | 代码跑通，真实输出已粘回正文 |
| ● done | 通过 16 项自检门禁 |

## 阶段范围与能力矩阵

<!-- 待补：每阶段 In-Scope / Out-Scope / 关键难点、C1–C7 覆盖矩阵、两条学习路线 -->

## 学习路线

- 最短可上线路径：1.1 → 1.3 → 1.5 → 1.6 → 1.7 → 1.8 → 2.1 → 2.2 → 2.4 → 2.7 → 3.1 → 3.2 → 3.4 → 3.5 → 3.7 → 5.1 → 5.3 → 5.4 → 5.6 → 5.7
- 完整体系路径：全部 {total} 节
"""


# ---------- 命令 ----------


def cmd_init(root: Path, toc: dict) -> int:
    created: list[str] = []
    tut = tutorial_dir(root)

    for stage in toc["stages"]:
        target = tut / stage["dir"]
        if not target.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            created.append(str(target.relative_to(root)))
    (tut / "appendix").mkdir(parents=True, exist_ok=True)
    (tut / ".state").mkdir(parents=True, exist_ok=True)

    for name, body in APPENDIX.items():
        target = tut / "appendix" / name
        if not target.is_file():
            target.write_text(body, encoding="utf-8")
            created.append(str(target.relative_to(root)))

    index = tut / INDEX_NAME
    if not index.is_file():
        index.write_text(
            INDEX_TEMPLATE.format(
                title=toc["title"], begin=BEGIN, end=END, total=len(toc["sections"])
            ),
            encoding="utf-8",
        )
        created.append(str(index.relative_to(root)))

    state = load_state(root)
    for section in toc["sections"]:
        state["sections"].setdefault(section["id"], {"status": "planned", "updated": None})
    save_state(root, state)
    refresh_index(root, toc, state)

    print(f"root: {root}")
    if created:
        print("新建：")
        for item in created:
            print(f"  + {item}")
    else:
        print("骨架已存在，无需新建（进度未改动）")
    print(f"进度文件: {state_path(root).relative_to(root)}")
    return 0


def collect(root: Path, toc: dict, state: dict) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    problems: list[str] = []
    for section in toc["sections"]:
        sid = section["id"]
        status = status_of(state, sid)
        path = section_path(root, section)
        fm = read_frontmatter_status(path)
        rows.append(
            {"id": sid, "section": section, "status": status, "exists": path.is_file(), "fm": fm}
        )
        if status in ("verified", "done") and not path.is_file():
            problems.append(f"[错误] {sid} 状态为 {status}，但文件不存在：{section['file']}")
        if path.is_file() and fm and fm != status:
            problems.append(f"[警告] {sid} frontmatter({fm}) 与 progress({status}) 不一致")
        if status == "done":
            for dep in section["depends_on"]:
                if status_of(state, dep) != "done":
                    problems.append(f"[警告] {sid} 已 done，但前置 {dep} 未 done")
    return rows, problems


def next_section_id(rows: list[dict]) -> str | None:
    for row in rows:
        if row["status"] != "done":
            return row["id"]
    return None


def cmd_status(root: Path, toc: dict, state: dict) -> int:
    rows, problems = collect(root, toc, state)
    total = len(rows)
    done = sum(1 for row in rows if row["status"] == "done")
    pct = (done * 100 // total) if total else 0
    nxt = next_section_id(rows)

    print("== agent-tutorial status ==")
    print(f"root    : {root}")
    print(f"progress: {state_path(root).relative_to(root)}"
          f"{'' if state_path(root).is_file() else '  (缺失，先跑 init)'}")
    print(f"totals  : {done}/{total} done ({pct}%)")
    if nxt is None:
        print("next    : 全部完成 🎉")
    else:
        title = next(r["section"]["title"] for r in rows if r["id"] == nxt)
        print(f"next    : {nxt} {title}")
    print()
    print(f"{pad('编号', 6)}{pad('状态', 12)}{pad('难度', 9)}文件")
    print("-" * 78)
    for row in rows:
        flag = "" if row["exists"] else "  (缺失)"
        print(
            f"{pad(row['id'], 6)}{pad(GLYPH[row['status']] + ' ' + row['status'], 12)}"
            f"{pad('★' * row['section']['difficulty'], 9)}{row['section']['file']}{flag}"
        )
    if problems:
        print()
        print("== 异常 ==")
        for item in problems:
            print(item)
    return 0


def print_card(root: Path, toc: dict, state: dict, section_id: str) -> int:
    section = next((s for s in toc["sections"] if s["id"] == section_id), None)
    if section is None:
        print(f"未知小节：{section_id}", file=sys.stderr)
        return 2
    path = section_path(root, section)
    status = status_of(state, section_id)
    deps = ", ".join(
        f"{dep}({status_of(state, dep)})" for dep in section["depends_on"]
    ) or "—"

    print(f"== {section_id} {section['title']} ==")
    print(f"状态   : {GLYPH[status]} {status}")
    print(f"难度   : {'★' * section['difficulty']}{'☆' * (5 - section['difficulty'])}"
          f"   时长: {section['minutes']}min   里程碑: {section['milestone']}")
    print(f"前置   : {deps}")
    print(f"文件   : {path.relative_to(root)}"
          f"{'' if path.is_file() else '   ← 尚未生成'}")
    print(f"产出   : {', '.join(section['produces'])}")
    print(f"验收   : {section['verifies']}")
    return 0


def cmd_set(root: Path, toc: dict, state: dict, section_id: str, value: str) -> int:
    if value not in STATUSES:
        print(f"状态必须是 {STATUSES} 之一，收到 {value!r}", file=sys.stderr)
        return 2
    if not any(s["id"] == section_id for s in toc["sections"]):
        print(f"未知小节：{section_id}", file=sys.stderr)
        return 2
    set_status(state, section_id, value)
    save_state(root, state)
    synced = refresh_index(root, toc, state)
    print(f"{section_id} -> {value}")
    print(f"progress.json 已更新")
    print(f"00-index.md  {'已同步' if synced else '未同步（缺标记区或缺文件）'}")
    return 0


def cmd_verify(root: Path, toc: dict, state: dict) -> int:
    rows, problems = collect(root, toc, state)
    errors = [p for p in problems if p.startswith("[错误]")]
    warns = [p for p in problems if p.startswith("[警告]")]
    print(f"检查 {len(rows)} 节：{len(errors)} 错误，{len(warns)} 警告")
    for item in problems:
        print(item)
    if not problems:
        print("一致 ✓")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tutorial.py", description="教程断点续跑 CLI")
    parser.add_argument("--root", default=None, help="仓库根目录（默认自动向上查找）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="建骨架，幂等")
    sub.add_parser("status", help="进度总览")
    sub.add_parser("next", help="下一节")
    sub.add_parser("verify", help="一致性检查")
    p_start = sub.add_parser("start", help="打印某节任务卡")
    p_start.add_argument("section")
    p_set = sub.add_parser("set", help="更新某节状态")
    p_set.add_argument("section")
    p_set.add_argument("status")
    p_path = sub.add_parser("path", help="打印某节文件路径")
    p_path.add_argument("section")

    args = parser.parse_args(argv)
    root = find_root(args.root)
    toc = load_toc()
    state = load_state(root)

    if args.command == "init":
        return cmd_init(root, toc)
    if args.command == "status":
        return cmd_status(root, toc, state)
    if args.command == "verify":
        return cmd_verify(root, toc, state)
    if args.command == "next":
        rows, _ = collect(root, toc, state)
        nxt = next_section_id(rows)
        if nxt is None:
            print("全部完成 🎉")
            return 0
        return print_card(root, toc, state, nxt)
    if args.command == "start":
        return print_card(root, toc, state, args.section)
    if args.command == "set":
        return cmd_set(root, toc, state, args.section, args.status)
    if args.command == "path":
        section = next((s for s in toc["sections"] if s["id"] == args.section), None)
        if section is None:
            print(f"未知小节：{args.section}", file=sys.stderr)
            return 2
        print(section_path(root, section))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
