# file: scripts/mock_llm_server.py
"""一个"假的 OpenAI 兼容服务"，只为 1.3 服务。

为什么需要它：1.3 要亲手解析 HTTP 与 SSE，但读者可能没有 API Key。
这个 mock 实现了与真实服务一致的三个关键行为：

1. POST /v1/chat/completions，返回 choices[].message.content + usage
2. stream=true 时返回 text/event-stream，逐块下发 delta，最后 data: [DONE]
3. 缺鉴权返回 401，messages 顺序违规返回 400 —— 让错误路径也能练

它不是玩具：客户端代码对着它能跑通，把 LLM_BASE_URL 换成真实地址即可无缝切换。

单独启动：
    uv run python scripts/mock_llm_server.py --port 8765
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

REPLY_TEXT = "异步不是让计算更快，而是让等待重叠。"
VALID_ROLES = {"system", "user", "assistant", "tool"}


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """极粗糙的 token 估算：JSON 字符数 / 3。

    真实 token 数必须用 tokenizer（tiktoken 之类）算，中英文差异很大。
    这里只是让 demo 有个稳定可算的数字。
    """
    return max(1, len(json.dumps(messages, ensure_ascii=False)) // 3)


def validate(payload: dict[str, Any]) -> str | None:
    """模拟真实服务的请求校验。返回错误描述，None 表示通过。"""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages 必须是非空数组"

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return f"messages[{index}] 必须是对象"
        role = message.get("role")
        if role not in VALID_ROLES:
            return f"messages[{index}].role 非法：{role!r}，允许 {sorted(VALID_ROLES)}"
        if role == "tool":
            # 并行调用时会出现连续多条 tool 消息，
            # 所以要往前找到**最近一条非 tool** 的消息来判断
            previous = {}
            for earlier in reversed(messages[:index]):
                if earlier.get("role") != "tool":
                    previous = earlier
                    break
            if not previous.get("tool_calls"):
                return (
                    f"messages[{index}] 是 tool 消息，但它前面没有"
                    f"带 tool_calls 的 assistant 消息（顺序错了）"
                )
    return None


def _delay_seconds(payload: dict[str, Any]) -> float:
    """两种制造延迟的方式：显式参数，或用户消息里带 #slow 标记。"""
    delay = float(payload.get("mock_delay_s") or 0)
    if delay:
        return delay
    for message in reversed(payload.get("messages", [])):
        if message.get("role") == "user" and "#slow" in (message.get("content") or ""):
            return 1.0
    return 0.0


def _usage(payload: dict[str, Any]) -> dict[str, int]:
    prompt_tokens = estimate_prompt_tokens(payload["messages"])
    completion_tokens = max(1, len(REPLY_TEXT) // 2)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


# --------------------------- 假 embedding（1.7 用） ---------------------------
#
# 用「字符二元组 + CRC32 哈希」做一个确定性向量：同样的文本永远得到同样的向量，
# 共享字组越多的文本，余弦相似度越高。它不是神经网络，但足以验证
# 「批量 / 并发 / 建库 / 过滤 / 阈值 / rerank」这整条工程链路。

EMBED_DIM = 256


def embed_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    """确定性哈希向量：字符二元组落到 dim 个桶里，再做 L2 归一化。"""
    vector = [0.0] * dim
    for index in range(len(text) - 1):
        gram = text[index : index + 2]
        bucket = zlib.crc32(gram.encode("utf-8")) % dim
        vector[bucket] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def build_embeddings(payload: dict[str, Any]) -> dict[str, Any]:
    raw_input = payload.get("input", [])
    texts = [raw_input] if isinstance(raw_input, str) else list(raw_input)
    dim = int(payload.get("dimensions") or EMBED_DIM)
    return {
        "object": "list",
        "model": payload.get("model", "mock-embed"),
        "data": [
            {"object": "embedding", "index": index, "embedding": embed_text(text, dim)}
            for index, text in enumerate(texts)
        ],
        "usage": {"prompt_tokens": sum(len(text) for text in texts), "total_tokens": 0},
    }


# ----------------------------- 故障注入（1.5 用） -----------------------------
#
# 测试重试逻辑时，我们需要"让服务端按剧本出错"：
#   queue_faults("429", "429", "ok")  → 前两次 429，第三次正常
# 支持的故障码：ok / 429 / 500 / 502 / timeout / badjson

_FAULT_LOCK = threading.Lock()
_FAULTS: list[str] = []
_STATE: dict[str, int] = {"requests": 0}


def queue_faults(*codes: str) -> None:
    """排入故障剧本，按请求顺序消费。"""
    with _FAULT_LOCK:
        _FAULTS.extend(codes)


def clear_faults() -> None:
    with _FAULT_LOCK:
        _FAULTS.clear()
        _STATE["requests"] = 0


def request_count() -> int:
    with _FAULT_LOCK:
        return _STATE["requests"]


def _count_request() -> None:
    with _FAULT_LOCK:
        _STATE["requests"] += 1


def _take_fault() -> str:
    with _FAULT_LOCK:
        return _FAULTS.pop(0) if _FAULTS else "ok"


def build_completion(payload: dict[str, Any]) -> dict[str, Any]:
    if _is_rag_prompt(payload):
        content, tool_calls = _rag_answer(payload), []
    elif _has_tools(payload):
        content, tool_calls = _tool_turn(payload)
    else:
        content, tool_calls = REPLY_TEXT, []

    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model", "mock-model"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": _usage(payload),
    }


# --------------------------- 假工具调用模型（2.1 用） ---------------------------
#
# 只要请求里带了 tools，mock 就按关键词决定调哪几个工具；
# 当最后一条消息是 tool（说明工具已执行完），就总结结果给出最终回答。

_ASSET_WORDS = ("资产", "电脑", "设备", "主机", "笔记本")
_PERM_WORDS = ("权限", "授权", "能不能访问")
_TICKET_WORDS = ("工单状态", "查工单", "工单进度")
_KNOWLEDGE_WORDS = ("怎么办", "怎么解决", "为什么", "失败")


def _has_tools(payload: dict[str, Any]) -> bool:
    return bool(payload.get("tools"))


def _tool_turn(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    messages = payload.get("messages", [])
    last = messages[-1] if messages else {}

    if last.get("role") == "tool":
        return _summarize_tool_results(messages), []

    question = " ".join(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "user"
    )
    available = [item["function"]["name"] for item in payload.get("tools") or []]
    planned = _planned_calls(question, available)
    if not planned:
        # 判断"这问题不需要工具"也是一种能力：此时直接回答，不要硬调工具
        return (
            "我可以帮你查设备资产、员工权限和工单状态。"
            "请告诉我具体的主机名（如 NB-1024）、员工编号（如 E-2048）或工单号（如 T-1001）。",
            [],
        )
    return "", planned


def _resolve_name(suffix: str, available: list[str]) -> str | None:
    """从客户端真正给的工具名里挑一个（可能带 mcp__server__ 命名空间）。"""
    for name in available:
        if name == suffix or name.endswith("__" + suffix):
            return name
    return None


def _planned_calls(question: str, available: list[str] | None = None) -> list[dict[str, Any]]:
    available = available or ["query_asset", "query_permission", "query_ticket", "search_knowledge"]
    planned: list[tuple[str, dict[str, Any]]] = []

    asset = _resolve_name("query_asset", available)
    if asset and (any(word in question for word in _ASSET_WORDS) or re.search(r"NB-\d+", question)):
        match = re.search(r"NB-\d+", question)
        planned.append((asset, {"hostname": match.group(0) if match else "NB-1024"}))

    perm = _resolve_name("query_permission", available)
    if perm and any(word in question for word in _PERM_WORDS):
        match = re.search(r"E-\d+", question)
        planned.append((perm, {"employee_id": match.group(0) if match else "E-2048"}))

    knowledge = _resolve_name("search_knowledge", available)
    if knowledge and any(word in question for word in _KNOWLEDGE_WORDS):
        planned.append((knowledge, {"query": question[:80], "top_k": 2}))

    ticket = _resolve_name("query_ticket", available)
    if ticket and any(word in question for word in _TICKET_WORDS):
        match = re.search(r"T-\d+", question)
        planned.append((ticket, {"ticket_id": match.group(0) if match else "T-1001"}))

    return [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }
        for index, (name, args) in enumerate(planned, start=1)
    ]


def _summarize_tool_results(messages: list[dict[str, Any]]) -> str:
    """把 tool 消息拼成一句人话，模拟"模型读了工具结果再回答"。"""
    parts: list[str] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = str(message.get("content") or "")
        parts.append(content[:120])
    if not parts:
        return "没有拿到工具结果。"
    return "根据工具返回：" + "；".join(parts)


# ----------------------------- 假 RAG 模型（1.8 用） -----------------------------
#
# mock 通过识别我们真实的 prompt 格式（含【资料】/【问题】标记）来决定行为，
# 不需要客户端传任何额外参数——生产 prompt 里不会因此多出脏东西。
#
# 三种行为：
#   1. 资料为空      → 老实回答"查不到"
#   2. 正常          → 摘取与问题最相关的那段资料，附上 [C几]
#   3. 含 #hallucinate → 故意编一个不存在的引用 [C9]，用来测校验层

_CONTEXT_BLOCK = re.compile(r"\[C(\d+)\]\s*(.*?)(?=\n\[C\d+\]|\n【问题】|\Z)", re.S)


def _is_rag_prompt(payload: dict[str, Any]) -> bool:
    return any(
        "【资料】" in (message.get("content") or "")
        for message in payload.get("messages", [])
    )


def _last_user_text(payload: dict[str, Any]) -> str:
    for message in reversed(payload.get("messages", [])):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _parse_context(text: str) -> list[tuple[int, str]]:
    """从 prompt 里把 [C1] 编号和正文抠出来。"""
    blocks: list[tuple[int, str]] = []
    for match in _CONTEXT_BLOCK.finditer(text):
        index = int(match.group(1))
        body = match.group(2).strip()
        # 去掉紧跟标题行（[C1] doc=… § …）之后的第一行说明
        if "\n" in body:
            body = body.split("\n", 1)[1].strip()
        if body:
            blocks.append((index, body))
    return blocks


def _grams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _rag_answer(payload: dict[str, Any]) -> str:
    user_text = _last_user_text(payload)
    question = user_text.split("【问题】", 1)[-1].strip()
    blocks = _parse_context(user_text)

    if "#hallucinate" in question:
        # 故意违规：引用一个不存在的编号，且编造内容
        return json.dumps(
            {
                "answer": "根据资料，VPN 认证失败时请直接重启路由器即可恢复[C9]。",
                "citations": [{"index": 9, "quote": "重启路由器"}],
            },
            ensure_ascii=False,
        )

    if not blocks or "（无相关片段）" in user_text:
        return json.dumps(
            {"answer": "知识库里没有找到与这个问题相关的资料。", "citations": []},
            ensure_ascii=False,
        )

    question_grams = _grams(question)
    best_index, best_body = max(
        blocks,
        key=lambda item: len(question_grams & _grams(item[1])),
    )
    first_sentence = re.split(r"(?<=[。！？])", best_body.strip())[0].strip()
    quote = first_sentence if len(first_sentence) <= 120 else first_sentence[:120] + "…"
    return json.dumps(
        {
            "answer": f"根据资料：{first_sentence}[C{best_index}]",
            "citations": [{"index": best_index, "quote": quote}],
        },
        ensure_ascii=False,
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "MockLLM/0.1"

    def log_message(self, *args: Any) -> None:  # 静音，别把测试输出刷满
        pass

    def _drain(self) -> None:
        """读掉请求体。否则客户端还在发 body 我们就关连接，会触发 RST。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def do_POST(self) -> None:
        # 先计数：连 401 也算一次"服务端收到了请求"，否则统计口径会骗人
        _count_request()

        path = self.path.rstrip("/")
        if path not in ("/v1/chat/completions", "/v1/embeddings"):
            self._drain()
            self._send_json(404, {"error": {"message": f"未知路径 {self.path}"}})
            return

        auth = (self.headers.get("Authorization") or "").strip()
        if not auth.startswith("Bearer ") or len(auth) <= len("Bearer "):
            self._send_json(
                401,
                {
                    "error": {
                        "message": "缺少 Authorization: Bearer <key>",
                        "type": "invalid_request_error",
                    }
                },
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"

        fault = _take_fault()
        if fault == "429":
            self._send_json(
                429,
                {"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}},
                extra_headers={"Retry-After": "1"},
            )
            return
        if fault in ("500", "502"):
            self._send_json(
                int(fault),
                {"error": {"message": f"upstream error {fault}", "type": "server_error"}},
            )
            return
        if fault == "timeout":
            time.sleep(0.5)          # 客户端 read timeout 设小就会抛
        if fault == "badjson":
            self._send_raw(200, b"<html>502 Bad Gateway</html>", "text/html")
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": {"message": f"JSON 解析失败: {exc}"}})
            return

        # 向量化请求不走 chat 的校验（它没有 messages）
        if path == "/v1/embeddings":
            self._send_json(200, build_embeddings(payload))
            return

        problem = validate(payload)
        if problem:
            self._send_json(
                400, {"error": {"message": problem, "type": "invalid_request_error"}}
            )
            return

        delay = _delay_seconds(payload)
        if delay:
            time.sleep(delay)

        if payload.get("stream"):
            self._stream(payload)
        else:
            self._send_json(200, build_completion(payload))

    # ------------------------------ 内部方法 ------------------------------

    def _send_json(
        self,
        status: int,
        body: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except ConnectionError:
            return   # 客户端已经走了（比如它自己的超时到了）

    def _send_raw(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            return

    def _write_event(self, event: dict[str, Any]) -> None:
        line = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except ConnectionError:
            raise  # 交给 _stream 统一处理（含 Reset / Aborted / BrokenPipe）

    def _stream(self, payload: dict[str, Any]) -> None:
        model = payload.get("model", "mock-model")
        include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            # 逐块下发 3 个字符，模拟真实模型"边生成边吐"
            for start in range(0, len(REPLY_TEXT), 3):
                piece = REPLY_TEXT[start : start + 3]
                self._write_event(
                    {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {"index": 0, "delta": {"content": piece}, "finish_reason": None}
                        ],
                    }
                )
                time.sleep(0.02)

            final: dict[str, Any] = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            # 关键细节：只有显式要求 include_usage，usage 才会出现。
            # 真实 OpenAI 兼容服务也是这样，所以很多流式调用"拿不到 token 数"。
            if include_usage:
                final["usage"] = _usage(payload)
            self._write_event(final)

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except ConnectionError:
            # 客户端中途断开（比如它自己的超时到了、或只读了一半就 break）——
            # 真实服务也会遇到，安静收尾，不要往 stderr 甩堆栈。
            return


def start_mock_server(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """在后台线程里起服务，返回 (server, base_url)。port=0 表示随机端口。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, real_port = server.server_address[0], server.server_address[1]
    return server, f"http://{host}:{real_port}/v1"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="假的 LLM 服务")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server, base_url = start_mock_server(args.port)
    print(f"mock LLM 已启动: {base_url}")
    print("按 Ctrl+C 结束")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
