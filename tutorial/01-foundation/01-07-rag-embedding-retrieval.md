---
section: "1.7"
title: "基础 RAG（二）：embedding、向量库与检索"
stage: 1
status: done
depth: L2
difficulty: 3
minutes: 100
milestone: M1
depends_on: ["1.6"]
produces: ["rag/store.py"]
verifies: "能演示「无 rerank vs 有 rerank」的 top-3 差异"
part: null
updated: "2026-11-17"
---

# 1.7 基础 RAG（二）：embedding、向量库与检索

## 0. 本节导航

**本节在链路中的位置**：接 1.6 切好的块，把它们变成**可检索**的东西。1.8 会把检索结果拼进 prompt。本节也是阶段三、四的基础——工单 Agent 的每一次回答，都要先经过这里。

**本节你将获得**

1. 一个 `Embedder` 接口 + 两种实现：本地哈希（零成本）与远程 API（批量 + 并发 + 错误分类）。
2. 用 **Qdrant 内存模式**建库、写入、按 `doc_id` 过滤、top-k 检索——`API 与线上完全一致`。
3. 用阈值把"最像的噪声"挡在门外，并说清"检索为空是正常结果"。
4. 一个可解释的 rerank，实测把**错的 top1 换对**（`email-faq#002` → `email-faq#001`）。
5. 三个真实事故的复现：`hash()` 跨进程漂移、换模型不重建索引、把空检索当异常。

**前置检查清单**

- 1.6 的 `uv run pytest` 是 49 passed。
- `knowledge/` 下两篇 `.md` 存在。

**难度**：★★★☆☆ ｜ **预计时长**：100 分钟 ｜ **本节费用**：**¥0**（本地哈希向量 + 内存向量库）

---

## 1. 为什么需要它（痛点先行）

1.6 结束时你手里有 26 个块。最直接的做法是：**把 26 块全塞进 prompt**。

算一下：26 块 × 143 字 ≈ 3700 字 ≈ 2500 token。一次问答就 2500 token 输入，1000 次问答就是 250 万 token。**而且 99% 的内容与问题无关**——真正相关的那一块，被埋在 25 块噪声里。

于是你要做检索。然后会遇到三个新问题：

**问题 A：什么算"足够相关"？**

不设阈值时，用户问"食堂几点开饭"，系统依然会返回 5 条"最像的"结果——实测最高分 **0.227**（而真正相关的查询是 **0.414**）。这 5 条全是噪声，但下游不知道，照样拼进 prompt 让模型"基于以上资料回答"。**模型只能硬编。**

**问题 B：top1 对了，但 top2/top3 全是凑数的。**

实测查询"VPN 提示认证失败怎么办"，向量检索的 top3 里有一条是文档开头的介绍段（`vpn-guide#000`）：

> 本文档面向全体员工，介绍公司 VPN 的安装、连接与常见故障处理。

它包含"VPN""安装""连接""故障处理"——**几乎什么都能匹配一点**。这种"泛化块"是 RAG 里最隐蔽的噪声源。

**问题 C：top1 干脆就是错的。**

实测查询"邮件附件上限"，向量 top1 是 `email-faq#002 §2.1 网页版无法登录`（0.387），而真正含答案的 `§1 邮箱容量与附件限制`（0.274）排在第三。

**分数更高的那个，答案是错的。** 这就是纯向量检索的局限：它比字面相似度，而"网页版无法登录"和"附件上限"共享"邮箱/邮件/上限"这些词。

本节要解决的正是这三件事：**阈值**、**过滤**、**rerank**。

---

## 2. 核心概念与架构设计

### 2.1 类比：图书馆的索引卡

- **embedding** = 给每张索引卡分配一个"主题坐标"。主题相近的卡片，坐标也接近。
- **向量库** = 按坐标排列的卡片柜，能"找出离这个坐标最近的 N 张卡"。
- **阈值** = "距离超过这个范围就别拿了，宁可说没找到"。
- **rerank** = 把拿出来的 10 张卡**重新翻一遍**，按"真的能回答问题"重新排序。

> **这个类比在哪失效**：坐标是模型算出来的，**它可能把"苹果公司"和"苹果水果"放在一起**。所以"坐标近"不等于"语义对"——这就是为什么后面还需要 rerank，以及阶段五的评估体系。

### 2.2 完整链路

```
文档 → [1.6 切分] → Chunk（带 heading_path）
                        │
                        │ embed(chunk.embedding_text)
                        ▼
                  向量（256 维）
                        │
                        │ upsert
                        ▼
              ┌──────────────────┐
              │   Qdrant 集合     │  payload: chunk_id/doc_id/heading/text
              └──────────────────┘
                        │
   用户问题 → embed → 查询向量
                        │
                        │ query_points(top_k × 2, filter=…)
                        ▼
                   候选（含 score）
                        │
                        │ min_score 阈值过滤
                        ▼
                   命中（可能为空！）
                        │
                        │ rerank（可选）
                        ▼
                   top-3 → [1.8 拼 prompt]
```

### 2.3 三个关键决策

| 决策 | 本节的取值 | 为什么 |
|---|---|---|
| 用什么向量 | 本地哈希（可换远程） | 教学要跑通链路，不是选模型 |
| 检索多少 | `top_k × 2` 再截断 | 阈值/rerank 会淘汰一些，多取点才够 |
| 阈值多少 | 0.30 | 实测相关 0.414 vs 无关 0.227，取中间 |

### 2.4 rerank 的定位（**最容易误解的一点**）

> **rerank 只能重排已经召回的候选，不能召回新的。**

实测（§7 案例 2）：rerank 前的 top3 里恰好包含了正确答案（排第 3），所以 rerank 能把它提到第 1。

但如果正确答案**根本不在 top5 里**，rerank 无能为力。所以工程顺序永远是：

1. 先保**召回率**（`top_k` 取大一点、切分别太碎）
2. 再用 **rerank** 提**精确率**

**反过来做（top_k 很小 + 指望 rerank 救）是无效的。**

### 2.5 适用范围（边界）

| 场景 | 需要向量库吗 |
|---|---|
| < 100 块 | ⚠️ 不需要，直接暴力算余弦更快 |
| 100 – 10 万块 | ✅ 需要 |
| 多租户 / 权限隔离 | ✅ 必须用 payload 过滤，不要靠"检索后过滤" |
| 精确匹配（工单号、错误码） | ❌ 用关键词/倒排，向量反而不准 |

> 本节只有 26 块，严格说"不需要向量库"。之所以还是建，是因为阶段三、四会扩到上千块，**现在把接口定下来，后面只换数据量**。

---

## 3. 环境与版本

```powershell
uv add qdrant-client
```

实测新增：

```
 + qdrant-client==1.20.0
 + numpy==2.5.5
 + protobuf==7.36.2
 + portalocker==4.4.2
 + urllib3==2.8.0
```

| 组件 | 实测版本 | 说明 |
|---|---|---|
| qdrant-client | `1.20.0` | 支持 `location=":memory:"` 本地模式 |
| numpy | `2.5.5` | qdrant 本地模式的依赖 |
| httpx | `0.28.1` | 远程 embedding |

**关键验证：本地模式能不能跑**

```powershell
uv run python -c "from qdrant_client import QdrantClient; c=QdrantClient(location=':memory:'); print('ok', c)"
```

能打印 `ok` 就说明不用起 docker，也不花钱。

---

## 4. 代码实战（4 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/rag/embedder.py` | 向量化：本地哈希 + 远程批量 |
| `src/servicedesk/rag/store.py` | **主角**：建库、检索、过滤、阈值、rerank |
| `scripts/rag_demo.py` | 五步演示 |
| `tests/test_rag_store.py` | 15 个断言 |

### 4.1 `Embedder` 接口与本地实现

```python
class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...


def hash_vector(text: str, dim: int = EMBED_DIM) -> list[float]:
    """字符二元组 → CRC32 分桶 → L2 归一化。

    为什么用 crc32 而不是内置 hash()：Python 的 hash() 每个进程加随机盐，
    同一份文本换次进程就得到不同向量，索引会直接失效。
    """
    vector = [0.0] * dim
    for index in range(len(text) - 1):
        gram = text[index : index + 2]
        vector[zlib.crc32(gram.encode("utf-8")) % dim] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]
```

### 4.2 远程实现：批量与并发

```python
    async def embed(self, texts: list[str]) -> list[list[float]]:
        semaphore = asyncio.Semaphore(self.concurrency)
        batches = self._batches(texts)
        results: list[list[float]] = []

        async with httpx.AsyncClient(base_url=..., timeout=...) as client:
            async def run(batch: list[str]) -> list[list[float]]:
                async with semaphore:
                    return await self._embed_batch(client, batch)

            # gather 保持顺序，即使并发完成的时间不同
            for chunk in await asyncio.gather(*(run(batch) for batch in batches)):
                results.extend(chunk)
        return results
```

### 4.3 向量库封装（核心部分）

```python
def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))


class QdrantStore:
    def __init__(self, *, dim=EMBED_DIM, collection="knowledge_chunks", client=None):
        self.dim = dim
        self.collection = collection
        self.client = client or QdrantClient(location=":memory:")

    def recreate(self) -> None:
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )

    def upsert(self, chunks, vectors) -> int:
        if len(chunks) != len(vectors):
            raise ValueError(f"块数({len(chunks)})与向量数({len(vectors)})不一致")
        points = [
            PointStruct(
                id=point_id(chunk.chunk_id),
                vector=list(vector),
                payload={
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "order": chunk.order,
                    "heading_path": list(chunk.heading_path),
                    "text": chunk.text,
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        self.client.upsert(collection_name=self.collection, points=points)
        return len(points)

    def search(self, vector, *, top_k=5, doc_ids=None, min_score=None) -> list[Hit]:
        query_filter = None
        if doc_ids:
            query_filter = Filter(
                must=[FieldCondition(key="doc_id", match=MatchAny(any=list(doc_ids)))]
            )
        response = self.client.query_points(
            collection_name=self.collection,
            query=list(vector),
            limit=max(top_k, 1) * 2,
            query_filter=query_filter,
            with_payload=True,
        )
        hits: list[Hit] = []
        for point in response.points:
            payload = point.payload or {}
            score = float(point.score)
            if min_score is not None and score < min_score:
                continue
            hits.append(Hit(...))
            if len(hits) >= top_k:
                break
        return hits
```

### 4.4 rerank

```python
def rerank(query: str, hits: list[Hit], *, top_k: int = 3) -> list[Hit]:
    terms = query_terms(query)
    if not terms:
        return hits[:top_k]

    rescored: list[Hit] = []
    for hit in hits:
        text = hit.text.lower()
        heading = " > ".join(hit.heading_path).lower()
        text_coverage = sum(1 for term in terms if term in text) / len(terms)
        heading_coverage = sum(1 for term in terms if term in heading) / len(terms)
        score = 0.6 * text_coverage + 0.4 * heading_coverage
        rescored.append(hit.with_rerank(round(score, 4)))

    # 主键 rerank 分，次键保留向量分（同分时不让结果随机）
    rescored.sort(key=lambda hit: (hit.rerank_score or 0.0, hit.score), reverse=True)
    return rescored[:top_k]
```

完整实现（含 `index_chunks` / `retrieve` 便捷函数）见 [src/servicedesk/rag/store.py](../../servicedesk-agent/src/servicedesk/rag/store.py)。

### 4.5 运行

```powershell
uv run python -m scripts.rag_demo            # 本地哈希向量
uv run python -m scripts.rag_demo --remote   # 走 mock 的 /embeddings
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么用 `Protocol` 而不是继承基类

```python
class Embedder(Protocol):
    dim: int
    async def embed(self, texts): ...
```

`Protocol` 是**结构化类型**：只要你有 `dim` 属性和 `embed` 方法，你就是 `Embedder`，不用继承任何东西。

好处是 `HashEmbedder` 和 `RemoteEmbedder` 之间**没有耦合**，第三方实现（比如你自己包的 bge-m3）也能直接插进来。**接口正确的标志，是别人不用改你的代码就能替换实现。**

### 5.2 批量 + 并发：两个必须一起做

只批量不并发：1000 块 / 16 = 63 个批次，串行发 = 63 × 100ms ≈ 6.3 秒。
只并发不批量：1000 个请求同时打出去 → 直接 429。

**两个都要**：批量把请求数从 1000 降到 63，并发把 63 个批次压到 4 路并行 ≈ 1.6 秒。

`asyncio.gather` 保证结果顺序与输入一致（即使完成时间不同），这一点很关键——**向量和块错位，索引就全乱了**。测试 `test_remote_embedder_preserves_order_across_batches` 专门钉这个行为。

### 5.3 Qdrant 的 point id 必须是 UUID 或整数

我们的 `chunk_id` 是 `vpn-guide#007`，Qdrant 不接受。用 `uuid5` 做**确定性映射**：

```python
uuid.uuid5(_POINT_NAMESPACE, chunk_id)
```

同一个 `chunk_id` 永远得到同一个 UUID。**重建索引时不会产生重复点**——如果你用随机 uuid4，每次重建都会留下旧点，检索结果里会出现重复的块。

### 5.4 payload 存什么

存了 5 个字段：`chunk_id / doc_id / order / heading_path / text`。

**为什么不存向量之外的更多信息**（比如 token 数、来源 URL）？因为 payload 会跟着每条检索结果返回到内存里，**它的大小直接影响检索延迟**。经验法则：**只存"拿到结果后立刻要用到"的字段**，其余去数据库按 `chunk_id` 反查。

### 5.5 阈值是产品决策，不是技术参数

实测：

```
相关查询「VPN 提示认证失败怎么办」最高分 0.414
无关查询「食堂几点开饭」      最高分 0.227
```

取 0.30 能把两者分开。但这个数字**依赖你的 embedding 模型和语料**——换模型后分数分布会整体漂移，阈值必须重新标定。

**这也是为什么阈值不能写成常量**：

```python
# ❌ 换模型后这里就废了
if score < 0.75: skip

# ✅ 可配置，并记录标定时的模型版本
min_score = settings.rag_min_score
```

### 5.6 rerank 为什么用"覆盖率"而不是另一个向量模型

真实的 rerank 是 **cross-encoder**：把 `(query, doc)` 拼成一个输入，过一遍模型，输出一个相关度分数。它比双塔向量准，但**每个候选都要跑一次模型**，10 个候选就是 10 次推理。

本节用的是**可解释的替代**：词项覆盖率 + 标题命中加权。它不需要模型、毫秒级、结果可解释，而且足以演示"rerank 能改变顺序"这件事。

**换真实模型时，只需要替换 `rerank()` 这一个函数**，接口（`list[Hit] → list[Hit]`）不变。

### 5.7 为什么 `index_chunks` 用 `embedding_text` 而不是 `text`

```python
vectors = await embedder.embed([chunk.embedding_text for chunk in batch])
```

`embedding_text` = 标题路径 + 正文。这是 1.6 的设计在这里兑现：

- 块只有 143 字，可能压根不含"VPN"这个词
- 但它的 `heading_path` 是 `["VPN 使用与排障手册", "4. 常见故障", "4.1 提示「认证失败」"]`

**把路径拼进去，等于给每个块补回了主语。**

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：把 `crc32` 换成内置 `hash()`

```python
vector[hash(gram) % dim] += 1.0     # ← 看起来更简洁
```

跑三个独立进程，打印同一段文本的 `hash()`：

```
进程1 hash: -3225766582479108023
进程2 hash: 3106764028059610809
进程3 hash: -1867041963702184015
```

**三次都不一样。** 而 `crc32` 永远是 `3793350002`。

后果链：

1. 你建好索引，向量是进程 A 算的
2. 服务重启，进程 B 用不同的盐重算查询向量
3. **查询向量和索引向量在两个不同的空间里** → 检索结果全部乱掉
4. 而且**没有报错**，只是答案变差

如果索引是持久化的，这个 bug 会在"重启后的第一次查询"爆发，而在你的开发机上（一次跑完建库 + 查询）**永远复现不出来**。

> 教训：**凡是要落盘或跨进程复用的哈希，都必须用稳定算法**（crc32 / sha256 / md5），不能用语言内置的随机哈希。

### 6.2 实验 B：去掉阈值

把 `min_score` 传 `None`（默认行为），问一个完全无关的问题：

```
查询「食堂几点开饭」top5：
  1. vec=0.227  email-faq#006  § 4. 垃圾邮件误判
  2. vec=0.213  email-faq#001  § 1. 邮箱容量与附件限制
  3. vec=0.189  email-faq#005  § 4. 垃圾邮件误判
  4. vec=0.179  email-faq#008  § 6. 邮件签名规范
  5. vec=0.161  vpn-guide#010  § 4.3 连接成功但打不开内网系统
```

**它总能返回 5 条。** 因为"最近邻"这个操作永远有结果——哪怕最近的邻居在 100 公里外。

这 5 条会被拼进 prompt，模型看到的是"参考资料：邮箱容量 50GB、垃圾邮件规则、签名规范…"，然后被迫基于这些回答"食堂几点开饭"。

**要么胡编，要么答非所问。** 而你的监控上一切正常：没有异常、没有空结果、延迟正常。

加阈值之后：

```
取阈值 0.30 再查一次 → （空）
```

上层拿到空列表，就能走"知识库没有这个信息"的分支——**这是一个功能，不是一个错误。**

---

## 7. 验证与预期输出

### 7.1 建库与检索

```powershell
uv run python -m scripts.rag_demo
```

实测（节选）：

```
第 1 步：建库（embedder = HashEmbedder（本地），dim=256）
  切分得到 26 块，写入 26 点，库内 count=26
  平均每块 143 字

第 2 步：检索「VPN 提示认证失败怎么办」
    1. vec=0.414  vpn-guide#007    § 4.1 提示「认证失败」
       如果客户端提示认证失败，通常有三类原因： - 密码已过期（公司域账号每 90 天…
    2. vec=0.335  vpn-guide#000    § VPN 使用与排障手册
       本文档面向全体员工，介绍公司 VPN 的安装、连接与常见故障处理。
    3. vec=0.257  vpn-guide#012    § 5. 联系支持

  最高分 = 0.414

第 3 步：阈值——不相关的问题应该「检索为空」
    （无关问题 top5 全是噪声，最高 0.227）
  相关查询最高分 0.414  vs  无关查询最高分 0.227

  取阈值 0.30 再查一次无关问题 →
    （空）
```

### 7.2 rerank 的两个案例

**案例 1：把"泛化块"挤出 top3**

```
  【案例】VPN 提示认证失败怎么办
    rerank 前 top3：
    1. vec=0.414  vpn-guide#007    § 4.1 提示「认证失败」
    2. vec=0.335  vpn-guide#000    § VPN 使用与排障手册      ← 泛化块
    3. vec=0.257  vpn-guide#012    § 5. 联系支持
    rerank 后 top3：
    1. vec=0.414  rerank=0.383  vpn-guide#007  § 4.1 提示「认证失败」
    2. vec=0.242  rerank=0.100  vpn-guide#006  § 3. 连接步骤
    3. vec=0.239  rerank=0.083  vpn-guide#009  § 4.2 提示「无法建立隧道」
    top1：vpn-guide#007 → vpn-guide#007  （未变）
    进入：['vpn-guide#006', 'vpn-guide#009']    挤出：['vpn-guide#000', 'vpn-guide#012']
```

**案例 2：把错的 top1 换对**

```
  【案例】邮件附件上限
    rerank 前 top3：
    1. vec=0.387  email-faq#002    § 2.1 网页版无法登录        ← 错的
    2. vec=0.277  vpn-guide#010    § 4.3 连接成功但打不开内网系统
    3. vec=0.274  email-faq#001    § 1. 邮箱容量与附件限制      ← 对的
    rerank 后 top3：
    1. vec=0.274  rerank=0.667  email-faq#001  § 1. 邮箱容量与附件限制
    2. vec=0.183  rerank=0.167  email-faq#011  § 9. 邮件归档与检索
    3. vec=0.387  rerank=0.000  email-faq#002  § 2.1 网页版无法登录
    top1：email-faq#002 → email-faq#001  ✅ 换对了
    进入：['email-faq#011']    挤出：['vpn-guide#010']
    top3 是否含实际答案（25 MB）：是
```

**注意第 3 名的 `rerank=0.000`**：那个"错误但向量分最高"的块，一个查询词项都没覆盖到，被直接踢到末位。这就是 rerank 的价值——**它看的是"能不能回答问题"，不是"字面有多像"**。

### 7.3 远程 embedding 链路

```powershell
uv run python -m scripts.rag_demo --remote
```

结果与本地完全一致（mock 用同一套哈希算法），说明**批量 + 并发 + 顺序保持**这条链路是对的。

### 7.4 测试

```powershell
uv run pytest
```

```
................................................................         [100%]
64 passed in 8.68s
```

（1.1–1.6 的 49 个 + 本节的 15 个。）

---

## 8. 生产环境踩坑实录

### 坑 1：换了 embedding 模型，忘了重建索引

**现象**

把 `text-embedding-3-small` 换成 `bge-m3`，检索结果**毫无变化**，或者干脆全部乱掉。

**定位方法**

```python
info = store.client.get_collection(store.collection)
print("集合维度:", info.config.params.vectors.size)
print("当前 embedder dim:", embedder.dim)
```

两个数字不一致 → 就是这个坑。

**根因**

两种可能，都很致命：

1. **维度不同**（1536 vs 1024）：写入直接报错，或者被静默截断。
2. **维度相同但语义空间不同**：不报错，但**两个向量空间不可比**——用新模型算的查询向量，去和旧模型建的索引比对，等于拿中文词典查英文句子。

**修法**

```diff
- store.ensure()
+ # 换模型 = 换索引空间，必须重建
+ store.recreate()
+ await index_chunks(store, chunks, embedder)
```

**顺手加固**：把模型名和维度写进集合的元信息，启动时校验：

```python
expected = f"{embedder.model}:{embedder.dim}"
if store.meta.get("embedding") != expected:
    raise FatalError(f"索引是用 {store.meta.get('embedding')} 建的，"
                     f"当前 embedder 是 {expected}，请重建索引")
```

本节代码里 `RemoteEmbedder` 已经做了维度校验（不一致直接 `FatalError`），但**"维度相同、空间不同"它发现不了**——那需要存模型名。

### 坑 2：检索为空被当成异常，上层 500

**现象**

用户问知识库没有的问题，接口返回 500。日志里是一条 `IndexError: list index out of range`——因为代码里写了 `hits[0].text`。

**定位方法**

```python
hits = await retrieve(...)
print(f"命中 {len(hits)} 条")     # 先看是不是 0
```

**根因**

把"检索为空"当成了"检索失败"。这两个是完全不同的事：

| 情况 | 含义 | 该怎么做 |
|---|---|---|
| 检索为空 | 知识库确实没有 | 走"我不知道"分支，或转人工 |
| 检索报错 | 服务/网络/权限问题 | 重试 / 告警 / 降级 |

**修法**

```diff
- answer = hits[0].text
+ if not hits:
+     return "知识库里没有找到相关信息，建议提交工单或转人工。"
+ answer = hits[0].text
```

**顺手加固**：在评估体系（5.2）里把"该为空时为空"作为一个指标——**空得对，也是正确**。

### 坑 3：以为 rerank 能救回"没召回到"的块

**现象**

上线 rerank 后，某些问题的准确率纹丝不动。查日志发现：正确答案压根不在候选集里。

**定位方法**

```python
print("候选 chunk_ids:", [hit.chunk_id for hit in hits])
# 检查正确答案的 chunk_id 是否在里面
```

**根因**

rerank 的输入是**已经召回的候选**，它只能重排。召回没拿到 → rerank 无从下手。

**修法**

先提高召回，再谈重排：

```diff
- hits = await retrieve(store, embedder, q, top_k=3)
+ hits = await retrieve(store, embedder, q, top_k=10)   # 先多召回
  ranked = rerank(q, hits, top_k=3)                     # 再重排
```

**顺手加固**：分别监控 **召回率**（正确答案在不在 top10 里）和 **top1 命中率**。前者靠切分和 `top_k`，后者靠 rerank。两个指标一起看，才能判断该往哪改。

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：为什么不能用 Python 内置的 <code>hash()</code> 做向量哈希？</b></summary>

因为 `hash()` 每个进程会加随机盐。实测三个进程对同一段文本得到三个不同值（`-3225766582479108023` / `3106764028059610809` / `-1867041963702184015`）。索引里的向量是进程 A 算的，查询向量是进程 B 算的，两者不在同一空间，检索结果全乱且不报错。要用 `crc32` / `sha256` 这类稳定哈希。

</details>

<details>
<summary><b>Q2：阈值应该怎么定？</b></summary>

用数据定，不用感觉。做法：拿一批"该有答案"和"该没答案"的问题，分别看最高分的分布，取两者之间的分界。本节的 0.30 来自实测（相关 0.414 vs 无关 0.227）。**换 embedding 模型后必须重新标定**，所以阈值要可配置，并记录标定时用的模型版本。

</details>

<details>
<summary><b>Q3：rerank 能提升召回率吗？</b></summary>

不能。rerank 只对已召回的候选重新排序，**召回没拿到的块它永远看不到**。所以工程顺序是：先保召回（切分别太碎、`top_k` 取大），再用 rerank 提精确率。

</details>

### 9.2 动手任务：加 MMR 去重

**要求**

实现 `mmr_select(query_vector, hits, k=3, lambda_=0.7)`：

1. 从候选里迭代选择：每次选 `lambda_ * 相关性 - (1 - lambda_) * 与已选块的最大相似度` 最大的那个。
2. 相似度用向量余弦（需要 hit 带上向量，或重新 embed）。
3. 用它替换 `rerank`，看 top3 是否变得更"多样"。

**验收标准**

- 查询"VPN 认证失败"，不加 MMR 时 top3 里出现 2 个以上来自同一小节的块；加 MMR 后同小节最多 1 个。
- 补 2 个用例：`lambda_=1.0` 时结果与纯相关性排序一致；`lambda_=0.0` 时结果尽量互不相似。
- 说明 MMR 的代价：**相关性会略微下降**，换来覆盖面。

**提示**：MMR 的经典用途是"别让 3 条结果说的是同一件事"。在 RAG 里它能显著减少 prompt 里的重复信息。

### 9.3 拓展挑战

给 `QdrantStore` 加**多租户过滤**：写入时 payload 带 `tenant_id`，检索时强制带上——**且必须写在 `search()` 内部，不能交给调用方**。

**思考**：

1. 如果让调用方传 `tenant_id`，会有什么安全事故？（提示：某个调用方忘了传，检索到什么？）
2. Qdrant 的 payload 索引（`create_payload_index`）在数据量大时为什么必须建？不建会怎样？

---

## 10. 本节小结与延伸

**三点回顾**

1. **"最近邻"永远有结果，所以阈值不可省**：无关问题的最高分是 0.227，不加阈值就会把 5 条噪声当证据拼进 prompt。**检索为空是功能，不是故障。**
2. **rerank 只在"答案已在候选里"时有用**：实测把 `email-faq#002`（错，vec=0.387）换成了 `email-faq#001`（对，rerank=0.667），并把错的那条打到 `rerank=0.000`。但它救不了没召回的块。
3. **向量必须用稳定哈希**：内置 `hash()` 三个进程三个值（实测），落盘索引后必然失效且不报错。

**已知限制（诚实记录）**

- **只验证了本地哈希向量**：真实 embedding（神经网络）的分数分布、阈值、rerank 效果都会不同，需要重新标定。
- **rerank 是"覆盖率"近似，不是 cross-encoder**：可解释但精度有限。
- **未做 MMR 去重**：top3 可能来自同一小节，信息重复。
- **未做多租户隔离**：`doc_ids` 是手动传的，生产里必须强制。

**延伸阅读**

- Qdrant 客户端与本地模式：https://qdrant.tech/documentation/quickstart/
- Qdrant 过滤（payload index）：https://qdrant.tech/documentation/concepts/filtering/
- 余弦相似度与归一化：https://en.wikipedia.org/wiki/Cosine_similarity
- MMR（最大边际相关性）：https://en.wikipedia.org/wiki/Maximal_marginal_relevance

---

## 修订记录

- 2026-11-17 初稿：建库 / 检索 / 过滤 / 阈值 / rerank 五步全部实机运行，§7 为真实输出
- 2026-11-17 补充：mock 服务新增 `/v1/embeddings`（确定性哈希向量），`--remote` 路径与本地结果一致，验证批量 + 并发 + 顺序保持
- 2026-11-17 修正：demo 的过滤查询从「附件最大能发多大」（向量 top1 错）改成「垃圾邮件误判」，避免"过滤器背锅"；rerank 演示改为两个案例，其中一个是 top1 换对的强证据
