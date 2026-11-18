---
section: "1.8"
title: "基础 RAG（三）：拼 prompt、给引用、压幻觉（M1 验收）"
stage: 1
status: done
depth: L2
difficulty: 3
minutes: 90
milestone: M1
depends_on: ["1.7"]
produces: ["rag/answer.py", "scripts/ask.py"]
verifies: "问一个知识库里没有的问题，模型明确说「查不到」而不是编"
part: null
updated: "2026-11-17"
---

# 1.8 基础 RAG（三）：拼 prompt、给引用、压幻觉（M1 验收）

## 0. 本节导航

**本节在链路中的位置**：阶段一的收尾，也是 **M1 里程碑验收**。它把前七节的产物第一次串成一条完整链路：

```
1.1 工程骨架 → 1.5 客户端（重试/异常/成本）
    → 1.6 切分 → 1.7 检索 → 1.8 拼 prompt + 校验引用
```

**本节你将获得**

1. 一个固定的三段式 prompt 模板（system 规则 / 【资料】/【问题】），以及为什么这样分。
2. 强约束输出：`{"answer": ..., "citations": [{"index": n}]}`，用 pydantic 校验。
3. **两道防幻觉闸门**：证据不足直接不调模型；引用不存在则整段作废。
4. 一个能用的命令行工具 `scripts/ask.py`（M1 验收物）。
5. 一组实测数字：可答问题 rerank 分 ≥ 0.22，不可答 ≤ 0.10。

**前置检查清单**

- 1.7 的 `uv run pytest` 是 64 passed。
- `knowledge/` 两篇文档、`scripts/mock_llm_server.py` 可用。

**难度**：★★★☆☆ ｜ **预计时长**：90 分钟 ｜ **本节费用**：**¥0**（mock，单次调用约 ¥0.00016 的等价 token）

---

## 1. 为什么需要它（痛点先行）

假设你把 1.7 的检索结果直接拼进 prompt：

```
以下是相关资料：
{把 5 个块直接倒进去}

请回答问题：{用户问题}
```

然后上线。三种事故会在一周内全部出现：

**事故 A：资料里没有，模型硬答。**

用户问"公司年会在哪里办"，检索确实返回了 5 条（最近邻永远有结果）。模型看着一堆 VPN 和邮箱资料，为了"有用"，编了一个答案。

**事故 B：有答案，但不知道依据是什么。**

用户问"VPN 认证失败怎么办"，模型答"重置密码"。用户追问"哪份文档说的？"——**你答不上来**。没有引用的回答，在客服场景里等于没有回答。

**事故 C：模型编了一个不存在的引用。**

你在 prompt 里要求"标注来源编号"，模型很配合地写：

> VPN 认证失败时请直接重启路由器即可恢复**[C9]**。

而资料里只有 `[C1]`–`[C5]`。**如果你直接渲染给用户，他会看到一个指向不存在资料的标注**——比不标引用更糟，因为它看起来可信。

**这三种事故，prompt 一个都防不住。** prompt 只能"请求"模型遵守规则，**不能"保证"它遵守**。所以本节的重点不是写 prompt，而是写**prompt 之外的两道闸门**。

---

## 2. 核心概念与架构设计

### 2.1 类比：带出处的学术引用

一篇合格的论文，必须满足三件事：

- **只引用真实存在的文献**（不能编参考条目）
- **每个论断后面标注来源**（[1]、[2]）
- **没有文献支撑的论断，要么删掉、要么明说"尚无定论"**

RAG 的回答是完全一样的标准。区别只在于：**论文作者会自己核对，而模型不会——你得用代码替它核对。**

> **这个类比在哪失效**：论文的引用是"作者读过并理解"的，而模型的引用可能只是"位置相邻"。它标了 [C1]，但 [C1] 里其实没有支撑那句话的内容。**位置对 ≠ 语义对**，这需要更强的校验（阶段五的评估体系）。

### 2.2 完整链路

```
用户问题
   │
   ▼
[1.7 检索] top-10（不设向量阈值）
   │
   ▼
[1.7 rerank] 取 top-5，拿到 rerank 分
   │
   ├─ rerank 分 < 0.15 ──► 闸门 1：直接返回"查不到"（不调模型，费用 0）
   │
   ▼
[本节] build_context  →  [C1]…[C5] + index_map
   │
   ▼
[本节] build_messages →  system 规则 + 【资料】+【问题】
   │
   ▼
[1.5 客户端] chat（重试 / 异常分类 / 成本统计）
   │
   ▼
[本节] parse_answer → pydantic 校验 {answer, citations:[{index, quote}]}
   │
   ▼
[本节] validate_citations
   │
   ├─ 引用了不存在的编号 ──► 闸门 2：整段作废，原文留档
   │
   ▼
RagResult（answer + citations + grounded + evidence_score + cost）
```

### 2.3 为什么证据闸门用 rerank 分，不用向量分

这是本节最重要的一个工程判断。

实测同一批问题（本语料，哈希向量 + rerank）：

| 问题 | 向量分 | rerank 分 | 可答？ |
|---|---|---|---|
| VPN 提示认证失败怎么办 | 0.414 | 0.383 | ✅ |
| 离职后邮箱还能用吗 | 0.432 | 0.356 | ✅ |
| 邮件附件上限是多少 | 0.217 | 0.378 | ✅ |
| **公司年会在哪里办** | 0.169 | **0.075** | ❌ |
| **食堂几点开饭** | 0.227 | **0.000** | ❌ |
| **打印机怎么装驱动** | 0.172 | **0.075** | ❌ |

看第三行和第五行：**"邮件附件上限"的向量分 0.217 比"食堂几点开饭"的 0.227 还低**。用向量分做闸门，必然把前者误杀、把后者放进去。

而 rerank 分把两类分得干干净净：**可答 ≥ 0.22，不可答 ≤ 0.10**，取 0.15 正好。

> 原因：向量分衡量"整段文本的字面相似度"，会被长度和泛词稀释；rerank 分衡量"问题里的词在这段资料里出现了多少"，更接近"有没有答案"。

### 2.4 两道闸门的分工

| 闸门 | 触发条件 | 动作 | 防的是 |
|---|---|---|---|
| **闸门 1**：证据不足 | rerank 分 < 0.15 | 不进 prompt，直接回"查不到" | 事故 A（硬答） |
| **闸门 2**：引用造假 | 引用了不存在的编号 | 整段回答作废，原始回答留档 | 事故 C（假引用） |

**注意闸门 2 是"整段作废"，不是"删掉引用保留结论"。** 后者更危险：用户仍会读到一个没有出处的断言。

### 2.5 适用范围（边界）

| 场景 | 适用 |
|---|---|
| 客服 / IT 支持（答案必须可追溯） | ✅ 本节的模式就是为它设计的 |
| 创意写作、头脑风暴 | ❌ 强约束引用会扼杀发挥 |
| 知识库很小（< 20 块） | ⚠️ 可以直接全塞，不必检索 |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 | 用在哪 |
|---|---|---|
| pydantic | `2.14.3` | `AnswerPayload` / `CitationRef` 校验 |
| qdrant-client | `1.20.0` | 检索 |
| structlog | `26.1.0` | 丢弃引用 / 证据不足的告警日志 |

**验证**

```powershell
uv run python -c "from servicedesk.rag.answer import SYSTEM_PROMPT; print(len(SYSTEM_PROMPT))"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/rag/answer.py` | **主角**：prompt、解析、两道闸门 |
| `scripts/ask.py` | M1 验收工具（命令行） |
| `tests/test_rag_answer.py` | 14 个断言 |

### 4.1 三段式 prompt

```python
SYSTEM_PROMPT = """你是企业 IT 服务台的助理，负责依据内部手册回答员工问题。

必须遵守的规则：
1. 只能使用【资料】里出现的内容回答，不得使用常识或自行推测。
2. 每条结论后面用 [C1]、[C2] 这样的编号标注来源，编号必须出现在【资料】里。
3. 如果【资料】里没有能回答该问题的内容，answer 就写"资料中没有相关信息"，citations 留空。
4. 只输出一个 JSON 对象，格式如下，不要输出任何解释文字：
   {"answer": "...", "citations": [{"index": 1, "quote": "支撑这句话的原文片段"}]}
"""


def build_messages(question: str, context: str, *, extra_instruction: str = "") -> list[dict[str, str]]:
    suffix = f"\n\n【附加要求】\n{extra_instruction}" if extra_instruction else ""
    user = f"【资料】\n{context or NO_EVIDENCE_PLACEHOLDER}\n\n【问题】\n{question}{suffix}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
```

**为什么把规则放 system、把资料放 user？** 因为 system 是"角色设定"，模型对它的服从度更高；资料是"本次任务的数据"，每次都不一样。**混在一起会让"规则"被"数据"稀释**——资料一长，模型的注意力就被拉走了。

### 4.2 上下文编号与映射

```python
def build_context(hits: Sequence[Hit]) -> tuple[str, dict[int, Hit]]:
    index_map: dict[int, Hit] = {}
    blocks: list[str] = []

    for position, hit in enumerate(hits, start=1):
        index_map[position] = hit
        path = " > ".join(hit.heading_path) or "（无标题）"
        blocks.append(f"[C{position}] doc={hit.doc_id} § {path}\n{hit.text.strip()}")

    return "\n\n".join(blocks), index_map
```

`index_map` 是**校验的唯一依据**：模型说 `[C3]`，我们就查 `index_map[3]`。模型没法伪造 `chunk_id`，因为它根本不知道 `chunk_id`。

### 4.3 解析：pydantic + 复用 1.4 的工具

```python
class CitationRef(BaseModel):
    index: int = Field(ge=1)
    quote: str = Field(default="", max_length=200)


class AnswerPayload(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)
    citations: list[CitationRef] = Field(default_factory=list)


def parse_answer(raw: str) -> AnswerPayload:
    cleaned = strip_code_fence(raw)              # 1.4 写过
    candidate = extract_json_object(cleaned)     # 1.4 写过
    if candidate is None:
        raise AnswerParseError(raw, "输出里找不到 JSON 对象")
    try:
        return AnswerPayload.model_validate_json(candidate)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc") or ()) or "整体"
        raise AnswerParseError(raw, f"字段 {location}：{first.get('msg')}") from exc
```

**`index` 用 `ge=1` 而不是 `ge=0`**：编号从 1 开始，`[C0]` 一定是模型幻觉。让校验层直接拒掉。

### 4.4 两道闸门（核心）

**闸门 1：证据不足不调模型**

```python
    ranked = rerank(question, hits, top_k=top_k)
    evidence = ranked[0].rerank_score or 0.0

    if evidence < rerank_min_score:
        log.info("answer_no_evidence", question=question, evidence_score=round(evidence, 4))
        return RagResult(
            question=question,
            answer=NO_EVIDENCE_ANSWER,
            hit_ids=[hit.chunk_id for hit in ranked],
            no_evidence=True,
            grounded=False,
            evidence_score=evidence,
        )
```

**闸门 2：引用造假则整段作废**

```python
    citations, dropped = validate_citations(payload, index_map)
    grounded = bool(citations)

    rejected = bool(dropped) and not grounded
    if rejected:
        log.warning("answer_rejected", dropped=dropped, raw_answer_preview=payload.answer[:120])
        return RagResult(
            question=question,
            answer=REJECTED_ANSWER,          # 对外：安全话术
            grounded=False,
            dropped_citations=dropped,
            rejected=True,
            raw_answer=payload.answer,       # 对内：原文留档，供审计
            ...
        )
```

**`raw_answer` 必须保留**：作废是产品决策，不是销毁证据。事后追查"模型为什么编"，你需要原文。

### 4.5 运行

```powershell
uv run python -m scripts.ask --demo
uv run python -m scripts.ask "VPN 提示认证失败怎么办"
uv run python -m scripts.ask --json "邮件附件最大能发多大"
```

---

## 5. 逐块拆解

### 5.1 为什么把 `[Cn]` 写进资料头，而不是让模型自己数顺序

```python
blocks.append(f"[C{position}] doc={hit.doc_id} § {path}\n{hit.text.strip()}")
```

如果资料块之间没有显式编号，模型需要"数第几段"，很容易数错。**给它一个明确的锚点**，引用准确率会显著提高。

### 5.2 为什么 `quote` 限长 200

`quote` 是"支撑这句话的原文片段"，不是"整段资料"。限长有两个作用：

1. 防止模型把整块资料复制一遍（浪费输出 token）
2. 逼它**只摘关键句**——摘不出来的话，说明它并没有真的找到依据

它也是后续人工复核的抓手：审核员看 `quote` 就知道结论靠不靠谱，不用回原文翻。

### 5.3 为什么解析失败要"降级"而不是"报错"

```python
    except AnswerParseError as exc:
        log.warning("answer_parse_failed", detail=exc.detail)
        return RagResult(answer=call.text.strip(), grounded=False, parse_ok=False, ...)
```

模型偶尔会输出自然语言而不是 JSON。这时有两条路：

- **抛异常** → 用户看到 500，一次偶发故障变成一次事故
- **降级** → 把原文当纯文本返回，标记 `parse_ok=False` 和 `grounded=False`，上层可以决定"要不要展示"

**选降级**。因为"模型偶尔不听话"是可预料的常态，不是故障。

### 5.4 `evidence_score` 为什么要暴露给上层

```python
    evidence_score: float = 0.0
```

它让上层能做**分级处理**：

| evidence_score | 建议动作 |
|---|---|
| ≥ 0.35 | 直接展示 |
| 0.15 – 0.35 | 展示 + "仅供参考" |
| < 0.15 | 不展示，转人工 |

**一个二值判断（有/无证据）不够用**——真实业务里总有"有点相关但不确定"的地带。

### 5.5 `extra_instruction` 为什么只进 prompt、不进检索

```python
async def answer_question(..., extra_instruction: str = "") -> RagResult:
    ...
    messages = build_messages(question, context, extra_instruction=extra_instruction)
```

这个参数是给"附加要求"用的（语气、语言、格式）。它**不参与检索**，因为检索看的是"用户到底在问什么"。

写这一节时踩到的真实例子：我最初把 `#hallucinate` 直接拼在问题后面，结果这个标记词稀释了 rerank 分（0.383 → 0.143），**连闸门 1 都过不了**，演示直接失效。**控制指令和数据必须分开放**，这是同一个道理。

### 5.6 为什么 `NO_EVIDENCE_ANSWER` 和 `REJECTED_ANSWER` 是两句不同的话

```python
NO_EVIDENCE_ANSWER = "知识库里没有找到与这个问题相关的资料，建议提交 IT 工单或转人工。"
REJECTED_ANSWER = "资料中未能确认该问题的答案。为避免误导，已拒绝本次回答，请转人工复核。"
```

前者是"没有资料"，后者是"有资料但模型不可信"。对用户来说都是"没答案"，但对**运维**来说完全不同：

- 前者说明**知识库缺内容**（该补文档）
- 后者说明**模型在乱编**（该改 prompt 或换模型）

**同一句话会让这两种问题永远混在一起。**

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：不校验引用，直接渲染

把 `validate_citations` 去掉，直接用模型给的回答渲染。模型输出：

```
VPN 认证失败时请直接重启路由器即可恢复[C9]。
引用编号: [9]（资料里只有 1..5）
```

用户看到的是一条**格式完美、带引用标注、但引用不存在**的回答。

更麻烦的是审计：如果日志里只记 `citations` 而没记 `index_map` 的大小，你事后**无法判断**这个 `[C9]` 是模型编的，还是当时资料真的有 9 条。

> 修法就是本节的两件事：**校验编号存在性 + 保留原始回答**。

### 6.2 实验 B：去掉证据闸门

把 `rerank_min_score` 设成 `0.0`，问一个知识库外的问题："公司年会在哪里办"。

实测：

```
回答: 根据资料：同时在线 | 退出其他设备上的客户端 |

排查时建议一次只改一项，改完立刻重试。[C1]
引用: ['[C1] vpn-guide § VPN 使用与排障手册 > 4. 常见故障 > 4.2 提示「无法建立隧道」']
接地: True   费用: ¥0.000165
```

**两个问题同时发生**：

1. **白花了钱**（¥0.000165，一天 10 万次就是 ¥16.5，而且全是废话）
2. **更糟：它 `grounded=True`**——有引用、有出处、格式合规。**任何自动校验都拦不住它**，因为引用是真的，只是答案与问题毫无关系。

这就是"证据闸门"必须在**调用模型之前**的原因：一旦让模型看到无关资料，它总能从里面"找到"点什么。

---

## 7. 验证与预期输出

### 7.1 四个场景

```powershell
uv run python -m scripts.ask --demo
```

实测输出（节选）：

```
索引就绪：26 块，dim=256，库内 count=26

问题 1：VPN 提示认证失败怎么办
回答：根据资料：如果客户端提示认证失败，通常有三类原因：
- 密码已过期（公司域账号每 90 天强制更换一次）
- 动态口令与服务器时间偏差超过 30 秒
- 账号被锁定（连续输错 5 次会自动锁定 30 分钟）
处理顺序：先在自助门户重置密码；若仍失败，请校准手机时间后重试；最后再提交工单申请解锁。[C1]

引用：
  [C1] vpn-guide § VPN 使用与排障手册 > 4. 常见故障 > 4.1 提示「认证失败」
    「如果客户端提示认证失败，通常有三类原因：…」

→ 校验结论：已接地，引用 1 条，费用 ¥0.000175

问题 2：邮件附件最大能发多大
回答：根据资料：每个员工邮箱容量为 50 GB，单封邮件附件上限为 25 MB。[C1]
引用：
  [C1] email-faq § 企业邮箱常见问题 > 1. 邮箱容量与附件限制
→ 校验结论：已接地，引用 1 条，费用 ¥0.000156

问题 3：公司年会在哪里办
回答：知识库里没有找到与这个问题相关的资料，建议提交 IT 工单或转人工。
引用：（无）
⚠️ 证据不足（rerank 分 0.075），未调用模型
→ 校验结论：证据不足（rerank=0.075）→ 没有调用模型（省钱且不会幻觉）

问题 4：VPN 认证失败怎么办 #hallucinate
回答：资料中未能确认该问题的答案。为避免误导，已拒绝本次回答，请转人工复核。
引用：（无）
⚠️ 已丢弃无效引用编号：[9]
⚠️ 原始回答已作废（保留供审计）：根据资料，VPN 认证失败时请直接重启路由器即可恢复[C9]。
→ 校验结论：模型引用了不存在的编号 [9] → 整段回答已作废
```

**四个场景分别验证了四件事**：

| 场景 | 验证点 |
|---|---|
| 问题 1 | 正常链路：检索 → prompt → 引用 → 接地 |
| 问题 2 | 换了文档域（邮箱）也能工作 |
| 问题 3 | **闸门 1**：证据不足，零成本、零幻觉 |
| 问题 4 | **闸门 2**：编造引用被拦，回答作废但原文留档 |

### 7.2 测试

```powershell
uv run pytest
```

```
........................................................................ [ 92%]
......                                                                   [100%]
78 passed in 9.31s
```

---

## 8. M1 里程碑验收

阶段一结束。逐条对照 toc 里的验收标准：

| # | 验收项 | 证据 | 状态 |
|---|---|---|---|
| 1 | 从空目录搭出可运行骨架 | `uv run python -m servicedesk` → `✓ 环境自检通过` | ✅ |
| 2 | 手写 RAG 链路并解释失效点 | 1.6 切分对比、1.7 阈值/rerank、1.8 两道闸门 | ✅ |
| 3 | 能说清"检索为空"和"检索到噪声"怎么查 | 1.7 §8 坑 2/3、1.8 §6.2 | ✅ |
| 4 | 知道什么时候该用异步、什么时候没用 | 1.2 实测 I/O 2.96 倍 vs CPU 1.554s | ✅ |
| 5 | 客户端有超时/重试/异常分层/成本账本 | 1.5 六个场景 + 36 个用例 | ✅ |
| 6 | 数据进出都有类型与校验 | 1.4 `Ticket`、1.8 `AnswerPayload` | ✅ |
| 7 | 端到端工具可跑 | `uv run python -m scripts.ask --demo` 四个场景 | ✅ |
| 8 | 没有 Key 也能跑通 | 全部基于 mock，0 成本 | ✅ |

**M1 完成标志**：能对知识库提问、给出带引用且可追溯的回答、查不到时明确说查不到。

---

## 9. 生产环境踩坑实录

### 坑 1：prompt 里写了"必须引用"，模型依然不引用

**现象**

`grounded` 长期在 60% 左右徘徊。日志里大量 `answer_ungrounded`：模型给了答案，但 `citations` 是空数组。

**定位方法**

```python
log.info("ungrounded_sample", answer=payload.answer[:200], hits=len(hits))
```

把样本捞出来看——常见形态是"模型用常识补了一句"：

> 另外建议定期更换密码，以提高账户安全性。

这句在任何资料里都没有，所以它没法标引用。**而它听起来完全正确。**

**根因**

模型被训练成"有帮助"，而"只说资料里有的话"和"有帮助"是冲突的。资料不够的时候，它会倾向补充。

**修法**

三层一起上：

```diff
  # 1) system prompt 里给"没依据时怎么说"的具体话术
- 3. 如果资料里没有能回答该问题的内容，answer 写"不知道"。
+ 3. 如果资料里没有能回答该问题的内容，answer 必须写"资料中没有相关信息"，
+    citations 留空。不要补充任何资料之外的建议。

  # 2) 代码层：没有引用的回答不直接给用户
+ if not grounded:
+     return f"{payload.answer}\n\n（以上内容未能对应到具体文档，请谨慎参考）"

  # 3) 评估层（阶段五）：把 grounded 率当成指标持续监控
```

**顺手加固**：把 `grounded` 率写进日常监控。它掉了，通常意味着**知识库覆盖度下降**，而不是模型变差。

### 坑 2：引用编号对，但引用的内容不支持结论

**现象**

用户投诉"你们说的和我看到的文档不一样"。查日志：`[C2]` 确实存在，但 `[C2]` 的内容是"附件上限 25MB"，而回答说的是"单封邮件不能超过 50MB"。

**定位方法**

对比 `quote` 与 `hit.text`：

```python
for cited in result.citations:
    hit = index_map[cited.index]
    if cited.quote and cited.quote not in hit.text:
        log.warning("quote_mismatch", chunk_id=hit.chunk_id, quote=cited.quote[:80])
```

**根因**

模型"顺手引用"——它标了一个位置相近的编号，但内容来自另一段，甚至是自己编的。**编号对、内容不对**，比编号错更难发现，因为形式上完全合规。

**修法**

```python
if cited.quote and cited.quote not in hit.text:
    dropped.append(cited.index)      # 或降级为"引用存疑"
```

本节代码还没做这一层（`quote` 只校验长度），**这是已知缺口**，阶段五的评估体系会正面处理。

**顺手加固**：`quote` 必须是 `hit.text` 的子串——这一条能拦掉大部分"顺口引用"。

### 坑 3：证据阈值换了 embedding 模型后失效

**现象**

把哈希向量换成真实 embedding 后，`no_evidence` 比例从 30% 暴涨到 80%：**大量本来答得出的问题被拦在闸门 1 外**。

**定位方法**

```python
print(f"evidence_score 分布: p50={...} p90={...}")
```

对比换模型前后的分布——会看到整体右移或左移。

**根因**

`rerank` 用的是"词项覆盖率"，与 embedding 无关，分数分布本不该变。但如果同时换了切分参数（块大小变了），覆盖率就会变——**块的边界变了，词项覆盖自然变**。

**修法**

阈值不是常量，是**需要标定的参数**：

```python
# settings 里配置，并记录标定日期与语料版本
rag_min_rerank_score: float = 0.15
```

**顺手加固**：每次换模型/换切分参数，跑一遍标定集（20 条有答案 + 20 条没答案），重算阈值。这件事阶段五会自动化。

---

## 10. 自检与作业

### 10.1 三个自检问题

<details>
<summary><b>Q1：为什么证据闸门要用 rerank 分而不是向量分？</b></summary>

向量分衡量整段文本的字面相似度，会被块长度和泛词稀释。实测"邮件附件上限"的向量分 0.217 比"食堂几点开饭"的 0.227 还低——用向量分做闸门必然误杀。rerank 分衡量"问题里的词在这段资料里出现了多少"，实测可答 ≥0.22、不可答 ≤0.10，能干净分开。

</details>

<details>
<summary><b>Q2：模型编造了引用，为什么要整段作废而不是删掉引用？</b></summary>

因为删掉引用只解决了"格式"，没解决"可信度"。用户仍会读到一个没有出处的断言，而它很可能是编的。**编造引用是一个强信号：这个模型的输出整体不可信**，所以整段作废、转人工，是最安全的选择。原始回答要留档，供事后追查。

</details>

<details>
<summary><b>Q3：解析失败为什么降级而不是报错？</b></summary>

"模型偶尔不按格式输出"是可预料的常态，不是故障。抛异常会让一次偶发问题变成一次用户可见的事故（500）。降级成纯文本 + 标记 `parse_ok=False`，把处理权交给上层，是更合理的分工。

</details>

### 10.2 动手任务：加"引用原文校验"

**要求**

在 `validate_citations` 里增加一条校验：**`quote` 必须是 `hit.text` 的子串**（允许忽略空白差异）。

1. 不匹配时，把该引用标记为 `suspicious`（新增字段），而不是直接丢弃。
2. `RagResult` 里暴露 `suspicious_citations: list[int]`。
3. 如果**所有**引用都 suspicious，按闸门 2 处理（整段作废）。
4. 补 3 个用例：quote 完全一致、quote 是改写的（应 suspicious）、quote 为空（不校验）。

**验收标准**

- 给 mock 加一种输出：引用编号正确但 quote 是编的（例如 `"index": 1, "quote": "重启路由器"`）。
- 用新逻辑跑，`suspicious_citations == [1]`，且回答被作废。
- 原有 78 个用例仍全绿。

**提示**：归一化时去掉所有空白再比较，否则"换行/空格差异"会造成大量误报。

### 10.3 拓展挑战

给 `ask.py` 加 `--stream` 模式：用 1.3 的 `client.stream()` 边生成边打印。

**思考**：

1. 流式下你拿不到完整 JSON，`citations` 校验怎么办？（提示：可以先做"受限解码"，或让模型先输出 `answer` 再输出 `citations`）
2. 如果校验失败，但用户已经看到了流式输出的前 100 个字，你打算怎么补救？

---

## 11. 本节小结与延伸

**三点回顾**

1. **prompt 只能"请求"模型守规矩，不能"保证"它守**。所以规矩必须写进代码：证据不足不调模型、引用造假整段作废。
2. **闸门要放在调用之前**：一旦让模型看到无关资料，它总能"找到"点什么（实测"公司年会"被答成 VPN 排查步骤，而且 `grounded=True`）。
3. **两种"没答案"必须用两句话**：`NO_EVIDENCE_ANSWER`（知识库缺内容）和 `REJECTED_ANSWER`（模型不可信）。混成一句，运维永远分不清该补文档还是该改模型。

**已知限制（诚实记录）**

- **`quote` 未做子串校验**：编号对但内容编造的引用拦不住（§9 坑 2，留作 §10.2 作业）。
- **mock 是脚本化的**：它证明链路正确，**不证明真实模型的行为**。真实模型的幻觉率要用阶段五的评估测。
- **阈值 0.15 只对本语料 + 本切分参数标定过**，换 embedding 或换切分都要重标。
- **无多轮对话**：每次提问独立，不记得上下文（阶段四解决）。

**M1 已达成**：能对知识库提问、给出带引用且可追溯的回答、查不到时明确说查不到。

**延伸阅读**

- 检索增强生成（RAG）原始论文：https://arxiv.org/abs/2005.11401
- 引用幻觉与归因（attribution）问题综述：https://arxiv.org/abs/2305.14627
- 结构化输出（JSON mode）最佳实践：https://platform.openai.com/docs/guides/structured-outputs

---

## 修订记录

- 2026-11-17 初稿：四个场景与 14 个测试全部实机运行，§7 为真实输出
- 2026-11-17 修正：证据闸门从"向量分 0.30"改为"rerank 分 0.15"——实测向量分无法区分可答与不可答（0.217 vs 0.227），rerank 分可分（≥0.22 vs ≤0.10）
- 2026-11-17 修正：模型编造引用时改为**整段回答作废**并保留 `raw_answer`，而不是只丢弃引用（原实现会在答案里残留 `[C9]` 字样）
- 2026-11-17 修正：`#hallucinate` 标记改为 `extra_instruction` 只进 prompt，不进检索（否则会稀释 rerank 分，连闸门 1 都过不了）
