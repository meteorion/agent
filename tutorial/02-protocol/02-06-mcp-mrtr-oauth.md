---
section: "2.6"
title: "用 MRTR + OAuth 2.0 处理「缺信息」与「缺权限」"
stage: 2
status: done
depth: L2
difficulty: 4
minutes: 100
milestone: M2
depends_on: ["2.5"]
produces: ["mcp/auth.py", "scripts/mrtr_client.py"]
verifies: "演示「工具说缺审批人 → 补上后重试成功」的完整往返"
part: null
updated: "2026-11-17"
---

# 2.6 用 MRTR + OAuth 2.0 处理「缺信息」与「缺权限」

> ⚠️ OAuth 部分用**不透明 token + 内存存储**演示协议流程。生产里应校验 JWT 的 `iss`
> （RFC 9207）并与授权服务器 JWKS 对齐；Dynamic Client Registration 已被 CIMD 取代（见 2.3 变更表）。
> 本节聚焦"权限模型怎么落到代码里"，不实现完整的 OAuth 授权流程。

## 0. 本节导航

**本节在链路中的位置**：2.5 的 Server 谁都能调。本节加上两件事——**"我还需要什么"**（MRTR）与**"你不该做这个"**（scope）。这是 2.7 把工具接回 Agent 前必须解决的问题：**Agent 遇到这两类失败，处理方式完全不同。**

**本节你将获得**

1. MRTR 的服务端实现：工具缺信息时**不报错**，返回 `resultType: "input_required"`。
2. 客户端的正确重试：**原请求 + `inputResponses`**，同 id、同方法。
3. 一套 scope 模型（read / write / approve），并支持**按参数动态提权**（建 P0 才需要审批权）。
4. 401 与 403 的严格区分，以及为什么**403 不能降级成 401**。
5. 一个真实踩坑：把中文塞进 `WWW-Authenticate` 会直接 500。

**前置检查清单**

- 2.5 的 `uv run python -m scripts.mcp_http_demo` 能跑通。
- 2.3 的 `build_retry_with_inputs` 已理解（本节真的用它）。

**难度**：★★★★☆ ｜ **预计时长**：100 分钟 ｜ **本节费用**：**¥0**（本地回环）

---

## 1. 为什么需要它（痛点先行）

用户要建一条工单，Agent 调 `create_ticket`，缺了"优先级"和"审批人"。系统返回：

```
错误：create_ticket() missing 2 required positional arguments
```

用户看到的是"系统出错了"。他不知道**缺什么**、也不知道**该给什么**。

**更糟的情况**：用户没有建单权限。系统返回：

```
401 Unauthorized
```

客户端于是自动跳转登录页。用户重新登录、再试、还是 401、再登录……**死循环**。

因为 401 的含义是"你身份不明"，而真实原因是"你身份清楚，但你不够格"。**这两件事在协议层的处置方式必须不同。**

本节要做的就是把"做不了"拆成两种：

| | 缺信息 | 缺权限 |
|---|---|---|
| 谁能解决 | 用户自己（回答一个问题） | 管理员（授予 scope） |
| 协议表达 | `resultType: "input_required"` | `403 insufficient_scope` |
| 重试方式 | 原请求 + `inputResponses` | 换 token 后重发 |

---

## 2. 核心概念与架构设计

### 2.1 类比：银行柜台

- **缺材料**（没带身份证复印件）：柜员说"请补一份身份证复印件"，你补了就能办。→ **MRTR**
- **缺授权**（你要取别人账户的钱）：柜员说"你不是本人，也没有授权书"。你补一百份复印件也没用。→ **403**

> **这个类比在哪失效**：银行柜员能"看着你"判断该不该办；而 MCP 的 Server 只能看 token 里的 scope。
> 所以"谁能办什么"必须**显式声明在 `TOOL_SCOPES` 里**，不能靠临场判断。

### 2.2 MRTR 的往返

```
Client                                      Server
  │ ① tools/call {name:create_ticket,
  │              arguments:{title:"..."}}          │
  │ ──────────────────────────────────────────────►│
  │                                                │ 工具发现缺 priority/approver
  │ ② 200 OK                                       │
  │    {resultType:"input_required",               │
  │     inputRequests:[{id:"priority",prompt,schema},│
  │                    {id:"approver",...}]}        │
  │ ◄──────────────────────────────────────────────│
  │                                                │
  │ （客户端去问用户，拿到答案）                     │
  │                                                │
  │ ③ **重试原请求**（同 id、同 method、同参数）      │
  │    + inputResponses:{priority:"P1",approver:"E-2048"}
  │ ──────────────────────────────────────────────►│
  │ ④ 200 OK {resultType:"complete", content:[...]}│
  │ ◄──────────────────────────────────────────────│
```

**关键：第 ③ 步是"重试原请求"，不是"发新请求"。**

这样服务端在收到第 ③ 步时，原始参数与补充答案在**同一个请求**里——它不需要记住第 ① 步问过什么。**这就是 MRTR 能保持无状态的原因。**

### 2.3 三种"做不了"的完整对照

| | 缺信息 | 缺权限 | 工具内部失败 |
|---|---|---|---|
| HTTP 状态 | **200** | **403** | 200 |
| 协议字段 | `resultType: "input_required"` | `error: insufficient_scope` | `result.isError: true` |
| 携带信息 | `inputRequests[]`（含 schema！） | `required` / `granted` scope 列表 | 错误文本 |
| 客户端能做什么 | **自动渲染表单**去问用户 | 换 token / 找管理员 | 让模型读错误并改正 |
| 重试 | 原请求 + `inputResponses` | 新 token 重发 | 重新生成参数 |

**这三行必须严格分开。** 混一行，用户体验就崩一类。

### 2.4 scope 模型

```python
SCOPE_READ    = "tools:read"       # 查资产、查权限、查工单
SCOPE_WRITE   = "tools:write"      # 建单
SCOPE_APPROVE = "admin:approve"    # 建 P0 单（事故级）
```

**静态映射**（工具 → 必需 scope）+ **动态追加**（参数 → 额外 scope）：

```python
def dynamic_scopes(tool: str, arguments: dict | None) -> frozenset[str]:
    if tool == "create_ticket" and (arguments or {}).get("priority") == "P0":
        return frozenset({SCOPE_APPROVE})
    return frozenset()
```

**为什么要动态**：真实业务里权限常常是"在某些参数下才需要更高权限"。
建 P3 工单是日常操作，建 P0（线上事故）必须有人担责。**用一个工具的静态 scope 表达不了这种差别。**

### 2.5 适用范围（边界）

| 场景 | 做法 |
|---|---|
| 缺必填参数 | ✅ MRTR（本节） |
| 需要人确认危险操作 | ✅ MRTR（"确认要删吗？"） |
| 权限不足 | ✅ 403 + `insufficient_scope` |
| 需要用户去另一个系统走审批流 | ⚠️ 不是 MRTR 的事，返回业务错误 + 审批链接 |

---

## 3. 环境与版本

本节**不新增依赖**。

| 组件 | 实测版本 | 用在哪 |
|---|---|---|
| httpx | `0.28.1` | 客户端 |
| pydantic | `2.14.3` | 工具参数（含 `Priority` 枚举） |
| Python | `3.12.13` | `secrets` / `hashlib` |

**验证**

```powershell
uv run python -c "from servicedesk.mcp.auth import TOOL_SCOPES; print(TOOL_SCOPES)"
```

---

## 4. 代码实战（5 个文件）

| 文件 | 角色 |
|---|---|
| `src/servicedesk/mcp/auth.py` | **主角**：token / scope / 401 / 403 |
| `src/servicedesk/tools/registry.py` | 新增 `NeedsInput` 与 `InputRequest` |
| `src/servicedesk/tools/builtin.py` | 新增 `create_ticket`（会抛 `NeedsInput`） |
| `src/servicedesk/mcp/server.py` | `NeedsInput` → `input_required` 的转换 |
| `scripts/mrtr_client.py` | 八步演示 |
| `tests/test_mcp_auth_mrtr.py` | 20 个断言 |

### 4.1 两种错误，两个状态码

```python
class AuthError(Exception):
    """401：没带 token、token 无效或过期。"""
    status = 401
    error_code = "invalid_token"


class InsufficientScope(AuthError):
    """403：token 有效，但权限不够。

    **不要把这条降级成 401**：401 会诱导客户端去"重新登录"，
    而重新登录也拿不到缺少的 scope——它需要提权审批。
    """
    status = 403
    error_code = "insufficient_scope"
```

### 4.2 头必须是 ASCII（本节踩的坑）

```python
    @property
    def www_authenticate(self) -> str:
        """注意：HTTP 头只能是 latin-1/ASCII。

        把中文错误描述塞进 `error_description` 会让 `send_header` 抛
        `UnicodeEncodeError`，直接 500 —— 人话放响应体，头里只放机器可读的码。
        """
        return f'Bearer realm="servicedesk-mcp", error="{self.error_code}"'
```

### 4.3 工具抛 `NeedsInput`，协议层转成 `input_required`

```python
async def _create_ticket(title, priority=None, approver=None) -> dict:
    requests: list[InputRequest] = []
    if priority is None:
        requests.append(InputRequest(id="priority", prompt="这条工单的优先级是？",
                                     schema={"enum": ["P0", "P1", "P2", "P3"]}))
    if approver is None:
        requests.append(InputRequest(id="approver", prompt="谁是这条工单的审批人？",
                                     schema={"type": "string", "examples": ["E-2048"]}))
    if requests:
        raise NeedsInput(requests)

    digest = hashlib.sha1(f"{title}|{priority}".encode()).hexdigest()[:4].upper()
    return {"ticket_id": f"T-{digest}", ...}
```

服务端侧：

```python
        responses = params.get("inputResponses") or {}
        merged = {**arguments, **{k: v for k, v in responses.items() if v is not None}}

        try:
            outcome = await self.registry.call(name, json.dumps(merged, ensure_ascii=False))
        except NeedsInput as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"resultType": "input_required",
                               "inputRequests": [{"id": r.id, "prompt": r.prompt, "schema": r.schema}
                                                 for r in exc.requests]}}
```

### 4.4 按参数动态校验 scope

```python
        if claims is not None and body_method == "tools/call":
            call_params = request.get("params") or {}
            tool_name = str(call_params.get("name") or "")
            try:
                check_scope(claims, tool_name, call_params.get("arguments"))
            except InsufficientScope as exc:
                self._json(exc.status, {"error": exc.payload},
                           extra_headers={"WWW-Authenticate": exc.www_authenticate})
                return
```

### 4.5 运行

```powershell
uv run python -m scripts.mrtr_client
uv run pytest
```

---

## 5. 逐块拆解

### 5.1 为什么 `NeedsInput` 要单独一个异常

它既不是"成功"也不是"失败"。用 `ToolResult(ok=False)` 表达，上层只能看到一句错误文本；
用异常表达，才能在协议层**无损转换**成 `input_required` + 结构化 `inputRequests`。

而 `registry.call()` 的 `except Exception` 必须**先放它走**：

```python
        except NeedsInput:
            raise          # 缺信息不是失败，交给协议层
        except Exception as exc:
            ...
```

**顺序错了，它会被包成"工具执行失败"，MRTR 直接失效。**

### 5.2 为什么 `inputRequests` 要带 `schema`

看第 4 步的实测输出：

```
- [priority] 这条工单的优先级是？
  schema={'enum': ['P0', 'P1', 'P2', 'P3']}
```

有了 `schema`，客户端可以**自动渲染一个下拉框**；没有 schema，它只能把 `prompt` 当纯文本丢给用户，
用户输入 `"紧急"` 而不是 `"P0"`，下一轮又失败。

**这是 MRTR 比"返回错误文本"强的地方：它是结构化的往返，不是自然语言的扯皮。**

### 5.3 为什么 `inputResponses` 只在重试时存在

第一次请求没有它，重试时有——服务端用"有没有这个字段"来判断"这是不是第二轮"。

**这要求客户端别在第一次就传一个空对象**。虽然 `{}` 也等价，但会让日志难以区分。
本节代码用 `params.get("inputResponses") or {}`，两者都能处理。

### 5.4 为什么 403 不能降级成 401

这是本节最重要的一条工程判断。

- **401** = "我不知道你是谁" → 客户端标准动作是**重新认证**
- **403** = "我知道你是谁，但你不够格" → 标准动作是**放弃或提权**

如果把权限不足返回 401，客户端会陷入"重新登录 → 再试 → 又 401"的循环。§7 第 3 步的实测里，
我们返回 403 并带上 `required` 与 `granted`，客户端就能直接告诉用户"你需要 `tools:write`"。

### 5.5 为什么 scope 要按参数动态加

实测第 7 步：

```
bob（有 write、无 approve）建 P0 → status=403
  {"error": "insufficient_scope", "tool": "create_ticket",
   "required": ["admin:approve", "tools:write"], "granted": ["tools:read", "tools:write"]}
carol（有 approve）建 P0 → status=200
  resultType=complete
```

**同一个工具、同一份参数结构，权限要求随 `priority` 变化。** 这是静态 `TOOL_SCOPES` 表达不了的。

### 5.6 幂等：重试不能重复建单

```python
    digest = hashlib.sha1(f"{title}|{priority}".encode()).hexdigest()[:4].upper()
    return {"ticket_id": f"T-{digest}", ...}
```

工单号由**内容**决定，所以"同样的答案重试两次"得到同一个号（实测 `T-FA36` == `T-FA36`）。

生产里更稳的做法是显式的幂等键（`share(checkpoint_id + tool + args_hash)`），
但核心思想一样：**写入操作必须对重试免疫**（1.5 §8 坑 2）。

### 5.7 401/403 为什么要先排空请求体

```python
            except AuthError as exc:
                # 先把请求体排空：否则客户端还在发 body，连接会被 RST
                self._drain_body()
```

如果你在客户端还在上传 body 时就关连接，客户端会收到 `ConnectionResetError` 而不是你的 401。
**这是一个纯粹的传输层细节，但会让排障方向完全跑偏。**

---

## 6. 「故意改坏它」对照实验

### 6.1 实验 A：把中文写进 `WWW-Authenticate`

我第一版是这么写的：

```python
return (
    f'Bearer realm="servicedesk-mcp", error="{self.error_code}", '
    f'error_description="{self}"'          # ← self 是中文错误信息
)
```

实测直接 500：

```
UnicodeEncodeError: 'latin-1' codec can't encode characters in position 92-93:
ordinal not in range(256)
```

栈里指向的是 `http/server.py` 的 `send_header`——**一个和鉴权毫无关系的地方**。

后果：**所有未认证的请求都变成 500，而不是 401。** 客户端以为服务端挂了，开始重试/熔断。

修法：头里只放机器可读的码，人话放响应体：

```python
return f'Bearer realm="servicedesk-mcp", error="{self.error_code}"'
```

并加了回归测试：

```python
def test_www_authenticate_is_ascii() -> None:
    assert AuthError("缺少 Authorization 头", error_code="missing_token").www_authenticate.isascii()
```

> 教训：**HTTP 头是字节，不是文本。** 任何要进头的东西，先问一句"它一定是 ASCII 吗"。

### 6.2 实验 B：缺参数时返回 `isError`

把 `create_ticket` 改成缺参数直接抛 `ValueError`（`registry` 会把它包成 `isError: true`），
客户端拿到的就只是：

```
"错误：工具 create_ticket 执行失败（ValueError: 缺少 priority）。"
```

对比 MRTR 版本：

```
{resultType: "input_required",
 inputRequests: [{id:"priority", prompt:"…", schema:{"enum":["P0","P1","P2","P3"]}},
                 {id:"approver", prompt:"…", schema:{"type":"string"}}]}
```

差别不在于"信息多少"，而在于**能不能被程序处理**：

| | `isError` 文本 | `input_required` |
|---|---|---|
| 客户端能自动渲染表单吗 | ❌ 只能把文本丢给用户 | ✅ 按 `schema` 生成控件 |
| 能自动重试吗 | ❌ 不知道补什么 | ✅ 按 `id` 填 `inputResponses` |
| 能被 Agent 消费吗 | ⚠️ 只能再问一次模型 | ✅ 结构化的"待办清单" |

**`schema` 是这两者之间唯一的分水岭。**

---

## 7. 验证与预期输出

### 7.1 八步演示

```powershell
uv run python -m scripts.mrtr_client
```

实测（节选）：

```
第 1 步：没有 token → 401，而不是 403
  status=401
  WWW-Authenticate: Bearer realm="servicedesk-mcp", error="missing_token"
  body: {'code': 'missing_token', 'message': '缺少 Authorization 头'}

第 2 步：有读权限 → 查询类工具正常
  status=200  isError=False
  内容: {"found": true, "hostname": "NB-1024", "owner": "张伟（E-2048）", ...}

第 3 步：只有读权限去建单 → 403 insufficient_scope
  status=403
  WWW-Authenticate: Bearer realm="servicedesk-mcp", error="insufficient_scope", scope="tools:write"
  body: {"error": "insufficient_scope", "tool": "create_ticket",
         "required": ["tools:write"], "granted": ["tools:read"]}

第 4 步：有写权限但没给全参数 → 200 + input_required
  status=200  resultType=input_required
    - [priority] 这条工单的优先级是？
      schema={'enum': ['P0', 'P1', 'P2', 'P3']}
    - [approver] 谁是这条工单的审批人？（P0/P1 必须有人审批）
      schema={'type': 'string', 'examples': ['E-2048']}

第 5 步：客户端补上答案，重试原请求
  重试用的 id=4（与首次相同：True）
  status=200  resultType=complete
  工单: {"ticket_id": "T-FA36", "title": "VPN 连接后立刻断开",
         "priority": "P1", "approver": "E-2048", "status": "已创建"}

第 6 步：同样答案再重试一次 → 工单号相同
  第一次: T-FA36   第二次: T-FA36
  是否相同: True   ← 幂等键 = title + priority

第 7 步：同一个工具，参数不同 → 需要的权限也不同
  bob（有 write、无 approve）建 P0 → status=403
    {"error": "insufficient_scope", "tool": "create_ticket",
     "required": ["admin:approve", "tools:write"],
     "granted": ["tools:read", "tools:write"]}
  carol（有 approve）建 P0 → status=200
    resultType=complete

第 8 步：过期 token → 401 expired_token
  status=401  error=expired_token
```

**第 4→5 步就是本节的验收点**："工具说缺审批人 → 补上后重试成功"，且重试用的是**同一个 request id**。

### 7.2 测试

```powershell
uv run pytest
```

```
........................................................................ [ 80%]
....................................                                     [100%]
180 passed in 22.17s
```

（1.x 的 78 + 阶段二的 102。）

---

## 8. 生产环境踩坑实录

### 坑 1：权限不足返回 401，客户端陷入重登录死循环

**现象**

某个角色的用户点"新建工单"，页面跳转登录页；登录回来再点，又跳登录页。

**定位方法**

看响应码与 `WWW-Authenticate`：

```python
log.warning("auth_denied", status=status, tool=tool, code=error_code)
```

如果 `status == 401` 但 `error_code == "insufficient_scope"` —— 就是这个坑。

**根因**

把 `InsufficientScope` 继承成了 `AuthError` 却复用了 `status = 401`。

**修法**

```python
class InsufficientScope(AuthError):
    status = 403            # ← 必须是 403
    error_code = "insufficient_scope"
```

**顺手加固**：客户端侧把"401 → 重新登录"和"403 → 提示权限不足"两条分支写死，
不要合并成"统一 4xx 处理"。

### 坑 2：把中文写进响应头，所有鉴权失败变成 500

**现象**

未登录访问返回 500，日志里是 `UnicodeEncodeError: 'latin-1' codec can't encode ...`。

**定位方法**

栈会指到 `http/server.py` 的 `send_header`，看起来完全不像鉴权问题。

**根因**

`WWW-Authenticate` 里带了中文 `error_description`。

**修法**

头里只放 ASCII（`error="invalid_token"`），把中文放进响应体。
并加一条回归测试：`assert header.isascii()`。

### 坑 3：MRTR 重试时发了一个新请求

**现象**

补完信息重试，服务端报"缺少参数"或"未知调用"。

**定位方法**

对比两次请求的 `id`：

```python
print("first:", first["id"], "retry:", retry["id"])
```

不同 → 就是这个坑。

**根因**

用 `build_request(...)` 新建了一个请求。新的 id 意味着服务端无法把它和上一轮关联——
除非服务端记住上一轮（又是 session）。

**修法**

```diff
- retry = build_request("tools/call", merged_params)
+ retry = build_retry_with_inputs(original, answers)
```

---

## 9. 自检与作业

### 9.1 三个自检问题

<details>
<summary><b>Q1：MRTR 为什么能保持无状态？</b></summary>

因为客户端**重试原请求**（同 id、同方法、同参数），只是多了 `inputResponses`。服务端收到时，原始参数与补充答案在同一个请求里，不需要记住"上一轮问过什么"。如果客户端发新请求，服务端就必须保存上下文——那又回到 session 了。

</details>

<details>
<summary><b>Q2：为什么权限不足不能返回 401？</b></summary>

401 的语义是"身份未验证"，客户端标准动作是重新登录。而权限不足时身份是清楚的，重新登录也拿不到缺少的 scope。返回 401 会让客户端陷入"登录 → 重试 → 401"的死循环。正确做法是 403 + `insufficient_scope` + `required`/`granted` 列表，让客户端能直接告诉用户该找谁开权限。

</details>

<details>
<summary><b>Q3：`inputRequests` 里的 <code>schema</code> 有什么用？</b></summary>

它是"能被程序处理"的关键。有 `schema`，客户端能自动渲染下拉框/输入框、能自动校验、能自动填 `inputResponses`；没有它，客户端只能把 `prompt` 当纯文本丢给用户，用户输入"紧急"而不是"P0"，下一轮又失败。

</details>

### 9.2 动手任务：给 MRTR 加"多轮"与"时限"

**要求**

1. 支持**两轮以上**：`create_ticket` 先要 `priority`，拿到后再要 `approver`（而不是一次要两个）。
2. 给 `inputRequests` 加 `expiresAt`（人类可读截止时间）；超时后的重试返回 `FatalError` 语义的 `isError`。
3. 客户端侧限制最多 3 轮，超过就放弃并给用户明确提示。
4. 补 3 个用例：两轮成功、超时失败、超轮次失败。

**验收标准**

- 第一轮只返回 1 个 `inputRequest`；第二轮再返回剩下的 1 个。
- `expiresAt` 用假时钟（monkeypatch `time.time`）验证。
- 客户端超过 3 轮抛明确异常，而不是无限循环。
- 原有 180 个用例仍全绿。

**提示**：多轮 MRTR 仍然无状态——每一轮都是"原请求 + 累积的 `inputResponses`"，**不要**在服务端记"这是第几轮"。

### 9.3 拓展挑战

把 scope 校验从"工具级"下沉到"数据级"：`query_asset` 永远只能查自己部门的设备。

**思考**：

1. 数据级权限该放在哪一层——`McpServer`、`ToolRegistry` 还是工具函数本身？
2. 如果 scope 里带 `dept:network`，而工具参数里有 `hostname`，你怎么把两者关联起来？
3. 越权尝试应该返回 403 还是"空结果"？（提示：返回空结果会让攻击者无法探测资源是否存在，但也让排障变难）

---

## 10. 本节小结与延伸

**三点回顾**

1. **"做不了"要分三种**：缺信息（200 + `input_required`）、缺权限（403 + `insufficient_scope`）、工具失败（200 + `isError`）。混一行，用户体验就崩一类。
2. **MRTR 靠"重试原请求"保持无状态**：同 id、同方法、同参数，只多 `inputResponses`。`inputRequests` 里的 `schema` 是"能被程序处理"的分水岭。
3. **HTTP 头是字节不是文本**：把中文塞进 `WWW-Authenticate` 会让所有鉴权失败变成 500（实测 `UnicodeEncodeError`），而栈指向一个和鉴权无关的地方。

**已知限制（诚实记录）**

- **OAuth 只做了 token + scope**：没有实现授权码流程、`iss` 校验（RFC 9207）、JWKS 轮换、CIMD。生产必须补齐。
- **无数据级权限**：所有 `tools:read` 都能查所有设备（§9.3 是作业）。
- **MRTR 只支持一轮补全**：两轮以上是 §9.2 作业。
- **幂等键是内容哈希**：生产应用显式的幂等键存储（1.5 §8 坑 2）。

**延伸阅读**

- MCP Authorization：https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- MRTR 的 SEP-2322（设计动机）：https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2322
- RFC 6750（Bearer Token 与 `WWW-Authenticate`）：https://www.rfc-editor.org/rfc/rfc6750
- RFC 9207（`iss` 参数校验）：https://www.rfc-editor.org/rfc/rfc9207

---

## 修订记录

- 2026-11-17 初稿：八步演示与 20 个测试全部实机运行，§7 为真实输出
- 2026-11-17 修正：`WWW-Authenticate` 含中文导致 `UnicodeEncodeError` → 500（实测），改为头内只放 ASCII，并加回归测试
- 2026-11-17 修正：scope 校验从"仅按工具名"改为"按工具 + 参数"，使建 P0 单需要 `admin:approve`；否则第 7 步演示的"缺授权"根本没有发生
- 2026-11-17 修正：401/403 响应前先排空请求体，消除客户端未发完 body 导致的连接重置
- 2026-11-17 说明：新增 `create_ticket` 使工具总数从 3 变 4，已同步更新 2.1/2.4 相关测试断言
