---
section: "4.1"
title: "先别急着多 Agent：什么时候单图就够"
stage: 4
status: done
depth: L2
difficulty: 2
minutes: 70
milestone: M4
depends_on: ["3.7"]
produces: ["multiagent/single_vs_multi.py"]
verifies: "用同一任务跑单图与多 Agent，比较成本/延迟/成功率"
part: null
updated: "2026-11-18"
---

# 4.1 先别急着多 Agent：什么时候单图就够

## 0. 本节导航

**本节在链路中的位置**：阶段四开篇，也是**反直觉的一节**。3.7 已经把图拆成子图了，接下来顺理成章该讲"多 Agent 协作"——但先停一下：**你确定需要吗？**

本节用 5 个任务、两套实现、四个指标，把这个决定变成可测量的问题。

**本节你将获得**

1. 一把尺子：**成功率 / 延迟 / 交接次数 / 交接上下文量**。
2. 实测：同一个任务集上，多 Agent **正确率相同、延迟 1.75 倍、多出 15 次交接**。
3. 一个"多 Agent 真的赢"的场景：**工具上下文省 61%**（用你自己仓库的 5 个真实 schema 算的）。
4. 该拆 / 不该拆各三个信号，以及一张决策表。
5. 一个"判分器必须能判错"的自检——否则 100% 通过率毫无意义。

**前置检查清单**

- 3.7 的 `uv run python -m scripts.composite_demo` 能跑通。
- 知道 `StateGraph` 的 `add_conditional_edges`（3.3）。

**难度**：★★☆☆☆ ｜ **预计时长**：70 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

多 Agent 是过去两年最容易"架构先行"的地方。常见的起点不是问题，而是：

- "别家都在用 supervisor + worker"
- "拆开更好维护"
- "以后要扩展"

这些话**可能都对**，但它们不是判据。判据应该是：

> **拆开之后，哪个指标变好了？变好了多少？**

如果不拆也能跑、拆了只有成本，那这个拆分就是**拿账单换架构图**。

本节的做法：**同一个任务集，写两套实现，量四个数**。

---

## 2. 核心概念与架构设计

### 2.1 类比：一个人搬 vs 三个人接力

一个人搬 3 个箱子：走过去、搬回来、走过去、搬回来……

三个人接力：A 搬到门口、交给 B、B 搬到电梯、交给 C……

**接力多了"交接"这个动作**：每一次交接都要把箱子放下、说清楚是什么、再拿起。
**箱子越重（上下文越大），交接成本越高。**

> **这个类比在哪失效**：接力至少能并行；而本节的多 Agent 是**串行调度**
> （supervisor → worker → supervisor → …），**没有并行收益，只有交接成本**。
> 想要并行收益，得让多个 worker 同时跑——那是 4.3 的内容。

### 2.2 四个指标

| 指标 | 怎么量 | 为什么重要 |
|---|---|---|
| **成功率** | 最终答复是否命中期望特征 | 正确性不能被架构牺牲 |
| **延迟** | 墙钟耗时 | 用户感知 |
| **交接次数** | Agent 之间的消息传递次数 | 每次都是一次"可能丢信息"的机会 |
| **交接上下文** | 每次交接序列化后的字节数 | token 成本的代理指标 |

### 2.3 两套实现

**单图**（4 个节点，共享 state）：

```
classify → retrieve → draft → review
```

**多 Agent**（supervisor + 3 个工人）：

```
              ┌────────────┐
              │ supervisor │◄────────┐
              └─────┬──────┘         │
      ┌─────────────┼─────────────┐  │
      ▼             ▼             ▼  │
 researcher      writer       reviewer┘
      │             │             │
      └─────────────┴─────────────┘
        每次回 supervisor 都要交接
```

**关键差别**：多 Agent 每次交接都要把上下文**序列化成一个消息**再传过去。
本节用 `HANDOFF_COST` 累计这些字节。

### 2.4 多 Agent 真正的收益

不是"分工好看"，而是：

> **每次调用时，prompt 里的工具 schema 变小了。**

本节实测（用仓库里 5 个真实工具的 schema）：

| | 每次调用的 schema |
|---|---|
| 单 Agent（带全部 5 个工具） | **3260 字节** |
| 多 Agent · asset 组（2 个工具） | 1230 字节 |
| 多 Agent · ticket 组（2 个工具） | 1361 字节 |
| 多 Agent · knowledge 组（1 个工具） | 669 字节 |

三次调用的总计：**9780 → 3809 字节，省 61.1%**。

**这才是拆分的正当理由**：工具越多、越专业，省得越多。

### 2.5 适用范围（边界）

| 场景 | 选择 |
|---|---|
| 线性 3 步、工具 < 5 | 单图 |
| 工具分 3 组、权限各不同 | 多 Agent |
| 要并行探索多个方案 | 多 Agent（且要真并行，见 4.3） |
| 审计要求上下文隔离 | 多 Agent（合规驱动，不是性能） |
| 不确定 | **先单图**，留好拆分点 |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 |
|---|---|
| langgraph | `1.2.22` |
| Python | `3.12.13` |

**验证**

```powershell
uv run python -c "from servicedesk.multiagent.single_vs_multi import compare; print('ok')"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/multiagent/single_vs_multi.py` | **主角**：两套实现 + 跑分 + 工具上下文对比 |
| `scripts/single_vs_multi_demo.py` | 五步演示 |
| `tests/test_single_vs_multi.py` | 11 个断言 |

### 4.1 任务集（带判分标准）

```python
@dataclass(frozen=True)
class Task:
    ticket_id: str
    title: str
    description: str
    expected_intent: str
    expected_keyword: str     # 最终答复里必须出现
    needs_approval: bool
```

**`expected_keyword` 是关键**：没有它，"成功率"就只能靠人看，无法自动跑分。

### 4.2 交接成本怎么算

```python
def _handoff(state, sender, receiver, payload):
    """Agent 之间的一次交接：把上下文序列化后塞进消息列表。

    真实多 Agent 框架里，这一步就是"把历史重新喂进另一个 prompt"——
    **成本主要来自这里**，而不是来自"多了一个 Agent"。
    """
    message = {"from": sender, "to": receiver, **payload}
    HANDOFF_COST["bytes"] += len(json.dumps(message, ensure_ascii=False).encode())
    return {"messages": [message], "next_agent": receiver, "trace": [f"handoff:{sender}->{receiver}"]}
```

### 4.3 supervisor 调度

```python
def _supervisor(state):
    if not state.get("findings"):
        return {"next_agent": "researcher", ...}
    if not state.get("draft"):
        return {"next_agent": "writer", ...}
    if not state.get("final"):
        return {"next_agent": "reviewer", ...}
    return {"next_agent": "done", ...}
```

**注意这是"轮询式"调度**：supervisor 反复被调用，每次只看"谁还没做完"。
它比单图多出来的，就是这些来回。

### 4.4 跑分

```python
async def compare(tasks=None) -> dict[str, Any]:
    single = await run_single(tasks)
    multi = await run_multi(tasks)
    return {"single": single.as_dict(), "multi": multi.as_dict()}
```

### 4.5 运行

```powershell
uv run python -m scripts.single_vs_multi_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么要"两套都写"

因为**只有跑过才有资格说"不需要"**。

如果你只写单图，别人问"为什么不多 Agent"，你只能回答"我觉得没必要"。
写了两套、量了四个数，你的回答就变成：

> "这个任务集上正确率一样，延迟 1.75 倍，多 15 次交接。"

**这是一句无法反驳的话。**

### 5.2 为什么延迟只差 1.75 倍，不是 3 倍

因为多 Agent 的额外成本主要是**调度与交接**，不是"多做了 3 倍工作"——
三个工人干的是同一件事的三段，不是三件事。

**1.75 倍**这个数字很典型：它不足以让功能不可用，但足以让**成本翻倍的地方**（LLM 调用）
变成真正的钱。

### 5.3 判分器必须能判错

这是本节最容易被忽略的自检。看这条测试：

```python
async def test_scorer_detects_wrong_answer() -> None:
    impossible = [Task("T-bad", "VPN 连不上", "认证失败", "vpn",
                       "这句话不会出现在任何答复里", True)]
    single = await run_single(impossible)
    assert single.success == 0 and single.failures
```

**如果这条测试不能通过，前面"100% 成功率"就毫无意义**——因为判分器可能永远返回 True。

**规则：先证明你的尺子能量出"错"，再用它量"对"。**

### 5.4 `recursion_limit` 为什么必须给

多 Agent 是 `supervisor ↔ worker` 的**多跳循环**。本节实测 5 个任务共 15 次交接，
平均每任务 3 跳。

虽然默认上限（3.3 实测 **10007**）远够，但显式传 `recursion_limit: 40` 有两个好处：

- 一旦调度逻辑写错（比如 supervisor 永远返回 `researcher`），能**快速失败**而不是跑 10007 步
- 让"我预期最多 40 跳"这个设计意图出现在代码里

### 5.5 为什么工具上下文要用"真实 schema"

`compare_tool_context()` 没有硬编码字节数，而是**从 `build_default_registry()` 读真实 schema**：

```python
    registry = build_default_registry()
    schemas = {item["function"]["name"]: item for item in registry.schemas()}
```

**好处**：你新增一个工具，重跑这个函数就知道"多 Agent 能省多少"变了没有。
**写死的数字会过期，算出来的不会。**

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：判分器写松，得到"100% 成功"的假象

把判分改成"只要 `final` 非空就算成功"：

```python
ok = bool(final)     # ← 几乎永远为 True
```

所有实现都会得到 100%。**这就是很多"多 Agent 成功率更高"的评测的来源。**

本节的做法：判分必须看**具体特征**（`"25 MB"`、`"补充信息"`），并且有一条测试专门验证
"不可能满足的任务会失败"。

### 6.2 实验 B：只看架构、不看指标

如果不量这四个数，你会得到这样的结论：

> "多 Agent 结构更清晰，职责更分明，建议采用。"

量了之后：

```
成功率              5/5 (100%)              5/5 (100%)
总延迟(ms)          16.7                    29.3
交接次数            0                       15
交接上下文(字节)    0                       2508
延迟倍增: 1.75x
```

**"更清晰"是真的，"更贵"也是真的。** 决策应该是把两者摆在一起做，而不是只讲一半。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.single_vs_multi_demo
```

实测（节选）：

```
第 1 步：两套实现的结构
  单图     节点: ['classify', 'retrieve', 'draft', 'review']
  多 Agent 节点: ['supervisor', 'researcher', 'writer', 'reviewer']

第 2 步：同一批 5 个任务跑两遍
  指标                单图                    多 Agent
  --------------------------------------------------------------
  成功率              5/5 (100%)              5/5 (100%)
  总延迟(ms)          16.7                    29.3
  交接次数            0                       15
  交接上下文(字节)    0                       2508

  延迟倍增: 1.75x
  多出的交接: 15 次，累计搬运 2508 字节

第 3 步：多 Agent 真正赢的地方
  可用工具数: 5
  单 Agent 每次调用要带全部 schema: 3260 字节
    三次调用合计                  : 9780 字节
  多 Agent 每个专家只带自己那组:
    asset      1230 字节
    ticket     1361 字节
    knowledge  669 字节
    合计（含路由清单 ×3）        : 3809 字节
  节省比例: 61.1%
```

跑测试：

```powershell
uv run pytest
```

```
........................................................................ [ 99%]
.                                                                        [100%]
289 passed in 34.15s
```

---

## 8. 生产环境踩坑实录

### 坑 1：评测 100% 通过，上线却一半失败

**现象**

内部评测集上单图 98%、多 Agent 99%，看起来多 Agent 更好。上线后多 Agent 的真实成功率低得多。

**根因**

评测集的判分器太松（只看"有没有输出"），而这个任务集上**多 Agent 的失败模式**是
"信息在交接中丢了"——输出依然有，只是答错了内容。

**修法**

判分器必须检查**具体特征**，并且要有"故意不可能满足"的用例（§5.3）。

### 坑 2：把"多 Agent"当成组织架构的投影

**现象**

团队有 5 个组，就设计了 5 个 Agent 互相交接。结果每个 Agent 只能看到自己那部分上下文，
遇到跨组问题就互相踢皮球。

**根因**

把**人的组织边界**当成了**上下文边界**。多 Agent 的正当理由是"上下文要变小"，
不是"组织结构长这样"。

**修法**

回到 §2.5 的表：只有"工具/权限差异大""要并行""合规隔离"三条才拆。

### 坑 3（本节实测中顺手修掉的）：`httpx.ReadError` 的偶发失败

**现象**

跑全量测试时偶尔出现：

```
FAILED tests/test_mcp_http.py::test_unknown_path_returns_404 - httpx.ReadError
FAILED tests/test_raw_client.py::test_chat_without_key_raises_401 - httpx.ReadError
```

单跑这两个用例又都通过。

**定位方法**

`ReadError` 表示"连接在读响应时断了"。看服务端：这两个用例都走**提前返回**的分支
（404 / 401），**没读请求体就返回了**。

**根因**

客户端还在发 body（`Content-Length > 0`），服务端已经写响应并关连接 → 触发 RST →
客户端读不到完整的响应。

**修法**

在服务端**返回任何提前响应之前先排空请求体**：

```python
    def _drain_body(self) -> None:
        """把请求体读完（幂等）。已经读过或没有 body 就什么都不做。"""
        if getattr(self, "_body_drained", False):
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            try:
                self.rfile.read(length)
            except OSError:
                pass
        self._body_drained = True
```

并在 `_json()` 里统一调用——**所有提前返回的响应都走同一条路**。

**教训（比这个 bug 本身更重要）**：**"重跑一次就过了"不等于问题不存在。**
我一开始重跑通过了，如果就此放过，这个 flake 会在 CI 里随机红几个月。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：多 Agent 在什么情况下真的比单图好？</b></summary>

按本节实测，收益来自"**每次调用的上下文变小**"（工具 schema 省 61%）。所以三个信号是：① 工具/权限差异大，每个人只该看到自己那组；② 要独立并行探索多个方向；③ 合规要求隔离上下文。都不是"分工好看"。

</details>

<details>
<summary><b>Q2：为什么判分器必须能判错？</b></summary>

因为"100% 通过"可能意味着"尺子坏了"，而不是"实现对了"。必须先有一条"不可能满足的任务会失败"的测试，才能相信其它用例的通过数字。这是本节 `test_scorer_detects_wrong_answer` 的作用。

</details>

<details>
<summary><b>Q3：为什么延迟只差 1.75 倍而不是 3 倍？</b></summary>

因为多 Agent 干的是"同一件事的三段"，不是"三件事"。额外成本来自调度轮转与上下文交接，不是重复劳动。但 1.75 倍放在 LLM 调用上就是真金白银——每次交接都是一次额外的 prompt。

</details>

### 9.2 动手任务：给你的业务补一份"该不该拆"的评估

**要求**

1. 从你手上挑一个真实流程，写出 5 个任务（含 1 个"无资料"的边界情况）。
2. 写单图版本，量四个指标（成功率/延迟/交接次数/上下文量）。
3. 写多 Agent 版本（supervisor + 2–3 个 worker），量同样四个指标。
4. 再跑一次 `compare_tool_context()`，看你仓库里工具拆组能省多少。
5. 给出一页结论：拆 / 不拆 / 先等等，并附四个数字。

**验收标准**

- 判分器有"能判错"的自检（否则数据无效）。
- 四个指标都来自实际运行，不是估算。
- 结论里必须包含"如果拆，我能得到什么、付出什么"。
- 原有 289 个用例仍全绿。

**提示**：如果你的任务集上多 Agent 的成功率**更高**，先检查是不是判分器或任务集分布的问题——
本节 5 个任务上两者都是 100%，说明这个任务集**区分度不够**。

### 9.3 拓展挑战

给评测集加"对抗性任务"：同一个意图，但描述里包含误导性关键词（比如"我的邮箱收不到 VPN 的邮件"）。

**思考**：

1. 单图会不会因为关键词冲突而误判意图？
2. 多 Agent 的 `researcher` 与 `writer` 会各自误判一次吗？（上下文丢失会放大误差）
3. 这类任务上，谁的失败更"可解释"？

---

## 10. 本节小结与延伸

**三点回顾**

1. **先单图，量四个数再决定**：本节的线性任务集上，多 Agent 正确率相同、延迟 1.75 倍、多 15 次交接。
2. **多 Agent 真正赢在"每次调用的上下文变小"**：用仓库真实 schema 算，省 **61.1%**。
3. **判分器必须先能判错**——否则 100% 通过率只是尺子坏了。

**已知限制（诚实记录）**

- **无真实 LLM**：调度是规则式的，所以"多 Agent 可能更聪明"这一点本节无法验证。
- **任务集区分度不足**：5 个任务上两套都是 100%，说明这个集子测不出质量差异。
- **没有并行**：本节的多 Agent 是串行轮转，**没有**并行收益（4.3 会做真并行）。
- **交接成本是字节数代理**：不是真实 token 数（没有 tokenizer）。

**延伸阅读**

- Anthropic《Building effective agents》（"先用最简单的方案"）：https://www.anthropic.com/research/building-effective-agents
- LangGraph 多 Agent 概念：https://langchain-ai.github.io/langgraph/concepts/multi_agent/
- 上下文工程（上下文越长越贵也越差）：https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

---

## 修订记录

- 2026-11-18 初稿：两套实现 + 11 个测试全部实机运行；四个指标与工具上下文对比均为实测
- 2026-11-18 修正：全量测试偶发两次 `httpx.ReadError`（404/401 分支未排空请求体导致 RST），在 `http_app._json()` 与服务端统一排空，连跑 3 次全绿
- 2026-11-18 记录：工具上下文对比改为读取**真实 schema**（而非硬编码），避免数字过期
