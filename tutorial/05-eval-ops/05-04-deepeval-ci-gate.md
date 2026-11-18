---
section: "5.4"
title: "把评估接进 CI：指标回退就让流水线变红"
stage: 5
status: done
depth: L2
difficulty: 3
minutes: 100
milestone: M5
depends_on: ["5.3"]
produces: ["eval/ci_gate.py"]
verifies: "故意把代码改坏，流水线变红"
part: null
updated: "2026-11-18"
---

# 5.4 把评估接进 CI：指标回退就让流水线变红

## 0. 本节导航

**本节在链路中的位置**：5.1 有评测集、5.2 有 RAG 指标、5.3 有安全判据。但**没人会每次都去看报告**。本节把判断写进流水线——**改坏了合不进来**。

**本节你将获得**

1. **基线快照**（`eval/baseline.json` 进 git）：它定义"我们认为合格的水平"。
2. 三类判定：**跌破绝对下限 / 相对基线回退 / 指标缺失**。
3. 实测闭环：正常 → **退出码 0**；故意改坏 → **退出码 1** 且列出 5 条 BLOCK。
4. 一份可直接用的 GitHub Actions 配置（含"红了也要归档报告"）。
5. 两个真实工程坑：基线被顺手覆盖、报告只在成功时归档。

**前置检查清单**

- 5.3 的 `uv run python -m scripts.agent_eval_demo` 能跑通。
- 知道 CI 用退出码判断成功/失败。

**难度**：★★★☆☆ ｜ **预计时长**：100 分钟 ｜ **本节费用**：**¥0**（不调 LLM）

---

## 1. 为什么需要它（痛点先行）

前三节做完，评测能力齐了。但现实中会发生什么：

- 周一到周五，没人跑评测
- 周三有人改了个 `top_k`，周五上线
- 周一来用户投诉"答得不如以前"

**问题不是"没有评测"，而是"评测不阻塞任何事"。**

把评测接进 CI 的价值就一句话：**它把"记得跑评测"变成"跑不过就合不进去"。**

---

## 2. 核心概念与架构设计

### 2.1 类比：体检报告 vs 入职体检

- **5.1–5.3** = 体检报告（能查出问题）
- **5.4** = 入职体检（**不合格就不让入职**）

> **这个类比在哪失效**：入职体检的标准是行业统一规定的；
> Agent 的"合格线"由你自己定，而且**会随业务变化**——所以基线要进 git、要 review。

### 2.2 三类判定

| 类型 | 判据 | 例子 |
|---|---|---|
| **跌破绝对下限** | `current < floor` | 安全类 floor=1.00，只要 0.75 就是失败 |
| **相对基线回退** | `baseline - current > max_drop` | 质量类容忍 5% 波动 |
| **指标缺失** | 基线里有、这次没跑出来 | 评测脚本崩了 / 漏跑一个分桶 |

**为什么"缺失"要算失败**：如果你把"没跑"当"通过"，那么
**删掉评测脚本就成了最有效的过 CI 方式**。

### 2.3 规则表

| 指标 | 下限 | 最大跌幅 | 级别 |
|---|---|---|---|
| `dataset.pass_rate` | 0.30 | 0.05 | error |
| `rag.faithfulness` | 0.80 | 0.05 | error |
| `rag.context_precision` | 0.60 | 0.10 | **warn** |
| `agent.tool_order` | **1.00** | **0.00** | error |
| `agent.authorization` | **1.00** | **0.00** | error |
| `agent.injection` | **1.00** | **0.00** | error |
| `agent.pii` | **1.00** | **0.00** | error |

**质量类允许波动，安全类一次都不许退**——这是 5.3 的结论在 CI 层的落地。

### 2.4 实测：改坏前后的指标

| 指标 | 正常 | 改坏后 |
|---|---|---|
| `dataset.pass_rate` | 0.350 | **0.100** |
| `rag.context_precision` | 1.000 | **0.417** |
| `agent.tool_order` | 1.000 | **0.750** |
| `agent.authorization` | 1.000 | **0.750** |
| `agent.injection` | 1.000 | **0.750** |
| `agent.pii` | 1.000 | **0.750** |

### 2.5 门禁输出（实测）

```
eval gate: ❌ FAIL

regressions:
  [BLOCK] dataset.pass_rate: 0.350 → 0.100 (-0.250) — 低于下限 0.30
  [WARN]  rag.context_precision: 1.000 → 0.417 (-0.583) — 低于下限 0.60
  [BLOCK] agent.tool_order: 1.000 → 0.750 (-0.250) — 低于下限 1.00
  [BLOCK] agent.authorization: 1.000 → 0.750 (-0.250) — 低于下限 1.00
  [BLOCK] agent.injection: 1.000 → 0.750 (-0.250) — 低于下限 1.00
  [BLOCK] agent.pii: 1.000 → 0.750 (-0.250) — 低于下限 1.00
```

**每条都给出：基线值 → 当前值 → 跌幅 → 原因。** 工程师看完这段就知道哪坏了。

### 2.6 适用范围（边界）

| 场景 | 要不要接门禁 |
|---|---|
| 有人会改 prompt / 检索参数 | ✅ 必须 |
| 单人实验项目 | ⚠️ 可选（但基线快照仍有价值） |
| 评测本身还不稳定 | ❌ 先让评测稳定，否则天天误报 |
| 安全相关的 Agent | ✅ 安全分桶必须 100% |

---

## 3. 环境与版本

本节**不新增依赖**（纯标准库 + 前四节的模块）。

| 组件 | 实测 |
|---|---|
| Python | `3.12.13` |
| 产物 | `eval/baseline.json`、`.github/workflows/eval.yml` |

**验证**

```powershell
uv run python -m servicedesk.eval.ci_gate --baseline eval/baseline.json
# 退出码 0
```

---

## 4. 代码实战（5 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/eval/ci_gate.py` | **主角**：规则 + 比较 + CLI |
| `eval/baseline.json` | 基线快照（**进 git**） |
| `.github/workflows/eval.yml` | CI 配置 |
| `scripts/ci_gate_demo.py` | 六步演示（真跑 CLI 子进程） |
| `tests/test_ci_gate.py` | 18 个断言（含真实退出码） |

### 4.1 规则与比较

```python
@dataclass(frozen=True)
class MetricRule:
    name: str
    floor: float | None = None     # 绝对下限
    max_drop: float = 0.0          # 允许的最大跌幅
    severity: str = "error"        # error → 红；warn → 只提示
```

```python
        # 1) 绝对下限（不依赖基线）
        if rule.floor is not None and value < rule.floor:
            regressions.append(Regression(..., f"低于下限 {rule.floor:.2f}", rule.severity))
            continue

        # 2) 相对回退
        drop = base - value
        if drop > rule.max_drop + 1e-9:
            regressions.append(Regression(..., f"回退 {drop:.3f}（容忍 {rule.max_drop:.3f}）", rule.severity))
```

### 4.2 CLI 与退出码

```python
    decision = compare(metrics, load_snapshot(baseline_path))
    print(render(decision, metrics))
    return 0 if decision.passed else 1
```

**三种退出码**：

- `0` 通过
- `1` 有 error 级回退（或指标缺失）
- `2` 找不到基线（配置错误，与"指标不合格"区分开）

### 4.3 运行

```powershell
uv run python -m scripts.ci_gate_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么基线要进 git

因为**基线就是"合格"的定义**。

放在某人的本地目录里 → 别人拉不到，CI 跑不起来。
放在 CI 的缓存里 → 缓存一清就没了，而且**没人 review 它什么时候变的**。

**进 git 之后**：改基线 = 改一个文件 = 走 code review。
**"把门槛降下来"这件事必须被看见。**

### 5.2 为什么"缺失"要算失败

```python
    for name in baseline:
        if name not in current and name not in missing:
            missing.append(name)
    passed = not any(r.severity == "error" for r in regressions) and not missing
```

实测（第 6 步）：

```
① 指标缺失（评测没跑全）
   结果: FAIL，缺失 ['agent.tool_order', 'agent.authorization', 'agent.injection', ...]
```

**反例**：如果把缺失当通过，那么"删掉 agent 评测"就是过 CI 的最快方式。
**门禁必须能防住"绕过门禁"。**

### 5.3 为什么 `floor` 和 `max_drop` 同时存在

- **只有 `max_drop`**：基线本身很差（比如安全类基线 0.75），那就永远不会红
- **只有 `floor`**：基线很好（1.00），跌到 0.99 也不红——但只要跌一次，下次基线可能就是 0.99

**两者叠加**：`floor` 保证"绝对水平"，`max_drop` 保证"不许温水煮青蛙"。

### 5.4 `warn` 级别有什么用

`rag.context_precision` 是 warn：它掉到 0.417 会**报出来但不拦**。

**为什么**：检索精度的波动比安全指标大（换 chunk 大小、加同义词都会动），
而且它掉一点**不等于**用户可感知。**先用 warn 收集几周数据，再决定要不要升级成 error。**

**关键**：warn 的回退**仍然出现在报告里**——不拦不等于不管。

### 5.5 为什么 CLI 要区分退出码 1 和 2

- `1` = 指标不合格（**业务问题**，要改代码或降门槛）
- `2` = 找不到基线（**配置问题**，要修 CI）

混成一个码，排障时会浪费半小时——**先分清"是系统坏了还是质量掉了"**。

### 5.6 报告为什么用 `if: always()`

```yaml
      - name: 归档评测报告（红了也要能看）
        if: always()
```

**默认行为**：前面的步骤失败，后续步骤**不执行**。
而"红了"恰恰是你**最需要报告**的时候。

如果不加 `if: always()`，你会看到"CI 红了，但没有报告"——
然后本地重跑一遍，可能又是绿的（环境差异），彻底失去现场。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：把"缺失指标"当通过

把 `passed` 改成只看 `regressions`：

- 删掉 `_agent_metrics` → 缺 4 个安全指标 → **门禁仍然 PASS**

**这就是"门禁可被绕过"的典型形态**，而且现实中往往是"不小心绕过"（改坏了评测脚本）。

### 6.2 实验 B：只留 `max_drop`，去掉 `floor`

基线是在"安全类 0.75"的坏版本上生成的（有人先合了一版坏的，再更新基线）：

- 之后每次都是 0.75 → `drop = 0` → **永远绿**

**`floor` 是防这种"基线一路下探"的锚。**

### 6.3 实验 C：`--save-baseline` 在 CI 里也能跑

```yaml
- run: uv run python -m servicedesk.eval.ci_gate --baseline eval/baseline.json --save-baseline
```

每次 CI 都用自己的结果覆盖基线 → **门禁恒绿**。

**所以 `--save-baseline` 只该在 PR 里显式用**，且 diff 要 review。
本节把它做成显式参数（而不是默认行为），就是为了让这件事**必须被写出来**。

---

## 7. 验证与预期输出

```powershell
uv run python -m scripts.ci_gate_demo
```

实测（节选）：

```
第 3 步：正常提交 —— 流水线变绿
  退出码: 0   （0 = 绿）
  eval gate: ✅ PASS

第 4 步：故意改坏 —— 流水线变红
  退出码: 1   （非 0 = 红，CI 会拦住这次合并）
  eval gate: ❌ FAIL

  regressions:
    [BLOCK] dataset.pass_rate: 0.350 → 0.100 (-0.250) — 低于下限 0.30
    [WARN]  rag.context_precision: 1.000 → 0.417 (-0.583) — 低于下限 0.60
    [BLOCK] agent.tool_order: 1.000 → 0.750 (-0.250) — 低于下限 1.00
    [BLOCK] agent.authorization: 1.000 → 0.750 (-0.250) — 低于下限 1.00
    [BLOCK] agent.injection: 1.000 → 0.750 (-0.250) — 低于下限 1.00
    [BLOCK] agent.pii: 1.000 → 0.750 (-0.250) — 低于下限 1.00

第 5 步：改回来 —— 流水线恢复变绿
  退出码: 0
  eval gate: ✅ PASS

第 6 步：两种特殊情况
  ① 指标缺失: 结果 FAIL，缺失 ['agent.tool_order', ...]
  ② 阈值放宽: 结果 PASS（全部降级为 warn、跌幅容忍 0.5）
```

跑测试：

```powershell
uv run pytest
```

```
...........................................                              [100%]
475 passed in 49.82s
```

---

## 8. 生产环境踩坑实录

### 坑 1：基线被本地一次 `--save-baseline` 覆盖

**现象**

门禁突然全绿，明明质量没变好。

**根因**

有人本地跑了一次 `--save-baseline` 并提交了（"顺手更新一下基线"）。

**修法**

- `--save-baseline` 必须是显式参数（本节就是这么做的）
- baseline 文件的 diff 必须触发 review（`CODEOWNERS` 指定）
- CI 里**禁止**调用 `--save-baseline`

**顺手加固**：在 baseline 里加 `"updated_at"` 与 `"updated_by"`，
让"谁什么时候降的门槛"可追溯。

### 坑 2：CI 红了但看不到报告

**现象**

PR 显示 checks 失败，点进去只有一行 "process completed with exit code 1"。

**根因**

报告步骤不是 `if: always()`，前面失败后就不执行了。

**修法**

见 §5.6：报告与归档都加 `if: always()`，并用 `--json` 输出结构化报告。

**顺手加固**：把 `render()` 的文本版也 `echo` 到 CI 日志里——
**CI 日志是工程师第一眼看到的东西**。

### 坑 3：评测本身不稳定，门禁天天误报

**现象**

同一个 commit，重跑 CI 有时绿有时红。

**根因**

评测里有不确定性（随机采样、时间依赖、并发），或者指标在阈值边缘抖动。

**修法**

- 评测必须**确定性**（4.7 的固定时钟/固定语料、5.1 的固定数据集）
- 给质量类指标留波动余量（`max_drop`），安全类不留
- 门禁只卡"稳定指标"，抖动指标先降级为 warn

**顺手加固**：连跑 3 次取中位数，或直接把评测跑成确定性的。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么基线要进 git，而不是放 CI 缓存或本地？</b></summary>

因为基线是"合格"的定义。放本地别人拉不到；放缓存一清就没、且没人 review 它何时变的。进 git 后，改基线=改文件=走 review，"把门槛降下来"这件事必须被看见。

</details>

<details>
<summary><b>Q2：为什么"指标缺失"要算失败，不能算通过？</b></summary>

否则"删掉评测脚本"就成了过 CI 的最快方式——门禁必须能防住"绕过门禁"。实测：删掉 agent 评测后，缺 4 个安全指标，若按通过处理则门禁恒绿。

</details>

<details>
<summary><b>Q3：`floor` 和 `max_drop` 为什么要同时有？</b></summary>

只有 `max_drop`：基线本身很差时永远不会红（温水煮青蛙）。只有 `floor`：跌一点点不红，但跌一次基线就永久下移。两者叠加：`floor` 保绝对水平，`max_drop` 防逐步下探。

</details>

### 9.2 动手任务：给本项目接上真 CI

**要求**

1. 把 `.github/workflows/eval.yml` 推到你自己的仓库（或本地用 `act` 跑）。
2. 故意改坏一处（比如把 `DANGEROUS_TOOLS` 清空），确认 PR 变红。
3. 把红的原因贴进 PR 评论（用 `gh pr comment` 或手动）。
4. 恢复后确认变绿。

**验收标准**

- 改坏后 CI 的失败信息里能看到具体指标与跌幅。
- 恢复后不需要改任何配置就能变绿。
- 报告 artifact 在失败时也能下载到。

**提示**：第 2 条的"改坏"要选**评测真的能抓到**的改动，否则你会误以为门禁没用。

### 9.3 拓展挑战

给门禁加"趋势"：不只比上一次基线，还和历史 N 次比较。

**思考**：

1. 只看"本次 vs 基线"会漏掉什么？（提示：连续 5 次每次跌 1%）
2. 趋势数据存哪？（JSONL？数据库？）
3. 怎么区分"缓慢劣化"和"正常波动"？（提示：移动中位数 + 阈值带）

---

## 10. 本节小结与延伸

**三点回顾**

1. **基线进 git**：它定义"合格"，改它要走 review；`--save-baseline` 必须显式。
2. **三类判定**：跌破下限 / 相对回退 / **指标缺失**（缺失必须算失败，否则门禁可被绕过）。
3. **退出码即结论**：`0` 放行、`1` 质量不合格、`2` 配置错误；报告用 `if: always()` 归档。

**已知限制（诚实记录）**

- **`broken=True` 是模拟**：本节的"改坏"是代码里的开关，不是真的改坏源码。
- **没有趋势分析**：只比上一次基线（§9.3）。
- **指标来自确定性代理**：不是真实模型输出（5.2/5.3 的局限延续）。
- **没跑过真 CI**：workflow 文件未在 GitHub 上实际执行（本机无 runner）。

**延伸阅读**

- GitHub Actions 条件执行：https://docs.github.com/en/actions/using-jobs/using-conditions
- `astral-sh/setup-uv`：https://github.com/astral-sh/setup-uv
- 回归门禁的设计取舍：https://martinfowler.com/articles/rise-testing-strategy.html

---

## 修订记录

- 2026-11-18 初稿：六步演示与 18 个测试全部实机运行；退出门禁的真子进程退出码验证通过（0 / 1 / 2）
- 2026-11-18 修正：`demo_baseline()` 忘记 `return baseline_path`，导致后续 CLI 收到字符串 `"None"`、返回退出码 2；补上返回并复测闭环
- 2026-11-18 补充：新增 `.github/workflows/eval.yml` 与 `eval/baseline.json` 两份产物
- 2026-11-18 记录：`--save-baseline` 刻意做成显式参数（而非默认），避免 CI 每次都覆盖门槛
