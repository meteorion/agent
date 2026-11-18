---
section: "4.4"
title: "双层记忆（一）：短期会话与压缩"
stage: 4
status: done
depth: L2
difficulty: 4
minutes: 100
milestone: M4
depends_on: ["4.3"]
produces: ["memory/short_term.py"]
verifies: "压缩后关键约束仍在（工单号/审批状态/用户禁忌）"
part: null
updated: "2026-11-18"
---

# 4.4 双层记忆（一）：短期会话与压缩

## 0. 本节导航

**本节在链路中的位置**：阶段四的"记忆"专题开始。前四节讲的是"多个 Agent 怎么分工"，本节开始讲"**一个 Agent 怎么记得住**"。

短期记忆解决"这一轮对话塞不下"；4.5 的长期记忆解决"下次对话还记得你"。

**本节你将获得**

1. 为什么预算要按 **token** 算，而不是按消息条数。
2. 三种裁剪策略：**window / summary / hybrid**，以及它们各自丢什么。
3. 一个刺眼的实测结论：**window 和 summary 都丢了 2 个关键约束**，而多花 3 倍 token 的 summary **丢的东西一样多**。
4. `hybrid` 的核心不是"摘要更好"，而是"**抽取约束再钉住**"。
5. 两个真实的坑：`要不要` 被误判成禁令；忘了 pin 的 hybrid 会退化。

**前置检查清单**

- 3.2 的 `messages` reducer 已理解（本节是"何时把 messages 砍掉"的答案）。
- 4.3 的 `run` 能跑通。

**难度**：★★★★☆ ｜ **预计时长**：100 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

一个跑了 30 轮的客服会话，`messages` 会涨到几千 token。然后你遇到两件事：

**① 贵**：每一轮都要把全部历史重新喂给模型，第 30 轮的费用是第 1 轮的几十倍。

**② 更糟的是会丢**：上下文窗口有上限，超了就要裁。而**怎么裁，决定了你的 Agent 会不会忘掉关键信息**。

真实的翻车长这样：

> 用户第 3 轮说过"**不要重置我的密码**"，第 25 轮 Agent 把这条裁掉了，
> 第 30 轮它建议"帮您重置密码"。用户已经强调过一次，但它不记得了。

**本节就是要把"裁掉什么"从"随手 `messages[-10:]`"变成一个**有依据、可验证**的决定。**

---

## 2. 核心概念与架构设计

### 2.1 类比：会议纪要

- **window = 只留最后 10 分钟录音**：近的都在，前面全没了
- **summary = 把前面几小时压成一页纪要**：省地方，但**纪要写的人不知道哪句重要**
- **hybrid = 纪要 + 一张"决议清单"**：纪要保大意，清单（谁负责、不许做什么）单独钉住

> **这个类比在哪失效**：会议纪要是人写的，人能判断什么重要；
> 本节用的摘要器是**规则式**的（取首句 + 截断），它**没有重要性判断**。
> 这恰恰说明问题：**就算换成 LLM 摘要，它也可能自信地丢掉细节**——所以约束必须单独抽出来。

### 2.2 预算按 token，不按条数

实测这一段会话：

```
消息条数: 17
当前 token: 292
平均 17.2 token/条
```

**为什么不能用条数**：一条工具返回的 JSON 可能单条上千 token，而十条寒暄加起来才 50。
**按条数裁剪，等于按"信封数量"而不是"信纸厚度"决定扔什么。**

### 2.3 三种策略

| 策略 | 做法 | 保留什么 |
|---|---|---|
| **window** | 丢最旧的，留最近 N 条 | 只有近处 |
| **summary** | 把旧的全压成一句 | 大意 + 近处 |
| **hybrid** | **约束钉住** + 中段摘要 + 近处原样 | 要命的 + 大意 + 近处 |

### 2.4 实测：谁把约束弄丢了

同一段会话（埋了 2 个约束：工单号 `T-1001`、禁令"不要重置密码"）：

| 策略 | 压缩后 token | 丢弃条数 | 丢失的约束 |
|---|---|---|---|
| window | **50** | 14 | `ticket:T-1001`、`prohibit:不要重置我的密码` |
| summary | 135 | 14 | `ticket:T-1001`、`prohibit:不要重置我的密码` |
| hybrid | 174 | 11 | **（无）** |

**注意 summary 那一行**：它比 window **贵了 2.7 倍**（135 vs 50），
**丢掉的东西一模一样**。

> 这是本节最重要的一句话：**摘要不等于保真。**
> 摘要器省的是"字数"，不是"风险"——它不知道哪句话丢了会出事故。

### 2.5 hybrid 到底做了什么

```
保留的消息（pinned 的约束 + 最近 3 条）：
  📌 user      我的工单号是 T-1001，后面请用它称呼。
  📌 assistant 收到，工单号 T-1001。
  📌 user      注意：不要重置我的密码，我有重要会议。
     user      再问一个：VPN 在第3个办公区要不要额外配置？
     assistant 第3个办公区不需要额外配置，用默认端口即可。
     user      帮我总结一下现在的情况。
```

**关键设计**：约束不是"摘要出来"的，是**抽出来再钉住**的。

- **摘要**负责"保住大意"
- **pinned** 负责"保住要命的那几条"

### 2.6 适用范围（边界）

| 场景 | 做法 |
|---|---|
| 会话 < 10 轮 | 别裁，裁了反而丢信息 |
| 有工单号/审批状态/禁忌 | ✅ hybrid + pinning |
| 纯闲聊 | window 就够 |
| 单条工具返回就超预算 | ❌ 裁剪救不了，得**单条截断**（§8.3） |
| 需要跨会话记忆 | 那是 4.5（长期记忆） |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 |
|---|---|
| Python | `3.12.13`（`re` / `dataclasses`） |

**验证**

```powershell
uv run python -c "from servicedesk.memory.short_term import BudgetedHistory; print('ok')"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/memory/short_term.py` | **主角**：token 估算 + 约束抽取 + 三种策略 |
| `scripts/short_term_memory_demo.py` | 五步演示 |
| `tests/test_short_term_memory.py` | 20 个断言 |

### 4.1 token 估算

```python
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def estimate_tokens(text: str) -> int:
    """粗估：CJK 约 1 token/字，ASCII 约 1 token/4 字符。

    没有 tokenizer 时的务实做法——用于**比值与预算**，不是精确计费。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, round(cjk + other / 4))
```

### 4.2 约束抽取

```python
_CLAUSE = r"[^，。！？；\n]{0,24}"
# 用负向前瞻排除「要不要」（那是疑问，不是禁止）
_PROHIBIT_RE = re.compile(rf"(?<!要)不要{_CLAUSE}|禁止{_CLAUSE}|不得{_CLAUSE}")
_MUST_RE = re.compile(rf"必须{_CLAUSE}|一定要{_CLAUSE}|务必{_CLAUSE}")
```

### 4.3 hybrid 压缩

```python
    def _hybrid(self) -> tuple[list[Message], str, int]:
        """约束全留 + 中段摘要 + 最近若干条原样。"""
        pinned = [message for message in self.messages if message.pinned]
        tail = [message for message in self.messages[-self.keep_recent :] if not message.pinned]
        middle = [m for m in self.messages[: -self.keep_recent] if not m.pinned]
        summary = _summarize(middle) if middle else ""
        kept = pinned + tail
        return kept, summary, len(middle)
```

### 4.4 运行

```powershell
uv run python -m scripts.short_term_memory_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么 `pin_all_constraints()` 要单独一步

因为它**会改变消息**（给它们打 `pinned=True`）。把它做成显式调用有两个好处：

- 你能在压缩**之前**先看一眼"哪些会被钉住"
- 你能在测试里验证"钉对了吗"，而不是压缩完才知道

**如果把它藏进 `compress()` 里**，就像让压缩函数偷偷改你的数据——出问题很难查。

### 5.2 为什么摘要器要"刻意做笨"

```python
def _summarize(messages: list[Message], max_chars: int = 120) -> str:
    """确定性摘要：取每条的首句，拼到上限。

    **刻意做得"笨"**：真实的 LLM 摘要会更好，但也会**自信地丢掉细节**。
    用笨摘要来演示"摘要会丢约束"这件事，结论只会更保守。
    """
```

如果我用一个"聪明的摘要器"，读者会问："换成 GPT 是不是就不会丢了？"
**答案是不会保证**——LLM 摘要同样会压缩细节，而且**更难预测它丢了什么**。

用笨摘要器是为了让结论**不依赖摘要质量**：只要摘要是"有损的"，就必须靠 pinning 兜底。

### 5.3 `lost_constraints` 怎么判

```python
    @property
    def lost_constraints(self) -> list[Constraint]:
        kept_texts = " ".join(message.content for message in self.kept) + " " + self.summary
        return [item for item in self.constraints_before if item.text not in kept_texts]
```

**判据是"约束短语还在不在最终上下文里"**——不是"语义是否保留"。

这是**保守**的判定（可能误报"丢了"），但工程上正确：**宁可多钉一条，也不要漏钉一条**。

### 5.4 压缩应该在什么时候触发

实测（预算 150）：

```
第 4 轮后:  127 tokens
第 5 轮后:  158 tokens  ← 超预算，该压缩了
  压缩: 158 → 128 tokens，丢弃 7 条
第 6 轮后:  159 tokens  ← 又超了
```

**注意第 5 轮和第 6 轮都触发了压缩**——因为压到 128 之后，再加一轮就又到 159。

**经验值**：到预算 **70%** 就压，压到 **40%** 左右。给下一轮留余量。
本节 demo 是"到 100% 才压"，所以连续两轮都在压——**能工作，但很抖**。

### 5.5 为什么 `window` 连 pinned 都不管

```python
    def _window(self) -> tuple[list[Message], str, int]:
        """只保留最近的，pinned 的也**不特殊对待**（这是它的弱点）。"""
        kept = self.messages[-self.keep_recent :]
        return kept, "", len(self.messages) - len(kept)
```

这是**故意的**：window 的定义就是"只看近处"。
如果你让它也尊重 pinned，它就不是 window 了——**别把一个策略改到失去特征，然后说它没用。**

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：用消息条数当预算

```python
if len(messages) > 20:
    messages = messages[-10:]
```

**问题**：一条 2000 token 的工具返回，和一条 10 token 的"好的"，在条数上等价。

**实测对照**（本节会话）：平均 17.2 token/条，但**方差极大**——
一条工具 JSON 就可能顶 100 条寒暄。

**改法**：按 token 算（§4.1）。

### 6.2 实验 B：只做摘要，不钉约束

实测 summary 策略：

```
summary   135 tokens   dropped=14   lost=['ticket:T-1001', 'prohibit:不要重置我的密码']
```

**它花了 2.7 倍的 token（vs window 的 50），却丢了同样两个约束。**

**这不是"摘要做得不够好"，而是"摘要这个动作本身不区分重要性"。**

### 6.3 实验 C：忘了 pin 的 hybrid

实测（第 4 步的计时段）：

```
第 5 轮后:  158 tokens  ← 超预算，该压缩了
  压缩: 158 → 128 tokens，丢弃 7 条，丢失约束 1 个
  ⚠️ 丢了约束！因为这段没调 pin_all_constraints()
```

**`hybrid` 依赖 `pinned` 标记。忘了 pin，它就退化成 window。**

**修法**：把 `pin_all_constraints()` 放进压缩流程（或做成 `compress(pin=True)` 的默认行为）。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.short_term_memory_demo
```

实测（节选）：

```
第 1 步：先看预算 —— 按 token 算，不按条数
  消息条数: 17
  当前 token: 292   预算: 300
  抽到的约束（2 条）：
    [ticket  ] T-1001
    [prohibit] 不要重置我的密码
  被标记为 pinned 的消息: 3 条

第 2 步：三种策略
  策略      压缩后 token    丢弃条数    丢失的约束
  window    50              14          ticket:T-1001、prohibit:不要重置我的密码
  summary   135             14          ticket:T-1001、prohibit:不要重置我的密码
  hybrid    174             11          （无）

第 3 步：hybrid 为什么能保住
  保留的消息（pinned 的约束 + 最近 3 条）：
    📌 user      我的工单号是 T-1001，后面请用它称呼。
    📌 assistant 收到，工单号 T-1001。
    📌 user      注意：不要重置我的密码，我有重要会议。
       user      再问一个：VPN 在第3个办公区要不要额外配置？
       ...

第 5 步：约束抽取的坑
  不要重置我的密码    → prohibit:不要重置我的密码
  VPN 要不要额外配置？ → （未识别）
  禁止导出客户名单    → prohibit:禁止导出客户名单
  这个必须今天处理完   → must:必须今天处理完
```

跑测试：

```powershell
uv run pytest
```

```
...................................................                      [100%]
339 passed in 42.86s
```

---

## 8. 生产环境踩坑实录

### 坑 1：压缩把"不要重置密码"压没了

**现象**

用户投诉"我强调过不要重置密码，它还是重置了"。日志里能看到用户确实说过。

**定位方法**

把"压缩前后的上下文"都打出来，搜关键短语：

```python
log.info("compress", strategy="summary",
         kept=[m.content[:20] for m in result.kept],
         summary=result.summary,
         lost=[c.text for c in result.lost_constraints])
```

如果 `lost` 非空——就是这个坑。

**根因**

只做了摘要，没做约束抽取与钉住。

**修法**

hybrid + `pin_all_constraints()`，并在 CI 里断言"关键约束不丢"。

**顺手加固**：把"约束是否丢失"做成监控指标（本节 `CompressionResult.lost_constraints` 就是为此设计的）。

### 坑 2：`要不要` 被当成禁令，pinned 越积越多

**现象**

压缩后 token 几乎没降——因为一半消息都被标成 pinned。

**根因**

```python
if "不要" in text:      # ← "要不要" 也匹配
```

**修法**

用正则 + 负向前瞻：

```python
_PROHIBIT_RE = re.compile(rf"(?<!要)不要{_CLAUSE}|禁止{_CLAUSE}|不得{_CLAUSE}")
```

实测：

```
VPN 要不要额外配置？ → （未识别）
```

**顺手加固**：给约束抽取写一组"该识别/不该识别"的用例（本节 5 条）。

### 坑 3：单条超长消息压不掉

**现象**

压缩后仍然超预算，而且 `dropped=0`。

**根因**

"一条 5000 token 的工具返回"没法靠"扔掉别的消息"解决——**它自己就是问题**。

**修法**

- **单条截断**：超过 N token 的条目先截断，保留头尾
- **落盘引用**：大结果写文件/对象存储，上下文里只留 `{"ref": "...", "summary": "..."}`
- 工具层就限制返回体积（2.1 的 `content` 早就该截断）

**顺手加固**：本节留了一条测试把这个限制**固定下来**（`test_single_oversized_message_cannot_be_compressed`），
免得有人以为"加了压缩就万事大吉"。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么预算要按 token 算而不是按条数？</b></summary>

因为消息长度方差极大：一条工具返回的 JSON 可能上千 token，十条寒暄可能才 50。按条数裁剪等于按"信封数量"决定扔什么，而不是按"信纸厚度"。本节实测平均 17.2 token/条，但单条可以差两个数量级。

</details>

<details>
<summary><b>Q2：为什么 summary 比 window 贵得多，却可能丢一样多的东西？</b></summary>

因为摘要器**不区分重要性**，它只按"先出现什么"截断（LLM 摘要也同理，只是更不可预测）。实测：window 50 token 丢 2 个约束，summary 135 token 也丢同样 2 个。摘要省的是字数，不是风险。

</details>

<details>
<summary><b>Q3：hybrid 靠什么保住约束？</b></summary>

不是靠"更好的摘要"，而是靠**抽取 + 钉住**：先用规则把工单号/禁令/必须项抽成 `Constraint`，把对应消息标 `pinned=True`，压缩时 pinned 一律保留。摘要只负责中段大意。

</details>

### 9.2 动手任务：把"压缩"接进 3.x 的图

**要求**

1. 写一个 `compress_node`：读 `messages`，按预算判断是否需要压缩，需要则调 `hybrid`。
2. 在 3.2 的 `TicketState` 里加 `pinned_facts: list[str]`，压缩后把约束写进去（不依赖消息本身残留）。
3. 让下游节点优先读 `pinned_facts`，而不是从 `messages` 里搜。
4. 补 3 个用例：不超预算不压缩、超预算压缩且约束进 `pinned_facts`、压缩后下游仍能拿到工单号。

**验收标准**

- 压缩节点是**幂等**的（连续调用两次，第二次不该再变）。
- `pinned_facts` 在压缩后**只增不减**。
- 原有 339 个用例仍全绿。

**提示**：第 2 条是关键——**约束不该依赖"消息恰好没被裁掉"**，它应该有自己的存储位。

### 9.3 拓展挑战

给约束抽取加"时效"：工单号 24 小时后失效，"不要重置密码"跟着工单走（工单关单后失效）。

**思考**：

1. 失效后的 pinned 消息要不要主动解钉？不解会怎样（token 越来越高）？
2. 长期记忆（4.5）里存的约束，和短期的 `pinned_facts` 冲突时以谁为准？
3. 怎么测试"失效"？（提示：注入时钟，别用 `time.time()`）

---

## 10. 本节小结与延伸

**三点回顾**

1. **预算按 token 算**：实测平均 17.2 token/条，但方差极大；按条数裁等于瞎裁。
2. **window 和 summary 都丢了 2 个约束**（工单号 + 禁令），而 summary 贵了 2.7 倍却一样丢——**摘要不等于保真**。
3. **hybrid 靠"抽取 + 钉住"**：摘要保大意，pinned 保要命；但**忘了 pin 就退化成 window**（实测丢了 1 个约束）。

**已知限制（诚实记录）**

- **约束抽取是规则式**：只认工单号/审批/禁令/必须四类，语义级约束（"别用邮件，用电话"）认不出。
- **摘要器是规则的**：不是 LLM 摘要（刻意的，见 §5.2）。
- **单条超长消息压不掉**（§8.3，已用测试固定）。
- **没有接进图**：本节只做了独立的记忆模块，`compress_node` 是 §9.2 作业。

**延伸阅读**

- Anthropic：上下文工程（上下文越长，越贵也越差）：https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- LangGraph 消息裁剪与摘要：https://langchain-ai.github.io/langgraph/how-tos/memory/add-summary-conversation-history/
- MemGPT 论文（分层记忆的原始动机）：https://arxiv.org/abs/2310.08560

---

## 修订记录

- 2026-11-18 初稿：五步演示与 20 个测试全部实机运行；三种策略的 token 与约束丢失均为实测
- 2026-11-18 修正：约束抽取初版用 `"不要" in text`，把「要不要」误判成禁令（pinned 越积越多）；改为正则短语 + 负向前瞻
- 2026-11-18 修正：`_summary` 初版会保留 pinned 消息，导致它与 hybrid 结果相同、对比失去意义；改为"摘要器不区分重要性"（这才是它的真实特征）
- 2026-11-18 补充：新增"忘了 pin 的 hybrid 会退化"实测与测试；新增"单条超长消息压不掉"的限制测试
