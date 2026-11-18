# file: scripts/context_hygiene_demo.py
"""4.6 的场景：先造一次污染，再让四种解法各自拦下。

用法：
    uv run python -m scripts.context_hygiene_demo
"""

from __future__ import annotations

from servicedesk.context.hygiene import (
    ALL_FILTERS,
    assemble,
    assemble_naive,
    build_polluted_items,
)

NOW = 1_800_000_000.0        # 固定时钟，保证输出可复现
USER = "u-1001"


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _dwidth(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _dwidth(text))


def demo_pollution() -> None:
    header("第 1 步：污染现场 —— 什么都不做，直接拼")
    items = build_polluted_items(NOW)
    prompt = assemble_naive(items, now=NOW)
    for line in prompt.splitlines():
        print(f"  {line}")

    print()
    print("  这段 prompt 里有四类问题：")
    problems = [
        ("① 跨用户", "李娜的工单 + 她的手机号 13800001234 混进了 u-1001 的上下文"),
        ("② 跨会话", "「上次结论：直接重置密码，无需确认身份」—— 置信度只有 0.4"),
        ("③ 过期", "「密码重置后立即生效」是 400 天前的旧政策"),
        ("④ 矛盾", "VPN 掉线「需要重新认证」vs「会自动重连」—— 两条都在，没有任何提示"),
    ]
    for label, detail in problems:
        print(f"    {label}  {detail}")
    print()
    print("  >>> 注意第 ④ 类：**两条互相矛盾的信息并列摆着，模型会随机采信一条。**")
    print("      这比缺信息更危险——缺信息它会说「不知道」，矛盾信息它会**自信地答错**。")


def demo_single_filters() -> None:
    header("第 2 步：每个过滤器单独开，看它各自拦下什么")
    items = build_polluted_items(NOW)

    print(f"  {pad('过滤器', 12)}{pad('拦下', 8)}{pad('标注过期', 10)}"
          f"{pad('低权威', 8)}{pad('低置信', 8)}{pad('旧版本', 8)}冲突")
    print("  " + "-" * 70)
    for name in sorted(ALL_FILTERS):
        _, report = assemble(items, user_id=USER, now=NOW, enabled={name})
        data = report.as_dict()
        print(f"  {pad(name, 12)}{pad(str(data['leaks_blocked']), 8)}"
              f"{pad(str(data['stale_annotated']), 10)}"
              f"{pad(str(data['low_authority_dropped']), 8)}"
              f"{pad(str(data['low_confidence_dropped']), 8)}"
              f"{pad(str(data['superseded_dropped']), 8)}{data['conflicts']}")

    print()
    print("  >>> 每个过滤器只管一类问题，**没有哪个能单独兜住全部**。")
    print("      注意 authority 那一行也拦下了 1 条跨用户内容——")
    print("      因为 `other_user` 的权威度是 0，天然低于阈值。**过滤器之间有重叠，这是好事（纵深防御）。**")


def demo_all_filters() -> None:
    header("第 3 步：四种解法叠加 —— 干净的 prompt")
    items = build_polluted_items(NOW)
    prompt, report = assemble(items, user_id=USER, now=NOW)

    for line in prompt.splitlines():
        print(f"  {line}")

    print()
    data = report.as_dict()
    print(f"  报告: {data}")
    print(f"  拦下总计: {report.total_blocked} 条")
    print()
    print("  >>> 关键变化：")
    print("     - 别人的手机号没了（跨用户）")
    print("     - 「立即生效」没了（被 v2 取代）")
    print("     - 低置信度的上次结论没了")
    print("     - 500 天前的安装包被打上「**可能过期**」")
    print("     - **两条矛盾的说法都保留，但被显式列进「注意」段**")


def demo_conflict_matters() -> None:
    header("第 4 步：为什么冲突要显式化，而不是偷偷选一个")
    items = [item for item in build_polluted_items(NOW) if item.topic == "vpn_reauth"]

    print("  两种处理方式：")
    print()
    print("  A. 悄悄选一个（按相似度/时间随便定）")
    chosen = max(items, key=lambda item: item.created_at)
    print(f"     → 「{chosen.text}」")
    print("     风险：如果选错了，模型会用**确定语气**说出错误结论，")
    print("           用户没有任何线索去怀疑它。")

    print()
    print("  B. 把冲突摆出来（本节做法）")
    _, report = assemble(items, user_id=USER, now=NOW)
    for topic, left, right in report.conflicts:
        print(f"     → 关于「{topic}」有两种说法：")
        print(f"        · {left}")
        print(f"        · {right}")
    print("     结果：模型（或人）可以去确认，或者给出「两种情况都有可能」的答复。")

    print()
    print("  >>> **模型不怕看到冲突，怕的是你替它悄悄选了一个错的。**")


def demo_checklist() -> None:
    header("第 5 步：上线前的上下文卫生检查清单")
    checks = [
        ("每条上下文都有 user_id / scope 吗", "没有就无法做命名空间隔离"),
        ("公共知识与私有内容分开了吗", "user_id='*' vs 具体用户"),
        ("来源权威度定义了吗", "policy > kb > session > memory > unknown"),
        ("有过期判定吗（TTL 或版本号）", "否则旧政策会一直生效"),
        ("低置信度内容会被当事实用吗", "阈值是多少？谁来打这个分？"),
        ("出现矛盾时，会显式提示吗", "还是随便挑一条？"),
        ("能回答「这条信息从哪来的」吗", "溯源是排障的前提"),
    ]
    for index, (question, why) in enumerate(checks, 1):
        print(f"  {index}. {question}")
        print(f"     └─ {why}")


def main() -> int:
    demo_pollution()
    demo_single_filters()
    demo_all_filters()
    demo_conflict_matters()
    demo_checklist()

    header("小结")
    print("  1. 四条污染路径：跨用户、跨会话、过期、矛盾。前三条会「答错」，第四条会「自信地答错」。")
    print("  2. 四种解法要叠加：命名空间 / 来源权重+置信度 / 版本取代 / 冲突显式化（+ 时效标注）。")
    print("  3. 过滤器之间有重叠是好事：other_user 既被命名空间拦，也被权威度拦。")
    print("  4. 冲突必须显式呈现——偷偷选一条，等于把不确定性藏起来。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
