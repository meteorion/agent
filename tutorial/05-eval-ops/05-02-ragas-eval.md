---
section: "5.2"
title: "用 RAGAS 给 RAG 打分并定位病根"
stage: 5
status: done
depth: L2
difficulty: 4
minutes: 110
milestone: M5
depends_on: ["5.1"]
produces: ["eval/rag_eval.py"]
verifies: "跑出四项分数并能定位到具体病根"
part: null
updated: "2026-11-18"
---

# 5.2 用 RAGAS 给 RAG 打分并定位病根

> **本节先说结论，免得你按 RAGAS 的数字去对账**：
> `ragas 0.4.4` 能装、能 import，但**离线跑不了**——四项指标都要传
> `llm: InstructorBaseRagasLLM`。没有 API key 就没有分数。
>
> 所以本节实现**同名的确定性代理指标**，并逐项标明差异。
> **本节的数字不能与公开榜单上的 RAGAS 分数对比。**

## 0. 本节导航

**本节在链路中的位置**：5.1 建了通用评测集（20 条、规则判分）。本节聚焦**RAG 这一种具体架构**：检索 + 生成两段，任何一段坏了分数都会掉——**但你得知道是哪一段**。

**本节你将获得**

1. RAGAS 离线不可跑的确切证据（安装成功 + 报错原文 + 构造函数签名）。
2. 四项指标各自在算什么，以及本节替换了哪一项、为什么。
3. **四套 RAG 配置**（健康 / 太宽 / 太窄 / 幻觉）的实测分数矩阵。
4. 一张**病根定位表**：不同指标组合 → 不同病根 → 不同对策。
5. 两个我自己踩的指标设计坑：`answer_relevancy` 的字符代理测不准、AP 式 precision 抓不到尾部噪声。

**前置检查清单**

- 5.1 的 `uv run python -m scripts.eval_dataset_demo` 能跑通。
- 1.7 的 RAG 链路（切分/向量/检索/生成）已了解。

**难度**：★★★★☆ ｜ **预计时长**：110 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

RAG 出问题时，团队的第一反应通常是**调 `top_k`**：

- 答得不好 → 把 `top_k` 从 3 调到 8
- 还是不好 → 再调到 15
- 更不好了 → 调回 5，开始怀疑 embedding 模型

**这一套"盲调"能持续几周而毫无进展**，因为：

| 症状 | 可能的病根 | 对策 |
|---|---|---|
| 答得不对 | 检索没找到 | 加 top_k / 改切分 |
| 答得不对 | 检索找到了但答偏了 | 改提示词 |
| 答得不对 | 检索一堆噪声，模型被带偏 | **减** top_k / 加阈值 |
| 答得不对 | 上下文很好，模型自己编 | 加引用校验 |

**四种病根，对策完全相反。** 没有指标就只能瞎试。

---

## 2. 核心概念与架构设计

### 2.1 类比：体检报告

- **单看一个指标** = 只看体重：轻了可能是减肥成功，也可能是营养不良
- **看指标组合** = 体重 ↓ + 肌肉量 ↓ = 掉的是肌肉，不是脂肪

RAG 的四项指标就是四张化验单，**组合起来才能定位**。

> **这个类比在哪失效**：体检有"正常范围"；RAG 的"正常"高度依赖业务——
> 医疗问答要求 faithfulness 极高，闲聊场景 0.6 也许就够了。

### 2.2 四项指标

| 指标 | 问的问题 | 本节实现 |
|---|---|---|
| **faithfulness** | 答复里的每句话，能在检索到的资料里找到依据吗？ | 按句切分，算句子 token 被 context 覆盖的比例 |
| **answer_correctness** | 答复覆盖了参考答案的要点吗？ | 参考答案 token 被答复覆盖的比例 |
| **context_precision** | 检索回来的片段里，有多少是相关的？ | 相关片段数 / 总片段数 |
| **context_recall** | 该找到的关键事实，找到了多少？ | 被 context 覆盖的 key_facts 比例 |

**关于 `answer_correctness` 这个替换**（重要）：

RAGAS 的第四项叫 `answer_relevancy`，做法是"用 LLM 从答复反推一个问题，再算与原始问题的相似度"。
本节离线做不到，试了两个代理都不可用（§8.1）：

- 字符级覆盖率：**正确答案只得 0.14**
- 哈希向量余弦：**正确答案得 0.000**

所以换成 `answer_correctness`（对参考答案的覆盖），并**明确标注这是替换**。

### 2.3 四套 RAG 配置（诊断的靶子）

| 配置 | 检索 | 生成 | 预期病根 |
|---|---|---|---|
| **good** | 准 | 忠实 | 健康 |
| **noisy** | 太宽（混入 2 条无关） | 忠实 | 检索太宽 |
| **narrow** | 太窄（只召回部分） | 忠实但信息不足 | 检索太窄 |
| **hallucinating** | 准 | 编造（上下文里没有"厂商""100 MB"） | 生成幻觉 |

### 2.4 实测分数矩阵

| 配置 | faithfulness | correctness | precision | recall | 诊断 |
|---|---|---|---|---|---|
| good | 1.00 | 1.00 | 1.00 | 1.00 | **健康** |
| noisy | 1.00 | 1.00 | **0.42** | 1.00 | 检索太宽（召回多但噪声大） |
| narrow | 1.00 | **0.33** | 1.00 | **0.58** | 检索太窄（漏检） |
| hallucinating | **0.00** | **0.19** | 1.00 | 1.00 | 生成幻觉（上下文对，答复编） |

**注意每一行只有一个（或一组）指标塌了**——这正是能定位的原因。

### 2.5 病根定位表

| 指标模式 | 病根 | 对策 |
|---|---|---|
| precision 低 + recall 高 | 检索太宽 | **缩** top_k、加阈值、加 rerank |
| recall 低 + precision 高 | 检索太窄 | 加 top_k、改切分、查询改写 |
| 两项都低 | 检索整体失效 | 先查索引与 embedding 是否对得上 |
| precision/recall 高 + faithfulness 低 | 生成幻觉 | 收紧提示词、加引用校验（1.8 证据门） |
| correctness 低 | 答错/答非所问 | 查意图路由与技能选择 |

### 2.6 适用范围（边界）

| 场景 | 建议 |
|---|---|
| 检索质量调优 | ✅ precision / recall |
| 幻觉排查 | ✅ faithfulness（配 1.8 的引用校验） |
| 与公开榜单对比 | ❌ 代理指标不可比 |
| 语义等价判断（"重设"vs"重置"） | ❌ 字面指标会误判，需要 LLM 判分 |

---

## 3. 环境与版本

```
uv add ragas    # 实测装出 0.4.4，会带进 instructor / tiktoken / sqlalchemy 等
```

| 组件 | 实测版本 | 说明 |
|---|---|---|
| `ragas` | **0.4.4** | 能 import，**不能离线跑** |
| Python | `3.12.13` | 代理指标只用标准库 |

**验证**

```powershell
uv run python -c "from servicedesk.eval.rag_eval import evaluate; print('ok')"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/eval/rag_eval.py` | **主角**：四项指标 + 报告 + 病根定位 |
| `scripts/rag_eval_demo.py` | 六步演示 |
| `tests/test_rag_eval.py` | 22 个断言 |

### 4.1 样本

```python
@dataclass
class RagSample:
    question: str
    ground_truth: str
    contexts: list[str]                 # 检索到的片段（有序）
    answer: str
    key_facts: list[str] = field(default_factory=list)   # 预标注的关键事实
```

**`key_facts` 是 recall 的基础**。没标就退化成"用参考答案切句"（代码里有兜底）。

### 4.2 faithfulness

```python
def score_faithfulness(answer: str, contexts: list[str], *, support_threshold: float = 0.6) -> float:
    sentences = [part.strip() for part in _SENTENCE_RE.findall(answer) if part.strip()]
    if not sentences:
        return 0.0
    context_tokens: set[str] = set()
    for context in contexts:
        context_tokens |= tokens(context)

    supported = sum(
        1 for sentence in sentences if coverage(tokens(sentence), context_tokens) >= support_threshold
    )
    return supported / len(sentences)
```

### 4.3 病根定位（核心）

```python
    # 检索太宽：噪声多（精度低），但该有的都在（召回高）
    if precision < LOW and recall >= GOOD:
        return Diagnosis("retrieval-noisy", ...)

    # 检索太窄：该有的没找到（召回低），但找到的都相关（精度高）
    if recall < LOW and precision >= GOOD:
        return Diagnosis("retrieval-narrow", ...)

    # 检索够好，但答复不支持 → 生成幻觉
    if precision >= GOOD and recall >= GOOD and faithfulness < LOW:
        return Diagnosis("generation-hallucination", ...)
```

**顺序很重要**：先排检索问题，再排生成问题。因为**检索错了，faithfulness 高低都没意义**。

### 4.4 运行

```powershell
uv run python -m scripts.rag_eval_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么"总分"完全没用

四个变体的总分（四项平均）：

- good: 1.00
- noisy: 0.85
- narrow: 0.73
- hallucinating: 0.55

**看起来只是"程度不同"。** 但它们是**四种完全不同的病**，对策互斥：

- noisy 要**缩** top_k
- narrow 要**加** top_k
- hallucinating 跟 top_k 毫无关系

**只看总分，你会对四种病开同一张药方。**

### 5.2 为什么先排检索、再排生成

因为**检索错了，faithfulness 的高低不代表任何东西**：

- 上下文全是噪声，模型忠实复述噪声 → faithfulness 高、但答案是错的
- 上下文缺失关键事实，模型老实说"资料里没有" → faithfulness 高、答案无用

**"忠实"只有在"资料对"的前提下才有意义。** 所以判断顺序必须是：检索 → 生成。

### 5.3 报告为什么要能指到"最差样本"

实测输出：

```
【noisy】最差样本: 附件太大发不出去怎么办？  (context_precision=0.33)
【narrow】最差样本: 附件太大发不出去怎么办？  (context_recall=0.50)
【hallucinating】最差样本: VPN 认证失败怎么办？  (faithfulness=0.00)
```

**平均分告诉你"病了"，最差样本告诉你"哪疼"。** 排障永远从最差样本开始看。

### 5.4 `support_threshold=0.6` 是怎么来的

这是一个**保守**的阈值：句子里 60% 的 token 能在上下文中找到，才算"有依据"。

调低（0.4）→ 编造的句子也能"部分匹配"，faithfulness 虚高。
调高（0.9）→ 换个说法就被判"无依据"，faithfulness 虚低。

**建议**：先用当前值，等有了人工标注的样本再校准——**别拍脑袋固定**。

### 5.5 为什么 `contexts` 要保留顺序

本节的 precision 用"相关片段占比"，不看顺序。但**真实体验里顺序很重要**：

- 相关片段排在最后 → 模型可能被前面的噪声带偏（"lost in the middle"）
- 相关片段排在最前 → 即使 top_k 大一点也没关系

**所以 `contexts` 用 `list` 不用 `set`**，为将来加"排序质量"指标留好接口。

### 5.6 为什么 `compare()` 要输出 `root_cause`

因为 RAG 调优的日常动作是"改一个参数，跑一遍，看哪一列动了"。

**只有一列变**，才说明你改对了地方。如果四列一起变，说明改动影响面太大，**无法归因**——
这种情况下就算分数变好了，你也不知道为什么，下次可能又变坏。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：把 precision 换成"排序加权 AP"

我最初用的是 AP（average precision，相关片段越靠前分越高）：

```
contexts = [F1, F2, NOISE1, NOISE2]     # 相关在前，噪声在后
AP = 1.0                                 # ← 完全看不出问题
```

**因为 AP 只看"相关的是否靠前"，不惩罚"后面还有多少噪声"。**

而"检索太宽"的**典型症状恰恰是尾部噪声**。所以换成"相关片段占比"：

```
precision = 2/4 = 0.50    ← 立刻暴露
```

**代价**：不再反映排序质量（排序交给 1.8 的 rerank 指标）。

### 6.2 实验 B：用字符覆盖率算 `answer_relevancy`

第一版：

```python
def score_answer_relevancy(question, answer):
    return coverage(tokens(question), tokens(answer))
```

实测把**正确答案**判成 **0.14**：

```
问题: VPN 认证失败怎么办？          (token: V,P,N,认,证,失,败,怎,么,办)
答案: 请先在自助门户重置密码……      (token: 请,先,在,自,助,门,户,重,置,密,码…)
```

**中文问答里，问句和答句的用词天然重叠很少**——"怎么办"这种词在答案里不会出现。

换成哈希向量余弦也一样：**正确答案 0.000**（字符三元组几乎不重叠）。

**这就是我最终换成 `answer_correctness` 的原因**：与其硬凑一个误导性的数字，
不如换一个能算准的指标，并**明确标注替换**。

### 6.3 实验 C：不给 key_facts

`RagSample` 支持省略 `key_facts`，此时会从参考答案切句退化。

**后果**：recall 变成"参考答案的每句话是否被覆盖"，颗粒度变粗。
一个参考答案有三句话、上下文覆盖了两句 → recall = 0.67，看起来还行——
但如果漏掉的是最关键那句，你就被骗了。

**所以：key_facts 该标就标，别偷懒。**

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.rag_eval_demo
```

实测（节选）：

```
第 1 步：先说实话 —— RAGAS 装得上，但离线跑不了
  实测安装：ragas 0.4.4（会带进 instructor / tiktoken / sqlalchemy 等依赖）
  实测 API：
    from ragas.metrics.collections import Faithfulness, AnswerRelevancy, ...
    Faithfulness.__init__(self, llm: InstructorBaseRagasLLM, ...)

  没有 API key 时：
    >>> Faithfulness()
    TypeError: Faithfulness.__init__() missing 1 required positional argument: 'llm'

第 3 步：四个变体的分数
  配置            faithfulness   correctness  precision   recall    诊断
  good            1.00           1.00         1.00        1.00      健康
  noisy           1.00           1.00         0.42        1.00      检索太宽（召回多但噪声大）
  narrow          1.00           0.33         1.00        0.58      检索太窄（漏检）
  hallucinating   0.00           0.19         1.00        1.00      生成幻觉（上下文对，答复编）

第 4 步：从分数反推病根
  【noisy】检索太宽（召回多但噪声大）
    证据: context_precision=0.42  context_recall=1.00  faithfulness=1.00
    建议: 缩小 top_k、加相似度阈值、加 rerank
    最差样本: 附件太大发不出去怎么办？  (context_precision=0.33)

  【narrow】检索太窄（漏检）
    证据: context_precision=1.00  context_recall=0.58  faithfulness=1.00
    最差样本: 附件太大发不出去怎么办？  (context_recall=0.50)

  【hallucinating】生成幻觉（上下文对，答复编）
    证据: context_precision=1.00  context_recall=1.00  faithfulness=0.00
    最差样本: VPN 认证失败怎么办？  (faithfulness=0.00)
```

跑测试：

```powershell
uv run pytest
```

```
....                                                                     [100%]
436 passed in 49.34s
```

---

## 8. 生产环境踩坑实录

### 坑 1：指标把正确答案判成低分（最伤士气的一种）

**现象**

团队改了半天，`answer_relevancy` 一直在 0.1–0.2 之间，以为答得极差。

**根因**

字符级覆盖率当"相关性"用。中文问句与答句的用词天然重叠少。

**修法**

换成对参考答案的覆盖（`answer_correctness`），或接真实 LLM 判分（§9.3）。

**教训**：**指标先验证"对正确答案给不给高分"，再拿去量模型。**
（这和 5.1 的"判分器必须先能判错"是一对。）

### 坑 2：AP 式 precision 抓不到尾部噪声

**现象**

`top_k` 从 3 调到 10，混进一堆无关文档，但 precision 还是 1.0。

**根因**

AP 只奖励"相关的靠前"，不惩罚"后面还有多少噪声"。

**修法**

用"相关片段占比"，或在 AP 之外**另加一个"噪声率"指标**。

**顺手加固**：把 `top_k` 和 precision 画在一起看——两者的曲线交叉点就是最优 `top_k`。

### 坑 3：拿代理指标的数字去和论文对比

**现象**

"我们的 faithfulness 是 1.00，超过论文里的 0.85！"——然后被质疑。

**根因**

代理指标与 RAGAS 的口径完全不同（一边是 token 覆盖，一边是 LLM 判断 claim）。

**修法**

- 报告里**显式标注**用的是哪套指标
- 只在**同一套指标内部**做纵向对比（改前 vs 改后）

**顺手加固**：指标实现文件的开头就写"不可与公开分数对比"（本节模块 docstring 就是）。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么 RAG 评测不能只看"总分"？</b></summary>

因为四种病根（检索太宽、太窄、整体失效、生成幻觉）的总分可能差不多（0.55–0.85），但对策完全相反：太宽要缩 top_k，太窄要加 top_k，幻觉跟 top_k 无关。只看总分等于对四种病开同一张药方。

</details>

<details>
<summary><b>Q2：为什么判断顺序必须是"先检索、后生成"？</b></summary>

因为"忠实"只有在"资料对"的前提下才有意义。上下文全是噪声时，模型忠实复述噪声 → faithfulness 高但答案是错的；上下文缺关键事实时，模型老实说"没有" → faithfulness 也高。所以先确认检索够好，faithfulness 才有解释力。

</details>

<details>
<summary><b>Q3：为什么本节的数字不能和 RAGAS 的公开分数比？</b></summary>

因为 RAGAS 的分数来自 LLM 判断（拆 claim、反推问题），本节用的是 token 覆盖率代理。两者口径不同，绝对值不可比。代理指标只适合**同一套指标内部的纵向对比**（改前 vs 改后）。

</details>

### 9.2 动手任务：给 1.7 的真实 RAG 打分

**要求**

1. 把 1.7 的 `rag/answer.py` 接上本节指标：输入问题，跑检索+生成，产出 `RagSample`。
2. 用 1.7 的 `knowledge/` 造 5 条评测样本（含 ground_truth 与 key_facts）。
3. 跑两套 `top_k`（2 和 6），对比四项指标，输出 `compare()` 表格。
4. 根据诊断结论，给出一句"该调什么"的建议。

**验收标准**

- 5 条样本都有 key_facts（不许省略）。
- 两套配置的四项指标都有变化，且能归因到某一项。
- 结论里必须写明"为什么不是另一项"。
- 原有 436 个用例仍全绿。

**提示**：如果两套配置的分数几乎一样，说明你的样本区分度不够——回去改样本，别改指标。

### 9.3 拓展挑战

把 `LLMJudge`（5.1 的骨架）接进来，实现真正的 `answer_relevancy`。

**思考**：

1. 反推问题的提示词怎么写才稳定？（提示：限定输出格式、给 3 个例子）
2. 反推 3 个问题再取平均（RAGAS 的 `strictness` 参数）能降低方差吗？
3. 怎么验证"LLM 判分和人工判分"一致？（提示：5.1 的 `agreement`/`kappa`）

---

## 10. 本节小结与延伸

**三点回顾**

1. **RAGAS 离线跑不了**：四项指标都要 LLM；本节用确定性代理，**显式标注不可比**。
2. **指标的价值在组合模式**：precision 低+recall 高=太宽；反之=太窄；两项都高但 faithfulness 低=幻觉。
3. **先排检索、再排生成**：检索错了，faithfulness 高低都没有解释力。

**已知限制（诚实记录）**

- **全部是字面指标**：同义表述（"重设" vs "重置"）会误判。
- **`answer_correctness` 是替换指标**：不是 RAGAS 的 `answer_relevancy`。
- **阈值（0.6 / 0.7）是经验值**：没做人工校准。
- **只测了 2 条样本/配置**：演示够用，结论不具统计意义。
- **没接真实 RAG 链路**：样本是手写的（§9.2 是作业）。

**延伸阅读**

- RAGAS 论文（四项指标的定义来源）：https://arxiv.org/abs/2309.15217
- RAGAS 文档：https://docs.ragas.io/
- "Lost in the middle"（上下文顺序的影响）：https://arxiv.org/abs/2307.03172

---

## 修订记录

- 2026-11-18 初稿：六步演示与 22 个测试全部实机运行；四套配置的诊断结果均为实测
- 2026-11-18 实测记录：`ragas 0.4.4` 安装成功但 `Faithfulness()` 缺 `llm` 参数直接 `TypeError`，离线不可跑；API 已迁移到 `ragas.metrics.collections`
- 2026-11-18 修正：`context_precision` 初版用排序加权 AP，实测"相关在前、噪声在后"时仍是 1.0，抓不到"检索太宽"；改为"相关片段占比"
- 2026-11-18 修正：`answer_relevancy` 的字符级代理把正确答案判成 0.14、哈希向量余弦判成 0.000；改用 `answer_correctness` 并**显式标注替换**，同时改掉诊断里的字段名
- 2026-11-18 修正：`narrow` 变体初版的第二条样本 context 为空，导致 precision 被拉到 0.5 而误诊为 `retrieval-broken`；改为"只召回了部分内容"的片段
