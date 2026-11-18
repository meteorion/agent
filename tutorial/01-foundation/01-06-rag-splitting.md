---
section: "1.6"
title: "基础 RAG（一）：把文档切成检索友好的块"
stage: 1
status: done
depth: L2
difficulty: 2
minutes: 75
milestone: M1
depends_on: ["1.4"]
produces: ["rag/splitter.py"]
verifies: "能指出某个 chunk 为什么会导致后续答非所问"
part: null
updated: "2026-11-17"
---

# 1.6 基础 RAG（一）：把文档切成检索友好的块

## 0. 本节导航

**本节在链路中的位置**：RAG 三部曲的第一部。1.7 会把这里的 Chunk 变成向量，1.8 会用它拼 prompt。**切分错了，后面两步无论多好都救不回来**——这是 RAG 里投入产出比最高、却最常被跳过的一步。

**本节你将获得**

1. 一个按**标题层级**对齐的切分器，每块自带 `heading_path`（能追溯到「哪一章哪一节」）。
2. 代码块**不被切开**的实现（含原理：等长占位符）。
3. 相邻块 `overlap` 的实现，以及一个反直觉结论：**overlap 只在同一小节内生效**。
4. 一套切分质量指标（块数 / p90 / 断句处 / top1 是否命中答案）。
5. 用"极简检索器"实测：同一份语料，**切碎后 top1 就找不到答案了**。

**前置检查清单**

- 1.4 的 `uv run pytest` 是 28 passed。
- `knowledge/` 目录下有两篇 `.md`（本节会创建）。

**难度**：★★☆☆☆ ｜ **预计时长**：75 分钟 ｜ **本节费用**：**¥0**（纯文本处理，不调模型）

---

## 1. 为什么需要它（痛点先行）

几乎所有 RAG 教程的切分都是这三行：

```python
chunks = [text[i:i+1000] for i in range(0, len(text), 1000)]
```

能跑。然后你上线了，用户问："**VPN 提示认证失败怎么办？**"

系统给出的回答是：

> 认证失败通常有三类原因：密码过期、时间偏差、账号锁定。

用户追问："所以我该怎么办？"

**答案其实就在原文里**——但它被切到了下一个块：

```
块 #012：「…通常有三类原因：密码已过期 / 时间偏差 / 账号被锁定」
块 #013：「处理顺序：先在自助门户重置密码；若仍失败，请校准手机时间后重试…」
```

检索命中了 #012（它包含"认证失败"这几个字），于是模型只看到"原因"，**看不到"怎么办"**。这就是"答非所问"最典型的形态：不是模型笨，是**证据被切断了**。

本节实测复现了这个事故（见 §7）：同一份语料，`max_chars=120` 时 top1 **不含答案**，`max_chars=400` 时 top1 **含答案**。

**第二个痛点**：你根本不知道自己切得好不好。

大多数人切完就往下走了，从不检查"块里还有没有主语"。一个只有「处理顺序：先重置密码」而没有「VPN 认证失败」的块，会被任何问题的查询都命中一次——它是检索系统里的噪声源。

---

## 2. 核心概念与架构设计

### 2.1 类比：把一本书裁成索引卡

RAG 的切分，本质是把一本书裁成一张张索引卡：

- **不能把一句话裁成两半**：卡片 A 写着"因为密码过期"，卡片 B 写着"所以要重置密码"，两张卡单独看都不完整。
- **每张卡要写明出处**：卡上要有"第 4 章 §4.1"，否则读者不知道这张卡在讲 VPN 还是邮箱。
- **卡片不能太大**：一张卡塞了半本书，等于没做索引——检索命中它，你也读不完。

> **这个类比在哪失效**：裁纸刀不会"理解"内容，而切分器需要理解 **Markdown 结构**（标题、代码块、表格）。所以"切得对"靠的不是算法多聪明，而是**你有没有把结构信息用起来**。

### 2.2 切分要同时满足三个约束

| 约束 | 手段 | 违反的后果 |
|---|---|---|
| **结构完整** | 按标题切，代码块/表格保持原子 | 块失去主语；代码被切成两半 |
| **边界合理** | 优先在段落 → 句号处断开 | 一句话被切成两半，两边都读不懂 |
| **冗余可控** | 相邻块重叠 80 字 | 跨块的信息丢失；或块过大浪费 token |

### 2.3 切分管线

```
原始 Markdown
    │
    │ ① blank_out_fences()   把代码块替换成等长占位符（偏移不变）
    ▼
    │ ② parse_sections()     按 # 标题切出小节，记录 heading_path
    ▼
    │ ③ _atomic_spans()      段落 → 句子 → 硬切，得到不可再小的单元
    ▼
    │ ④ _pack()              贪心装箱到 max_chars，相邻块加 overlap
    ▼
    │ ⑤ _merge_tiny()        过短的碎片并入前一块
    ▼
  Chunk 列表（带 heading_path 与原文偏移）
```

### 2.4 三个参数

| 参数 | 含义 | 建议起点 | 调大的代价 |
|---|---|---|---|
| `max_chars` | 目标块大小 | 400–800 | 一块混多个主题，检索精度降、prompt 变贵 |
| `overlap_chars` | 相邻块重叠长度 | `max_chars` 的 15%–20% | token 重复计费；块可能以残缺表格开头 |
| `min_chars` | 最小块，过短则合并 | 60–100 | 设得比 `max_chars` 大 → 全部合并成一块（见 §8 坑 1） |

### 2.5 反直觉结论（**本节最值钱的一句**）

> **决定块边界的第一个因素不是 `max_chars`，而是文档的标题结构。**

实测（§7）：`max=400` 与 `max=1500` 切出来的块数**完全一样**（都是 26 块）。因为这份语料最大的小节也只有 390 字——`max_chars` 根本没机会生效。

**推论**：如果你的文档结构清晰（有标题层级），调 `max_chars` 的收益很小；如果文档是一大坨无标题的纯文本，`max_chars` 才会成为主要因素。**所以真正该先做的是把文档结构化，而不是调参数。**

### 2.6 适用范围（边界）

| 文档类型 | 适用 |
|---|---|
| Markdown / 带标题的文档 | ✅ 本节方法直接可用 |
| PDF（有版式无标题） | ⚠️ 需要先抽取标题与页码，`heading_path` 用页码替代 |
| 纯文本（无结构） | ⚠️ 退化到按段落/句子切，效果明显变差 |
| 代码仓库 | ❌ 应该按函数/类切，不是按字符 |

---

## 3. 环境与版本

本节**不新增依赖**（只用标准库 + 已有的 pydantic）。

| 组件 | 实测版本 | 说明 |
|---|---|---|
| pydantic | `2.14.3` | `Chunk` 模型 |
| Python | `3.12.13` | — |

**语料位置**：`servicedesk-agent/knowledge/`

```
knowledge/
├── email-faq.md      1414 字符
└── vpn-guide.md      2588 字符
```

两篇都是带标题层级、表格、代码块的真实感 IT 手册——**故意放这三种结构**，因为它们是切分最容易出事的地方。

**验证**

```powershell
uv run python -c "from pathlib import Path; print([p.name for p in Path('knowledge').glob('*.md')])"
```

---

## 4. 代码实战（3 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/rag/splitter.py` | **主角**：解析、切分、统计（下文内联核心部分） |
| `scripts/split_demo.py` | 三组参数 + 极简检索对比 |
| `tests/test_splitter.py` | 13 个断言 |

### 4.1 数据模型

```python
class Chunk(BaseModel):
    chunk_id: str          # vpn-guide#003
    doc_id: str
    order: int
    text: str              # 原始片段（不含标题）
    heading_path: list[str]  # ["VPN 使用与排障手册", "4. 常见故障", "4.1 提示「认证失败」"]
    char_start: int
    char_end: int

    @property
    def embedding_text(self) -> str:
        """拿去算向量的文本：标题路径 + 正文。"""
        if not self.heading_path:
            return self.text
        return " > ".join(self.heading_path) + "\n" + self.text
```

**`char_start` / `char_end` 是刻意保留的**：出问题时你能一秒定位回原文，而不是拿 `grep` 全文找。这对"用户投诉答错了"的排查至关重要。

### 4.2 保护代码块（等长占位符）

```python
def blank_out_fences(text: str) -> str:
    """把围栏代码块替换成等长的 NUL 字符。

    为什么是"等长替换"而不是删除：偏移完全不变，
    后面所有 span 仍能直接用来切原文。
    """
    return FENCE_RE.sub(
        lambda match: "\x00" * (match.end() - match.start()), text
    )
```

### 4.3 按标题切小节

```python
def parse_sections(md: str) -> list[_Section]:
    blanked = blank_out_fences(md)      # 代码块里的 "# 注释" 不会变成标题
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []   # (level, title)
    path_now: list[str] = []
    body_start = 0
    offset = 0

    for line in blanked.splitlines(keepends=True):
        heading = HEADING_RE.match(line.rstrip("\n"))
        if heading:
            if offset > body_start:
                sections.append(_Section(list(path_now), body_start, offset))
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path_now = [name for _, name in stack]
            body_start = offset + len(line)
        offset += len(line)

    if len(blanked) > body_start:
        sections.append(_Section(list(path_now), body_start, len(blanked)))
    return sections
```

`stack` 那几行是维护"标题层级"的关键：遇到 `###` 时把 `##` 及更深的弹出，`path_now` 永远是**从一级到当前**的完整路径。

### 4.4 拆成不可再小的单元

```python
def _atomic_spans(blanked: str, max_unit: int) -> list[tuple[int, int]]:
    units: list[tuple[int, int]] = []

    for start, end in _paragraph_spans(blanked):        # 先按空行分段
        if end - start <= max_unit:
            units.append((start, end))
            continue

        paragraph = blanked[start:end]
        sentences = [
            (start + m.start(), start + m.end())
            for m in SENTENCE_RE.finditer(paragraph)
            if m.end() > m.start()
        ]
        if len(sentences) <= 1:
            # 没有句子边界（典型的整块代码/表格）→ 保持原子，宁可超长
            units.append((start, end))
            continue

        for s, e in sentences:                          # 再按句末标点切
            if e - s <= max_unit:
                units.append((s, e))
            else:
                for cut in range(s, e, max_unit):       # 最后才硬切
                    units.append((cut, min(cut + max_unit, e)))

    return units
```

**三层降级**：段落 → 句子 → 硬切。**能不切就不切**，只有在当前粒度仍然超长时才往下一层走。

### 4.5 装箱与重叠

```python
def _pack(spans, max_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    if not spans:
        return []

    packed: list[list[int]] = []
    current_start, current_end = spans[0]

    for start, end in spans[1:]:
        if end - current_start <= max_chars:
            current_end = end            # 还装得下，继续塞
            continue
        packed.append([current_start, current_end])
        new_start = current_end - overlap_chars if overlap_chars else start
        new_start = max(0, min(new_start, start))
        current_start, current_end = new_start, end

    packed.append([current_start, current_end])
    return [(start, end) for start, end in packed]
```

### 4.6 完整文件

`save_chunks` / `load_chunks` / `chunk_stats` 等十行级工具函数见 [src/servicedesk/rag/splitter.py](../../servicedesk-agent/src/servicedesk/rag/splitter.py)（JSONL 落盘、统计指标）。

### 4.7 运行

```powershell
uv run python -m scripts.split_demo
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么必须按小节切，而不是按字符切

因为**小节是作者已经分好的语义边界**。作者写 `### 4.1 提示「认证失败」` 的时候，已经告诉你"这一节只讲这一件事"。

按字符切等于**丢掉作者给你的免费信息**，然后再用算法去猜边界。实测代价（§7）：`max=120` 时 41 个块、4 处断在句子中间，而且 **top1 找不到答案**。

### 5.2 为什么用"等长占位符"而不是删掉代码块

三个方案对比：

| 方案 | 偏移是否有效 | 代码块是否原子 |
|---|---|---|
| 直接删掉代码块 | ❌ 偏移全乱 | ✅ |
| 用 `[CODE]` 替换 | ❌ 长度变了 | ✅ |
| **等长 NUL 字符替换** | ✅ | ✅ |

等长替换的妙处：`len(blanked) == len(md)`，所以 `blanked` 上算出来的所有 span，可以直接拿去切 `md`。**偏移守恒**是整段代码能写得这么短的原因。

而代码块内部没有 `\n\n`（都被 NUL 填了）、也没有句号，所以段落切分和句子切分都不会碰它——它天然地成为一个原子单元。**这是"利用数据结构"而不是"加特判"**。

### 5.3 `_merge_tiny` 与那个 `min >= max` 的约束

```python
if min_chars >= max_chars:
    raise ValueError("min_chars 必须小于 max_chars")
```

这行校验是我在写测试时踩坑加上的：用 `max_chars=60`（没传 `min_chars`，默认 80）时，**每一块都被判定为"太短"，于是全部合并回一整块**。参数看起来生效了，实际完全没切。

当时的报错是：

```
AssertionError: 这个小节应该被切成多块
assert 1 >= 2
```

**这就是"参数非法时静默降级"的典型危害**：不报错、不警告，只是结果悄悄地不对。凡是"两个参数之间有关系"的地方，都该显式校验。

### 5.4 overlap 为什么只在小节内生效

```python
new_start = current_end - overlap_chars
```

`_pack` 是**按 section 分别调用**的，所以 `current_end` 一定还在同一小节内。跨小节时 `current_start` 会重置成新小节的第一段——**不加重叠**。

这是有意的：如果把上一节的尾巴带进下一节，新块的开头就是"上一节的内容"，检索时会误导。**overlap 的目的是接住被切断的句子，不是把两块粘起来。**

代价：跨小节的一句话（比如小节末尾的总结句）依然可能被切断。这属于"结构性取舍"，认了。

### 5.5 overlap 的副作用（实测）

看 §7 方案 C 的 top3 第 2 名：

```
3. 0.305 ✅ vpn-guide#006 § 3. 连接步骤
```

而方案 C 的 #009 开头是：

```
同时在线 | 退出其他设备上的客户端 | 排查时建议一次只改一项…
```

**它以上一行表格的残片开头。** 因为 overlap 把上一块最后 80 字原样搬了过来，而上一块恰好结束在表格中间。

这不是 bug 是取舍：你换来了"句子不被切断"，代价是"块开头可能不干净"。**如果你更在意块的可读性，就把 overlap 调小；更在意召回，就调大。**

### 5.6 `token_estimate` 为什么只敢叫"估算"

```python
return max(1, round(len(text) / 1.5))
```

中文大约 1.5 字符 / token，英文约 4 字符 / token。这个公式对中英混排的误差能有 ±30%。

**用途只有一个**：让你知道"大概多少 token"，从而估算 prompt 成本。**绝对不要拿它去核对账单**——那是 tokenizer 的活。

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：丢掉 heading_path

把 `Chunk.embedding_text` 改成只返回 `self.text`（等价于不保留标题层级），再跑同一个查询。

实测 top1 分数：

| 方案 | 仅正文 | 标题路径 + 正文 |
|---|---|---|
| A（max=120） | 0.410 | **0.621**（+51%） |
| B（max=400） | 0.413 | **0.646**（+56%） |

**小块获益更大（+51%）**。原因很直接：块越小，正文里越可能不含"VPN"这个词；而标题路径里一定有。

> 结论：**heading_path 不是装饰，它是小块的主语来源。** 如果你打算用很小的块（比如语义切分），那 heading 必须拼进 embedding 文本，否则召回会明显变差。

### 6.2 实验 B：用固定长度硬切，会切坏代码块

```python
chunks = [md[i:i+size] for i in range(0, len(md), size)]
```

扫描 28 种块大小（60–600，步长 20），统计"围栏数量为奇数"的块（= 代码块被切成两半）：

```
其中 15 种会把代码块切开：

坏块示例(size=60):
  '化」状态。\n\n安装完成后请校验版本，低于 5.2 的版本存在已知的重连缺陷：\n\n```powershell\n# 安装后校'
  围栏数量: 1 (奇数=不闭合)
```

**28 种尺寸里有 15 种会切坏。** 也就是说，你随手选一个 `chunk_size`，有 54% 的概率把代码块切碎，而且**没有任何报错**。

切碎后的后果：
- 这段代码在检索时既不能匹配"命令怎么写"，也不能匹配"版本要求"——它两边都不像
- 如果它被塞进 prompt，模型会看到半截代码，然后**自信地补全**（幻觉的高发场景）

**修法**：本节的三层切分 + 等长占位符。同一份语料，围栏不闭合的块数为 **0**。

---

## 7. 验证与预期输出

### 7.1 三组参数对比

```powershell
uv run python -m scripts.split_demo
```

实测（语料：2 篇，共 4002 字符）：

```
第 0 步：语料
  email-faq.md         1414 字符
  vpn-guide.md         2588 字符
  合计 2 篇，4002 字符

A 切碎   max=120 overlap=0
  块数 41   平均 86.2 字   中位 87   p90 120   最大 187   约 2356 tokens
  在句中断开的边界：4 处
    例：email-faq#002 结尾「…签 - 确认密码未过期 - 清除浏览器缓存后重试」
        email-faq#003 开头「这通常是客户端使用了旧的协议配置。请在账户设置中…」

  检索「VPN 提示认证失败怎么办」（匹配仅正文）top3：
    1. 0.410 ◐ vpn-guide#012  § 4.1 提示「认证失败」
       如果客户端提示认证失败，通常有三类原因： - 密码已过期（公司域账号每 90 天强制更换…
  检索「VPN 提示认证失败怎么办」（匹配标题路径 + 正文）top3：
    1. 0.621 ✅ vpn-guide#012  § 4.1 提示「认证失败」
  → top1 是否包含『怎么办』的实际答案（重置密码）：❌ 否，答案被切到下一块了

B 对齐   max=400 overlap=0
  块数 26   平均 136.9 字   p90 308   最大 390   约 2374 tokens
  在句中断开的边界：2 处
  检索…（仅正文）
    1. 0.413 ◐ vpn-guide#007  § 4.1 提示「认证失败」
  → top1 是否包含『怎么办』的实际答案（重置密码）：✅ 是

C 加重叠 max=400 overlap=80
  块数 26   平均 143.2 字   p90 309   最大 390   约 2484 tokens
  在句中断开的边界：2 处
  → top1 是否包含『怎么办』的实际答案（重置密码）：✅ 是

对比
  方案                               块数     平均   p90    最大  tokens    断句处    top1含答案
  A 切碎   max=120 overlap=0         41   86.2   120   187    2356      4          否
  B 对齐   max=400 overlap=0         26  136.9   308   390    2374      2          是
  C 加重叠 max=400 overlap=80        26  143.2   309   390    2484      2          是
```

**四行数字就是本节的全部结论**：

1. **A → B**：块数 41 → 26，`top1含答案` 从 **否** 变 **是**。切碎直接导致答非所问。
2. **B → C**：块数不变（26），tokens 从 2374 涨到 2484（+4.6%）——overlap 的成本很便宜。
3. **B 与 C 块数相同**：说明 `max_chars` 从 400 调到更大也不会变——**瓶颈在文档结构，不在参数**。

### 7.2 测试

```powershell
uv run pytest
```

```
.................................................                        [100%]
49 passed in 7.28s
```

（1.1–1.5 的 36 个 + 本节的 13 个。）

---

## 8. 生产环境踩坑实录

### 坑 1：参数看起来生效了，实际一块都没切

**现象**

把 `chunk_size` 从 1000 调到 300，重新索引后检索效果**一点变化都没有**。查 chunk 数量，发现和之前一模一样。

**定位方法**

```python
print(f"块数={len(chunks)} 最大块={max(c.char_len for c in chunks)}")
```

如果 `最大块 ≈ 全文长度`，说明根本没切。

**根因**

`min_chars >= max_chars`。`_merge_tiny` 会把每一块都判成"太短"，然后**一路合并回一整块**。整个过程不报错、不警告。

**修法**

```diff
+ if min_chars >= max_chars:
+     raise ValueError("min_chars 必须小于 max_chars")
```

**顺手加固**：切分完立刻跑一次 `chunk_stats`，把 `count` 和 `max` 打到日志里。**"块数没变"是这类问题的唯一信号**，而它只在统计指标里才看得见。

### 坑 2：代码块被切成两半，模型开始编命令

**现象**

用户问"怎么校验客户端版本"，系统给了一段**看起来像 PowerShell 但跑不了**的命令——括号不配对，或者参数名是编的。

**定位方法**

```python
broken = [c for c in chunks if c.text.count("```") % 2 == 1]
print(f"围栏不闭合的块：{len(broken)}")
if broken:
    print(repr(broken[0].text[:80]))
```

**根因**

按字符硬切，且切点落在代码块中间（实测 28 种尺寸里 15 种会这样）。

**修法**

用 `blank_out_fences` 做等长占位，让代码块在切分阶段就"不可分割"；并在测试里断言 `text.count("```") == 2`。

**顺手加固**：如果某块超长且是代码块，**宁可让它超过 `max_chars`**。超长只是贵一点，切碎是要命的。

### 坑 3：表格被切成两半，答案变成了没有表头的行

**现象**

用户问"连接后立刻断开是什么原因"，回答是：

> 退出所有代理与加速工具后重连。

**没有主语、没有条件**——用户完全不知道这是在说什么。

**定位方法**

找块开头是不是 `|` 或者以表格行开始的：

```python
suspicious = [c for c in chunks if c.text.lstrip().startswith("|")]
```

**根因**

表格靠 `|` 和换行构成结构，但**没有句号**。按句子切时它会被当成一个超长句，然后被硬切；`overlap` 又会把上一块的表尾带进下一块（§5.5 实测）。

**修法**

把表格当代码块处理：在 `blank_out_fences` 里一并做原子保护（识别连续的 `|` 开头行）。本节没实现，留作 §9.3 的挑战——但**你必须知道这个坑存在**，否则会以为是模型在胡编。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么按标题切比按固定长度切更好？</b></summary>

标题是作者已经做好的语义边界，用它等于"免费拿到人工标注"。按长度切完全丢弃这个信息，再用算法猜边界。实测同一语料：按长度切（120）后 top1 不含答案，按小节切（400）后 top1 含答案。

</details>

<details>
<summary><b>Q2：`max_chars` 从 400 调到 1500，块数为什么没变？</b></summary>

因为这份语料最大的小节只有 390 字，`max_chars` 从未被触发。**块边界由小节决定，`max_chars` 只是小节内的上限。** 这说明：文档结构比参数重要；如果你的块不够大，先去看文档有没有结构，而不是继续调数字。

</details>

<details>
<summary><b>Q3：为什么 overlap 只在同一小节内加？</b></summary>

overlap 的目的是"接住被切断的句子"。跨小节时，新小节的内容本身是完整的，把上一节的尾巴带进来反而会让新块开头出现无关内容，检索时误导。实测副作用见 §5.5：块会以残缺的表格行开头。

</details>

### 9.2 动手任务：给切分器加"按 token 切分"

**要求**

1. `split_markdown` 增加参数 `max_tokens: int | None = None`，与 `max_chars` 二选一（同时传则报错）。
2. 实现 `estimate_tokens` 的反向换算：`tokens_to_chars(n) ≈ n * 1.5`，用它把 `max_tokens` 折算成 `max_chars` 后复用现有逻辑。
3. 在 `chunk_stats` 里增加 `max_tokens` 与 `over_max_tokens`（超过 `max_tokens` 的块数）。
4. 补测试：`max_tokens=100` 时，绝大多数块 `token_estimate <= 100`。

**验收标准**

- `split_markdown(doc, "d", max_tokens=100)` 能跑通，`chunk_stats(chunks)["max_tokens"] <= 110`（允许代码块超一点）。
- 同时传 `max_chars` 和 `max_tokens` 时抛 `ValueError`。
- 新增 2 个用例，全绿。

**提示**：这其实就是生产里的真实做法——**按 token 而不是字符切**，因为你要控制的是 prompt 成本，不是字符数。

### 9.3 拓展挑战：保护表格

在 `blank_out_fences` 之外再加一层 `blank_out_tables`：识别连续以 `|` 开头的行（含分隔行 `|---|`），当做一个不可分割单元。

**思考**：

1. 表格可能长达 30 行、超过 `max_chars` 一倍，这时"保持原子"还是"允许切"？切的话怎么保证每块都带表头？
2. 如果一块里同时有表格和它的说明文字，`heading_path` 够不够表达"这是第几张表"？

---

## 10. 本节小结与延伸

**三点回顾**

1. **切分错了，后面全白搭**：实测切碎（max=120）后 top1 直接不含答案——模型不是笨，是证据被切断了。
2. **块边界主要由文档的标题结构决定，不是 `max_chars`**：`max=400` 与 `max=1500` 块数完全相同（26 块）。先修结构，再调参数。
3. **heading_path 是小块的主语**：把它拼进 `embedding_text`，top1 分数从 0.410/0.413 涨到 0.621/0.646（+51% / +56%）。

**已知限制（诚实记录）**

- **表格未做原子保护**：长表格仍可能被切开，块可能以残缺表头开头。
- **overlap 只在小节内**：跨小节的总结句仍可能被切断。
- **token 是估算**：`len/1.5` 对中英混排误差可达 ±30%。
- **只支持 Markdown**：PDF / HTML 需要先抽取标题与结构。

**延伸阅读**

- Markdown 标题语法（本文档的解析依据）：https://www.markdownguide.org/basic-syntax/
- RAG 切分策略综述（chunk size 与召回的关系）：https://www.pinecone.io/learn/chunking-strategies/
- Pydantic 模型与属性：https://docs.pydantic.dev/latest/concepts/models/

---

## 修订记录

- 2026-11-17 初稿：三组参数与"极简检索器"对比全部实机运行，§7 为真实输出
- 2026-11-17 修正：`split_markdown` 增加 `min_chars < max_chars` 校验（默认值组合会导致"全部合并回一块"，测试时踩到）；补 §8 坑 1
- 2026-11-17 修正：`chunk_stats` 去掉无意义的 `lossless` 字段
- 2026-11-17 补充：§6.2 围栏破损扫描（28 种尺寸中 15 种切坏）、§5.5 overlap 导致残缺表格开头的实测
