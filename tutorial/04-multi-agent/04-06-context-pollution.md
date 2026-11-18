---
section: "4.6"
title: "上下文污染的根因与四种解法"
stage: 4
status: done
depth: L2
difficulty: 4
minutes: 100
milestone: M4
depends_on: ["4.5"]
produces: ["context/hygiene.py"]
verifies: "构造一次污染并演示四种解法各自拦下"
part: null
updated: "2026-11-18"
---

# 4.6 上下文污染的根因与四种解法

## 0. 本节导航

**本节在链路中的位置**：4.4 讲"怎么裁"、4.5 讲"怎么记"，本节讲"**怎么防毒**"。
裁得再多、记得再全，只要塞进 prompt 的东西本身是错的，模型就会一本正经地答错。

**本节你将获得**

1. 四条污染路径：**跨用户 / 跨会话 / 过期 / 矛盾**。
2. 实测：什么都不做时，一段 prompt 里同时出现别人的手机号、旧政策、两条互相矛盾的说法。
3. 四种解法：**命名空间隔离 / 来源权重+置信度 / 版本取代 / 冲突显式化**（外加时效标注）。
4. 一个关键判断：**每个过滤器单独开都不够**（有测试固定这一点）。
5. 一个反直觉结论：**模型不怕看到冲突，怕的是你替它悄悄选了一个错的。**

**前置检查清单**

- 4.5 的 `uv run python -m scripts.long_term_memory_demo` 能跑通。
- 1.x 的"证据门"（1.8）已了解——本节是同一套思路在上下文层的应用。

**难度**：★★★★☆ ｜ **预计时长**：100 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

先看一段真实的"裸拼"上下文（本节 demo 的第一屏）：

```
- 密码重置后 5 分钟内生效，期间请勿重复提交。
- 密码重置后立即生效。
- 李娜（E-3102）的工单：她的手机号 13800001234，VPN 已解封。
- 上次结论：直接重置密码即可，无需确认身份。
- 旧版客户端请使用 2024 版安装包。
- VPN 掉线后需要重新认证，通常会要求输入动态口令。
- VPN 掉线后会自动重连，不会要求重新认证。
- 网上帖子说：把 DNS 改成 8.8.8.8 就能解决 VPN 认证问题。
```

**四类问题同时存在**：

| # | 类型 | 具体问题 | 后果 |
|---|---|---|---|
| ① | **跨用户** | 李娜的手机号 13800001234 | 数据泄漏（合规事故） |
| ② | **跨会话** | "上次结论：无需确认身份"（置信度 0.4） | 把失败尝试当事实 |
| ③ | **过期** | "立即生效"是 400 天前的旧政策 | 给用户错误指引 |
| ④ | **矛盾** | "需要重新认证" vs "会自动重连" | **自信地答错** |

**第 ④ 类最危险**，因为它不报错：

- 缺信息 → 模型会说"我不知道"（可接受）
- 矛盾信息 → 模型会**随机采信一条，并用确定语气说出来**（不可接受）

---

## 2. 核心概念与架构设计

### 2.1 类比：给厨师备菜

- **裸拼** = 把所有食材倒进一个盆，包括过期的、别人点的、来路不明的
- **命名空间隔离** = 只拿"这一桌"的食材
- **来源权重** = 来路不明的不要
- **版本取代** = 过期食材扔掉
- **冲突显式化** = 两个菜谱说法不一致时，**告诉厨师"这里有两版，请确认"**，而不是替他选

> **这个类比在哪失效**：厨师尝一口就知道食材坏没坏；
> 模型**尝不出来**——它对"这条信息是 400 天前的"没有任何直觉，除非你标出来。

### 2.2 四种解法

| 解法 | 拦什么 | 实现 |
|---|---|---|
| **① 命名空间隔离** | 跨用户 | `user_id not in (current, "*")` → 丢弃 |
| **② 来源权重 + 置信度** | 低权威、失败结论 | `weight < 0.35` 或 `confidence < 0.5` → 丢弃 |
| **③ 版本取代** | 过期政策 | 同 topic 只保留 `version` 最大的 |
| **④ 冲突显式化** | 矛盾说法 | 同 topic 不同 claim → **两条都留，另加"注意"段** |

**外加一件事：时效标注**（不算解法，算"让模型看得见时间"）。

### 2.3 实测：每个过滤器单独开

| 过滤器 | 拦下跨用户 | 标注过期 | 低权威 | 低置信 | 旧版本 | 冲突 |
|---|---|---|---|---|---|---|
| authority | 0 | 0 | **2** | 0 | 0 | 0 |
| confidence | 0 | 0 | 0 | **1** | 0 | 0 |
| conflict | 0 | 0 | 0 | 0 | 0 | **2** |
| namespace | **1** | 0 | 0 | 0 | 0 | 0 |
| stale | 0 | **2** | 0 | 0 | 0 | 0 |
| version | 0 | 0 | 0 | 0 | **1** | 0 |

**两个值得注意的地方**：

1. **`authority` 那一行也拦下了跨用户内容**——因为 `other_user` 的权威度是 0，天然低于阈值。
   **过滤器之间有重叠是好事**（纵深防御），但演示"各拦各的"时要说清楚。
2. **`conflict` 单独开时报 2 条冲突**（`reset_policy` + `vpn_reauth`），
   因为旧政策还没被版本过滤掉。**叠加 version 后只剩 1 条**——两个过滤器互补。

### 2.4 叠加后的干净结果

```
- [policy·2 天前] 密码重置后 5 分钟内生效，期间请勿重复提交。
- [kb·500 天前·**可能过期**] 旧版客户端请使用 2024 版安装包。
- [kb·30 天前] VPN 掉线后需要重新认证，通常会要求输入动态口令。
- [kb·10 天前] VPN 掉线后会自动重连，不会要求重新认证。

**注意：以下信息互相矛盾，请以最新政策为准并向用户确认：**
  - 关于「vpn_reauth」：一种说法是「…需要重新认证…」，另一种是「…会自动重连…」
```

**报告**：

```
{'leaks_blocked': 1, 'stale_annotated': 1, 'low_authority_dropped': 1,
 'low_confidence_dropped': 1, 'superseded_dropped': 1, 'conflicts': 1, 'kept': 4}
拦下总计: 4 条
```

### 2.5 适用范围（边界）

| 场景 | 需要哪几条 |
|---|---|
| 单用户本地工具 | ④（冲突）就够 |
| 多租户 SaaS | ①③④ 必须，② 建议 |
| 有合规要求（医疗/金融） | 全部 + 审计日志 |
| 纯闲聊 Agent | 几乎不需要 |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 |
|---|---|
| Python | `3.12.13` |

**验证**

```powershell
uv run python -c "from servicedesk.context.hygiene import build_polluted_items; print(len(build_polluted_items(0)))"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/context/hygiene.py` | **主角**：条目模型 + 四个过滤器 + 报告 |
| `scripts/context_hygiene_demo.py` | 五步演示 |
| `tests/test_context_hygiene.py` | 15 个断言 |

### 4.1 条目模型

```python
@dataclass
class ContextItem:
    id: str
    text: str
    source: str                 # policy / kb / session / memory / other_user / unknown
    user_id: str                # 这条信息属于谁；"*" 表示公共知识
    created_at: float
    topic: str = ""             # 用于冲突检测：同一话题的不同说法
    confidence: float = 1.0     # 写入方自评的置信度
    version: int = 1            # 政策/文档版本，越大越新
```

**`user_id="*"` 是本节的第一个坑**（§8.1）：政策与知识库不属于任何个人，
如果隔离规则写成 `item.user_id != user_id` 就丢，**公共知识会被全部误杀**。

### 4.2 来源权重

```python
SOURCE_WEIGHT: dict[str, float] = {
    "policy": 1.0,      # 公司政策，最高
    "kb": 0.8,          # 知识库
    "session": 0.5,     # 上一轮对话
    "memory": 0.4,      # 长期记忆
    "other_user": 0.0,  # 别人的东西，根本不该出现
}

MIN_WEIGHT = 0.35
MIN_CONFIDENCE = 0.5
```

### 4.3 四道过滤（按顺序）

```python
    for item in items:
        # 公共知识（policy/kb）user_id 为 "*"，不算泄漏；别人的私有内容才算
        if "namespace" in active and (
            item.source == "other_user" or item.user_id not in (user_id, SHARED_USER)
        ):
            report.leaks_blocked.append(...)
            continue
        if "authority" in active and item.weight < min_weight:
            report.low_authority_dropped.append(...)
            continue
        if "confidence" in active and item.confidence < MIN_CONFIDENCE:
            report.low_confidence_dropped.append(...)
            continue
        survivors.append(item)
```

### 4.4 冲突显式化

```python
    for topic, group in by_topic.items() if "conflict" in active else []:
        live = [item for item in group if item.id not in superseded_ids]
        claims = {_claim_of(item.text) for item in live}
        if len(claims) > 1:
            first, second = live[0], live[1]
            report.conflicts.append((topic, first.text[:36], second.text[:36]))
```

### 4.5 运行

```powershell
uv run python -m scripts.context_hygiene_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么过滤要在"组装"这一层做

因为**组装是唯一能同时看到所有来源的地方**。

- 在召回层做：只看得到记忆，看不到知识库
- 在 prompt 拼接层做：只看得到字符串，看不到 `user_id` 与 `source`

**组装层是所有元信息最后一次齐全的地方**——过了这一层，它们就变成一段纯文本了。

### 5.2 为什么冲突检测要"两条都留"

看第 4 步的对比：

**A. 悄悄选一个**（按时间/相似度随便定）→ 选中了"会自动重连"

> 风险：如果选错了，模型会用**确定语气**说出错误结论，用户没有任何线索去怀疑。

**B. 把冲突摆出来**：

```
**注意：以下信息互相矛盾，请以最新政策为准并向用户确认：**
  - 关于「vpn_reauth」：一种说法是「…」，另一种是「…」
```

> 结果：模型（或人）可以去确认，或者给出"两种情况都有可能"的答复。

**这句话值得记住**：**模型不怕看到冲突，怕的是你替它悄悄选了一个错的。**

### 5.3 `_claim_of` 为什么用"数字+单位"

```python
_CLAIM_RE = re.compile(r"[0-9]+\s*(?:分钟|小时|天|秒)")


def _claim_of(text: str) -> str:
    numbers = _CLAIM_RE.findall(text)
    return "|".join(numbers) if numbers else text.strip()
```

**这是一个粗糙的代理**：同一话题下，"5 分钟"和"立即"是两种说法，靠数字差异能区分。

**但它有明显的漏判**："需要重新认证"和"会自动重连"都**没有数字**，
所以会退化成"整句文本比较"——两句不同 → 判为冲突。

> **这是本节的已知局限**（§10）：真实系统应该用结构化表示（三元组、JSON 字段），
> 而不是比字符串。这里用文本比较是为了 demo 能自包含。

### 5.4 为什么"每个单独开都不够"要用测试固定

```python
def test_each_filter_alone_is_insufficient(items) -> None:
    for name in ALL_FILTERS:
        prompt, _ = assemble(items, user_id=USER, now=NOW, enabled={name})
        clean = (…四项检查全过…)
        assert not clean, f"`{name}` 单独开竟然清干净了？检查样例是否失效"
```

**这条测试有两个作用**：

1. 现在：证明"必须叠加"
2. 将来：如果有人改动让某个过滤器变得过强（或样例失效），它会红

**注意断言写的是 `assert not clean`** —— 这是"反向断言"：
**验证的是"不够干净"，而不是"够干净"。**

### 5.5 时效标注为什么不算"解法"

因为它**不丢弃任何东西**，只是让模型看见时间：

```
[kb·500 天前·**可能过期**] 旧版客户端请使用 2024 版安装包。
```

**为什么还需要它**：有些旧信息不能删（比如"2024 版安装包"对老客户端仍有效），
但**必须让模型知道它是旧的**。

**丢弃是"二选一"，标注是"把判断权交回去"。** 两者都要有。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：只做命名空间隔离

数据（§2.3）：`namespace` 单独开只拦 1 条跨用户内容，其余三样全在。

**结果**：别人的手机号没了，但旧政策、低置信度结论、矛盾说法**都还在**。

**这说明**：跨用户是"合规问题"，其余三类是"正确性问题"——**解决一个不等于解决另一个**。

### 6.2 实验 B：把 `user_id="*"` 也当泄漏

隔离规则写成：

```python
if item.user_id != user_id:      # ← 忘了公共知识
    drop()
```

实测后果（我第一版就是这个 bug）：

```
report: {'leaks_blocked': 4, ...}
kept: 2
```

**4 条被当成"泄漏"**——其中 3 条是知识库的公共条目。**干净得过头，等于把知识库清空。**

**修法**：

```python
if item.source == "other_user" or item.user_id not in (user_id, SHARED_USER):
```

### 6.3 实验 C：只做版本取代，不做冲突检测

后果：`reset_policy` 被正确处理（旧版丢弃），
但 `vpn_reauth` 的两条**同级**矛盾（都是 kb，版本都是 1）**原样并列**，没有任何提示。

**版本取代处理"新旧"，冲突检测处理"同级"。** 两者管的是不同的病。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.context_hygiene_demo
```

实测（节选）：

```
第 1 步：污染现场
  - 密码重置后 5 分钟内生效，期间请勿重复提交。
  - 密码重置后立即生效。
  - 李娜（E-3102）的工单：她的手机号 13800001234，VPN 已解封。
  - 上次结论：直接重置密码即可，无需确认身份。
  - 旧版客户端请使用 2024 版安装包。
  - VPN 掉线后需要重新认证，通常会要求输入动态口令。
  - VPN 掉线后会自动重连，不会要求重新认证。
  - 网上帖子说：把 DNS 改成 8.8.8.8 就能解决 VPN 认证问题。

第 2 步：每个过滤器单独开
  过滤器      拦下    标注过期  低权威  低置信  旧版本  冲突
  authority   0       0         2       0       0       0
  confidence  0       0         0       1       0       0
  conflict    0       0         0       0       0       2
  namespace   1       0         0       0       0       0
  stale       0       2         0       0       0       0
  version     0       0         0       0       1       0

第 3 步：四种解法叠加
  - [policy·2 天前] 密码重置后 5 分钟内生效，期间请勿重复提交。
  - [kb·500 天前·**可能过期**] 旧版客户端请使用 2024 版安装包。
  - [kb·30 天前] VPN 掉线后需要重新认证，通常会要求输入动态口令。
  - [kb·10 天前] VPN 掉线后会自动重连，不会要求重新认证。

  **注意：以下信息互相矛盾，请以最新政策为准并向用户确认：**
    - 关于「vpn_reauth」：…

  报告: {'leaks_blocked': 1, 'stale_annotated': 1, 'low_authority_dropped': 1,
         'low_confidence_dropped': 1, 'superseded_dropped': 1, 'conflicts': 1, 'kept': 4}
  拦下总计: 4 条
```

跑测试：

```powershell
uv run pytest
```

```
..........                                                               [100%]
370 passed in 42.96s
```

---

## 8. 生产环境踩坑实录

### 坑 1：隔离规则把公共知识也杀了

**现象**

上线多租户隔离后，Agent 突然什么都不知道了——知识库条目全没了。

**定位方法**

看 `leaks_blocked` 的数量：

```python
log.info("hygiene", **report.as_dict())
# leaks_blocked: 4   ← 只该是 1
```

**根因**

```python
if item.user_id != user_id:    # 公共知识的 user_id 是 "*"
```

**修法**

```python
if item.source == "other_user" or item.user_id not in (user_id, SHARED_USER):
```

**顺手加固**：给"公共知识必须保留"写一条测试（本节 `test_shared_items_are_not_treated_as_leaks`）。

### 坑 2：上次失败的结论被当成事实

**现象**

用户问同一个问题，Agent 直接给出"上次的结论"，但上次那个结论是错的。

**根因**

`session` 来源的条目权重 0.5，通过了 `MIN_WEIGHT=0.35` 的阈值——
而**失败尝试**和**成功结论**在元信息上长得一样。

**修法**

- 给结论加 `confidence`（写入方自评），`< 0.5` 直接丢
- 或者更明确：加 `outcome: success / failed / partial` 字段，`failed` 一律不进事实区

**顺手加固**：把"上次结论"降级成"参考"而不是"事实"——在 prompt 里用不同标签。

### 坑 3：两条矛盾信息并列，模型随机采信

**现象**

同一个问题，Agent 有时答"需要重新认证"，有时答"会自动重连"，用户以为系统不稳定。

**定位方法**

在 prompt 里搜同 topic 的多条：

```python
from collections import Counter
topics = Counter(item.topic for item in items if item.topic)
print([t for t, n in topics.items() if n > 1])
```

**根因**

没有任何冲突检测，两条都塞进去了。

**修法**

冲突显式化（§4.4）：两条都留，但**加一段"注意：以下信息互相矛盾"**。

**顺手加固**：把"冲突数量"做成监控指标——**它上升说明知识库该治理了**。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么四种解法必须叠加，不能选一个？</b></summary>

因为它们拦的是不同问题：命名空间拦跨用户（合规）、来源权重拦低权威、置信度拦失败结论、版本取代拦新旧、冲突检测拦同级矛盾。实测每个单独开都留下至少三类污染（`test_each_filter_alone_is_insufficient` 把这条固定住了）。

</details>

<details>
<summary><b>Q2：为什么冲突要显式呈现，而不是选一个最可信的？</b></summary>

因为"选错"的代价不对称：显式呈现冲突，模型会说"有两种说法，建议确认"；悄悄选一个，模型会用确定语气说出可能错误的话，用户没有线索怀疑。**模型不怕看到冲突，怕的是你替它悄悄选了一个错的。**

</details>

<details>
<summary><b>Q3：时效标注和"丢弃过期"有什么区别，为什么都要有？</b></summary>

丢弃是二选一（要么留要么删），标注是**把时间信息交给模型判断**。有些旧信息不能删（老客户端仍需要 2024 版安装包），但必须让模型知道它旧。**丢弃保正确性，标注保信息量。**

</details>

### 9.2 动手任务：把卫生检查接进组装流程

**要求**

1. 写 `build_context(user_id, query, now)`：召回长期记忆（4.5）+ 知识库 + 会话结论，
   转成 `ContextItem` 列表。
2. 调 `assemble()` 生成 prompt 片段，并把 `HygieneReport` 写进日志。
3. 加一条**硬断言**：如果 `report.conflicts` 非空，在最终答复里**必须**出现"两种说法"字样。
4. 补 3 个用例：无污染时报告全 0、有冲突时答复含提示、跨用户内容不出现。

**验收标准**

- 报告字段齐全且能进日志（结构化）。
- 冲突时答复**不会**给出单一定论。
- 原有 370 个用例仍全绿。

**提示**：第 3 条是关键——**把"是否显式化冲突"变成可测试的约束**，而不是靠人记得。

### 9.3 拓展挑战

把 `_claim_of` 从"比字符串"换成**结构化声明**：`{"topic": "vpn_reauth", "fact": "需要重新认证", "value": true}`。

**思考**：

1. 结构化之后，怎么判断两条声明矛盾？（提示：同 topic 同 fact，value 不同）
2. 非布尔类型的声明（比如"生效时间 5 分钟"）怎么比？
3. 谁来负责把自然语言抽成结构化声明？抽取错了怎么办（再引入一层污染）？

---

## 10. 本节小结与延伸

**三点回顾**

1. **四条污染路径**：跨用户（合规）、跨会话（失败结论）、过期（旧政策）、矛盾（**自信地答错**）。前三条让你答错，第四条让你**理直气壮地**答错。
2. **四种解法要叠加**：命名空间 / 来源权重+置信度 / 版本取代 / 冲突显式化；实测每个单独开都不够。
3. **冲突必须显式化**：模型不怕看到冲突，怕的是你替它悄悄选了一个错的。

**已知限制（诚实记录）**

- **冲突判定靠文本比较**：`_claim_of` 对没有数字的说法会退化成整句比较，误判率不低。
- **权重与阈值是拍的**：`0.35 / 0.5 / 180 天` 都需要用真实数据校准。
- **无脱敏**：跨用户内容被"拦下"，但如果它已经进了别处（日志、缓存），本节不管。
- **未接进组装流程**：`build_context` 是 §9.2 作业。

**延伸阅读**

- OWASP LLM Top 10（LLM01 提示注入、LLM06 敏感信息泄漏）：https://owasp.org/www-project-top-10-for-large-language-model-applications/
- Anthropic：上下文工程：https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- NIST AI RMF（数据治理与可追溯）：https://www.nist.gov/itl/ai-risk-management-framework

---

## 修订记录

- 2026-11-18 初稿：五步演示与 15 个测试全部实机运行；四种污染与四种解法均为实测
- 2026-11-18 修正：隔离规则初版写成 `item.user_id != user_id`，把 `user_id="*"` 的公共知识（policy/kb）也当成泄漏，`leaks_blocked` 一度是 4、`kept` 只剩 2；改为 `not in (user_id, SHARED_USER)`
- 2026-11-18 补充：新增低权威来源样例（网络偏方），让 `authority` 过滤器真正生效；新增"每个过滤器单独开都不够"的反向断言测试
- 2026-11-18 记录：`conflict` 单独开报 2 条（含旧政策），叠加 `version` 后剩 1 条——两个过滤器互补
