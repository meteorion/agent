# file: scripts/long_term_memory_demo.py
"""4.5 的场景：三个月前的工单能被召回，过期记忆不被召回。

用法：
    uv run python -m scripts.long_term_memory_demo
"""

from __future__ import annotations

import time

from servicedesk.memory.long_term import (
    DAY,
    KIND_EPISODIC,
    KIND_SEMANTIC,
    MemoryStore,
)


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _dwidth(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _dwidth(text))


def build_store(now: float) -> MemoryStore:
    store = MemoryStore()

    # 语义记忆：用户偏好（几乎总要带上）
    store.remember(
        KIND_SEMANTIC, "张伟偏好邮件联系，不喜欢电话打扰",
        user_id="u-1001", now=now - 3 * DAY, importance=0.9, tags=("preference",),
    )
    # 情景记忆：三个月前的同题工单（本节验收点）
    store.remember(
        KIND_EPISODIC, "工单 T-1001：VPN 认证失败，最终通过重置密码解决",
        user_id="u-1001", now=now - 90 * DAY, importance=0.8, tags=("vpn", "ticket"),
    )
    # 情景记忆：一个月前的无关工单
    store.remember(
        KIND_EPISODIC, "工单 T-0588：打印机卡纸，换纸盒解决",
        user_id="u-1001", now=now - 30 * DAY, importance=0.4, tags=("printer",),
    )
    # 情景记忆：一小时前的临时信息（十分钟后就该忘）
    store.remember(
        KIND_EPISODIC, "临时验证码 123456，有效期十分钟",
        user_id="u-1001", now=now - 3600, importance=0.2, ttl_s=600, tags=("temp",),
    )
    return store


def demo_recall(store: MemoryStore, now: float) -> None:
    header("第 1 步：新会话提问 —— 三个月前的工单能不能召回")
    query = "VPN 认证失败怎么办？"
    print(f"  查询: {query}   用户: u-1001")
    print()

    hits = store.recall(query, user_id="u-1001", now=now, top_k=5)
    print(f"  {pad('类型', 12)}{pad('相似度', 10)}{pad('重要性', 10)}{pad('新鲜度', 10)}"
          f"{pad('总分', 10)}内容")
    print("  " + "-" * 92)
    for row in hits:
        item = row.item
        kind = "语义" if item.kind == KIND_SEMANTIC else "情景"
        print(f"  {pad(kind, 12)}{pad(f'{row.similarity:.3f}', 10)}"
              f"{pad(f'{row.importance:.3f}', 10)}{pad(f'{row.recency:.3f}', 10)}"
              f"{pad(f'{row.score:.4f}', 10)}{item.text[:26]}")

    print()
    print("  拼给模型的上下文块：")
    for line in store.context_block(hits, now=now).splitlines():
        print(f"    {line}")

    print()
    print("  >>> 三个月前那条排第 1：它是**内容最像**的（相似度 0.38，其它都在 0.1 以下）。")
    print("      『越旧越不重要』没有压过『内容最相关』——这正是我们想要的取舍。")

    stats = store.stats(now)
    print()
    print(f"  库内统计: {stats}")
    print("  >>> 注意 expired=1 但召回只有 3 条：那条临时验证码**没有被召回**。")


def demo_forgetting(store: MemoryStore, now: float) -> None:
    header("第 2 步：遗忘 —— 三种必须有的能力")

    print("  ① TTL 过期（时间到了自动失效）")
    hits = store.recall("临时验证码", user_id="u-1001", now=now, top_k=5)
    found_temp = any("验证码" in row.item.text for row in hits)
    print(f"     查『临时验证码』: 返回 {len(hits)} 条，其中含验证码: {found_temp}")
    print("     >>> 它的 TTL 是 600 秒，已经过了一小时，召回时被过滤掉了。")

    cleaned = store.forget_expired(now)
    print(f"     后台清理: 删掉 {cleaned} 条过期记忆")
    print(f"     清理后统计: {store.stats(now)}")

    print()
    print("  ② 显式删除（用户说『忘掉这件事』）")
    printer = store.recall("打印机卡纸", user_id="u-1001", now=now, top_k=3)
    target = next((row for row in printer if "打印机" in row.item.text), None)
    if target:
        print(f"     删除前：{target.item.text[:30]}")
        ok = store.forget(target.item.id)
        print(f"     forget(id) → {ok}")
        after = store.recall("打印机卡纸", user_id="u-1001", now=now, top_k=3)
        still = any("打印机" in row.item.text for row in after)
        print(f"     删除后还能召回吗: {still}")

    print()
    print("  ③ 用户级删除（被遗忘权 / GDPR）")
    removed = store.forget_user("u-1001")
    print(f"     forget_user('u-1001') → 删除 {removed} 条")
    remaining = store.recall("VPN", user_id="u-1001", now=now, top_k=5)
    print(f"     该用户的剩余可召回记忆: {len(remaining)} 条")
    print()
    print("  >>> 遗忘不是「删数据」那么简单：**没有遗忘的记忆库会越来越差**——")
    print("      过期偏好、一次性验证码、已解决的工单都会变成召回噪声。")


def demo_decay() -> None:
    header("第 3 步：衰减 —— 同样相关，新旧差多少")
    now = time.time()

    rows = []
    for age_days, importance in [(1, 0.5), (30, 0.5), (90, 0.5), (365, 0.5),
                                 (90, 0.9), (365, 0.9)]:
        store = MemoryStore()
        store.remember(
            KIND_EPISODIC, "VPN 认证失败，重置密码后恢复",
            user_id="u", now=now - age_days * DAY, importance=importance,
        )
        hit = store.recall("VPN 认证失败", user_id="u", now=now, top_k=1)[0]
        rows.append((age_days, importance, hit.similarity, hit.importance, hit.recency, hit.score))

    print(f"  {pad('年龄(天)', 12)}{pad('原始重要性', 14)}{pad('相似度', 10)}"
          f"{pad('衰减后重要性', 16)}{pad('新鲜度', 10)}总分")
    print("  " + "-" * 82)
    for age, importance, sim, decayed, recency, score in rows:
        print(f"  {pad(str(age), 12)}{pad(str(importance), 14)}{pad(f'{sim:.3f}', 10)}"
              f"{pad(f'{decayed:.3f}', 16)}{pad(f'{recency:.3f}', 10)}{score:.4f}")

    print()
    print("  >>> 三种力量在拉扯：**相似度（内容像不像）、重要性（重不重要）、新鲜度（多久了）**。")
    print("      权重 0.6/0.25/0.15 是本节的选择；换成客服场景可能把新鲜度权重调高，")
    print("      换成长期健康档案则应该把重要性调高、新鲜度调低。")


def demo_two_kinds() -> None:
    header("第 4 步：两类记忆的分工")
    now = time.time()
    store = build_store(now)

    vpn_hits = store.recall("VPN 认证失败", user_id="u-1001", now=now, top_k=5)
    printer_hits = store.recall("打印机卡纸", user_id="u-1001", now=now, top_k=5)

    print("  问 VPN:      ", [("语义" if h.item.kind == KIND_SEMANTIC else "情景", h.item.text[:14]) for h in vpn_hits])
    print("  问打印机:    ", [("语义" if h.item.kind == KIND_SEMANTIC else "情景", h.item.text[:14]) for h in printer_hits])
    print()
    print("  >>> 语义记忆（偏好）两次都在：它**与问题无关，但总该带上**；")
    print("      情景记忆按相似度换：问 VPN 召回 VPN 工单，问打印机召回打印机工单。")
    print()
    print("  所以『合并两类记忆』不是简单拼接，而是：")
    print("    - 语义记忆：按重要性取 Top-K（几乎恒置）")
    print("    - 情景记忆：按相似度 + 新鲜度取 Top-K")
    print("    - 一起塞进上下文，用 [偏好]/[历史] 标出来，让模型知道两者的分量不同")


def main() -> int:
    now = time.time()
    store = build_store(now)

    demo_recall(store, now)
    demo_forgetting(store, now)
    demo_decay()
    demo_two_kinds()

    header("小结")
    print("  1. 语义记忆（偏好）与情景记忆（事件）**检索逻辑不同**：前者恒置，后者按相似度。")
    print("  2. 三个月前的工单能排第 1：内容相关性压过了'越旧越不重要'。")
    print("  3. 遗忘三件套：TTL / 显式删除 / 用户级删除；过期的不会被召回。")
    print("  4. 打分 = 0.6·相似度 + 0.25·重要性 + 0.15·新鲜度，权重随业务调。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
