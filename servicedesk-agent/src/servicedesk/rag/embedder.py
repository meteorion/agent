# file: src/servicedesk/rag/embedder.py
"""把文本变成向量。两种实现，同一个接口。

    HashEmbedder    —— 本地确定性哈希向量，零依赖、零成本，用来跑通链路
    RemoteEmbedder  —— 调 OpenAI 兼容的 /embeddings 接口，批量 + 并发 + 限流

换实现只需要改一行构造，下游（建库、检索、评估）完全不用动。
"""

from __future__ import annotations

import asyncio
import math
import zlib
from typing import Protocol

import httpx

from servicedesk.config import Settings, get_settings
from servicedesk.errors import FatalError, NeedsHumanError, RetryableError
from servicedesk.logging_setup import get_logger

log = get_logger("rag.embedder")

EMBED_DIM = 256


class Embedder(Protocol):
    """向量化接口。dim 必须固定，否则建库时会冲突。"""

    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...


# ------------------------------ 本地哈希实现 ------------------------------


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


class HashEmbedder:
    """离线可跑的实现。真实项目里它只用来做冒烟测试。"""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hash_vector(text, self.dim) for text in texts]


# ------------------------------ 远程接口实现 ------------------------------


class RemoteEmbedder:
    """调 /embeddings。重点是**批量**与**并发上限**这两件事。

    为什么不一条一条发：一次 HTTP 往返 50–200ms，逐条发 100 个块就是 5–20 秒。
    批量发 16 条，同样的数据量只需要 1 次往返。
    为什么要有并发上限：批次数一多，同时打出去的请求会把对方限流，
    反而更慢（还会触发 429）。
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        model: str = "text-embedding-3-small",
        dim: int = EMBED_DIM,
        batch_size: int = 16,
        concurrency: int = 4,
        timeout_s: float = 30.0,
    ) -> None:
        self._settings = settings or get_settings()
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.concurrency = concurrency
        self._timeout_s = timeout_s

    def _batches(self, texts: list[str]) -> list[list[str]]:
        return [
            texts[start : start + self.batch_size]
            for start in range(0, len(texts), self.batch_size)
        ]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        semaphore = asyncio.Semaphore(self.concurrency)
        batches = self._batches(texts)
        results: list[list[float]] = []

        async with httpx.AsyncClient(
            base_url=f"{self._settings.llm_base_url.rstrip('/')}/",
            timeout=httpx.Timeout(connect=5.0, read=self._timeout_s, write=10.0, pool=5.0),
        ) as client:
            async def run(batch: list[str]) -> list[list[float]]:
                async with semaphore:
                    return await self._embed_batch(client, batch)

            # gather 保持顺序，即使并发完成的时间不同
            for chunk in await asyncio.gather(*(run(batch) for batch in batches)):
                results.extend(chunk)

        log.info(
            "embed_done",
            texts=len(texts),
            batches=len(batches),
            concurrency=self.concurrency,
            model=self.model,
        )
        return results

    async def _embed_batch(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        key = self._settings.llm_api_key
        if key is not None:
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"

        payload = {"model": self.model, "input": batch, "dimensions": self.dim}
        response = await client.post("embeddings", json=payload, headers=headers)

        if response.status_code >= 400:
            detail = response.text[:200]
            if response.status_code in {401, 403}:
                raise NeedsHumanError(
                    f"embedding 鉴权失败: HTTP {response.status_code}",
                    hint="检查 LLM_API_KEY 是否有效、是否有 embedding 模型权限",
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableError(f"embedding 暂时失败: HTTP {response.status_code}")
            raise FatalError(f"embedding 请求非法: HTTP {response.status_code}: {detail}")

        data = response.json()
        vectors = [item["embedding"] for item in data["data"]]
        for vector in vectors:
            if len(vector) != self.dim:
                raise FatalError(
                    f"向量维度不一致：期望 {self.dim}，实际 {len(vector)}。"
                    "换 embedding 模型必须重建索引。"
                )
        return vectors
