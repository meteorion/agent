# 报错对照表

> 报错原文 → 人话翻译 → 先查哪里。按出现顺序追加。

| 报错原文 | 人话翻译 | 先查哪里 | 出现小节 |
|---|---|---|---|
| `ModuleNotFoundError: No module named 'servicedesk'` | Python 找不到你的包 | ① 有没有 `uv sync` ② 是不是在 `src/` 里执行 ③ `__init__.py` 漏了没 | 1.1 |
| `ValueError: Unable to determine which files to ship inside the wheel` | 打包工具按项目名找不到包目录 | `[tool.hatch.build.targets.wheel] packages` 是否写了 `["src/servicedesk"]` | 1.1 |
| `Call to hatchling.build.build_editable failed (exit code: 1)` | 上一行的外层包装，真正原因往下翻 | 看 `ValueError` 那几行，别停在表层 | 1.1 |
| `Extra inputs are not permitted [type=extra_forbidden]` | `.env` 里有 `Settings` 未声明的键，且 `extra` 不是 `ignore` | `SettingsConfigDict(extra="ignore")` | 1.1 |
| 命令无任何输出、退出码 1 | Windows Store 的 `python.exe` 占位别名在静默失败 | `Get-Command python` 是否指向 `WindowsApps`；改用 `uv run python` | 1.1 |
| `RuntimeWarning: coroutine 'x' was never awaited` | 只是创建了协程对象，没提交给事件循环，逻辑没执行 | 漏写 `await`；用 `-W error::RuntimeWarning` 跑一次 | 1.2 |
| 异步代码跑了但并发完全不生效（无报错） | 协程里混进同步阻塞调用，事件循环被按住 | 搜 `time.sleep` / `requests.` / 同步 DB 驱动；装心跳检测 drift | 1.2 |
| 并发 3 个只报出 1 个错误 | `gather` 默认只抛第一个异常，其余丢弃 | 改 `TaskGroup` + `except*`，或 `gather(..., return_exceptions=True)` | 1.2 |
| `ModuleNotFoundError: No module named 'scripts'` | 直接跑 `python scripts/x.py` 时 `sys.path[0]` 变成了 scripts/ 目录 | 改用 `uv run python -m scripts.x` | 1.3 |
| `HTTP 401: 缺少 Authorization: Bearer` | 请求头没带 key，不是网络问题 | `.env` 的 `LLM_API_KEY` 是否为空；`has_llm_key` 是否为真 | 1.3 |
| `HTTP 404`（同一 key 本地正常） | `LLM_BASE_URL` 配错，少写或多写了 `/v1` | 打印 `response.request.url` 看实际打到哪 | 1.3 |
| `JSONDecodeError: Expecting value: line 1 column 1` | SSE 切到了半行，或把 `: ping` 注释行/`[DONE]` 当 JSON 解析 | 改用 `aiter_lines()`，过滤非 `data:` 行与 `[DONE]` | 1.3 |
| `Unclosed client session` / `PoolTimeout` | `AsyncClient` 用完没关，连接泄漏 | 用 `async with`，全应用共用一个长生命周期 client | 1.3 |
| `JSONDecodeError: Expecting value: line 1 column 1`（模型输出） | 输出被 markdown 代码块或前后废话包裹 | 先 `strip_code_fence` 再 `extract_json_object` | 1.4 |
| `JSONDecodeError: Unterminated string starting at` | 用正则抠 JSON，在字符串内部的 `}` 处截断了 | 改括号配对法，跟踪 `in_string`/`escaped` | 1.4 |
| `Extra inputs are not permitted [extra_forbidden]`（模型输出） | 模型自己加了未定义字段 | `extra="forbid"` + 把字段名写进 repair_message | 1.4 |
| `Field required [type=missing]` | 模型漏了必填字段 | repair_message 里对 missing 显示"（未提供）"而非整个对象 | 1.4 |
| `TypeError: BaseModel.__init__() takes 1 positional argument but 2 were given` | 把 pydantic v1 的写法用在 v2 上 | 用 `model_validate`/`model_dump`；跑一次 `bump-pydantic` | 1.4 |
| `AttributeError: type object 'X' has no attribute 'parse_obj'` | 同上，v1 API 在 v2 中已移除 | 见 §8 坑 1 的 v1→v2 对照表 | 1.4 |
| 重试率 ≈ 100%，QPS 放大数倍 | 把 4xx 当成了可重试，故障时自己放大流量 | 看 `retries/attempts`；状态码用白名单而非黑名单 | 1.5 |
| 同一业务被重复执行（工单建了三条） | 重试让模型的工具调用被重复发送 | 写操作加幂等键；只对读操作自动重试 | 1.5 |
| 堆栈最底层停在 `raise error` | `raise error` 吞掉了原始异常链 | 改 `raise error from exc`，保留 `__cause__` | 1.5 |
| 日志里出现「（已尝试 1 次）（已尝试 3 次）」 | 重试包装时复用了已拼接过的 `reason` | 用 `base_reason` 重新构造 | 1.5 |
| 调整 `chunk_size` 后块数一点没变 | `min_chars >= max_chars`，每块都被判"太短"并合并回一整块 | 加参数校验；打印 `chunk_stats` 的 `count` 与 `max` | 1.6 |
| `text.count("```") % 2 == 1`（围栏不闭合） | 按字符硬切，切点落在代码块中间 | 等长占位符保护代码块；断言围栏成对 | 1.6 |
| 回答给出无主语的片段（如"退出所有代理后重连"） | 表格/句子被切开，块里没有主语 | 检查块开头是否为 `\|`；表格也需原子保护 | 1.6 |
| 重启后检索结果全乱，且无报错 | 用内置 `hash()` 做向量哈希，跨进程盐不同 | 改用 `crc32`/`sha256` 等稳定哈希 | 1.7 |
| 换 embedding 模型后检索毫无变化或全乱 | 索引还是旧模型建的，两个向量空间不可比 | `recreate()` 重建；把模型名+维度存进集合元信息并启动校验 | 1.7 |
| `IndexError: list index out of range`（检索处） | `hits[0]` 但检索结果为空 | 空结果是正常分支，不是异常 | 1.7 |
| 上了 rerank 但准确率没变 | 正确答案根本不在候选里，rerank 只能重排 | 先提 `top_k`/改切分保召回，再谈重排 | 1.7 |
| `AnswerParseError`：输出里找不到 JSON 对象 | 模型给了自然语言而不是约定结构 | 降级为纯文本并标 `parse_ok=False`；用 repair_message 回喂 | 1.8 |
| 回答里出现 `[C9]`，但资料只有 5 条 | 模型编造引用编号 | 校验编号存在性；编造则整段作废并保留 `raw_answer` | 1.8 |
| 引用编号对，但引用内容不支持结论 | 模型"顺口引用"，归因失败 | 校验 `quote` 是否为原文子串（§10.2 作业） | 1.8 |
| 换 embedding 后 `no_evidence` 比例暴涨 | 证据阈值是标定值，换模型/换切分后失效 | 用标定集重算阈值；阈值放配置并记录标定版本 | 1.8 |
| `TypeError: string indices must be integers` | 把 `arguments` 当成 dict 用了，它其实是 JSON 字符串 | 先 `json.loads(arguments)`，且只在 registry 里解析一次 | 2.1 |
| `TypeError: f() got an unexpected keyword argument` | 模型的参数里多了字段，且没做校验/过滤 | 用 pydantic `model_validate` 兜住（默认忽略多余字段） | 2.1 |
| `HTTP 400: messages[n] 是 tool 消息，但它前面没有带 tool_calls 的 assistant 消息` | 漏发了 assistant 的 tool_calls，或顺序错了 | 先把 assistant（含 tool_calls）原样 append，再 append tool 消息 | 2.1 |
| 模型第二轮答非所问（"我没发起过这个调用"） | tool 消息缺 `tool_call_id`，模型无法配对 | 每条 tool 消息必须带 `tool_call_id` | 2.1 |
| 用户问权限，模型却调了查资产 | 工具描述含糊、边界不清 | 描述里写清"什么时候用"与"什么时候别用"，参数加 `pattern` | 2.1 |
| 账单暴涨，某个"查文档"工具调用量极高 | 把只读数据做成 Tool，模型每次都"决定去读"全文 | 改成 Resource 由 Host 挂载；或限制 Tool 返回长度 | 2.2 |
| MCP Server 上线后无人复用、改一次要发两个服务 | 只有一个消费方就上了协议（M=1） | 先写普通函数，出现第二个消费方再抽象 | 2.2 |
| `404: Session not found`（多实例部署） | 旧版把状态藏在 session 里，请求落到别的实例 | 迁移到 2026-07-28：状态改显式句柄 | 2.3 |
| 升级后旧客户端全部报版本不支持 | Server 只声明新版本，没做 dual-era 双栈 | 过渡期同时支持新旧，`server/discover` 如实广播 | 2.3 |
| 旧版 Server 一夜之间全部失败 | 把「缺 `resultType`」当成错误 | 缺失或不认识一律按 `complete` | 2.3 |
| 句柄 `h_1`/`h_2` 能被枚举，读到他人数据 | 用自增 id 做句柄 | 用 uuid4 + 归属校验 + TTL | 2.3 |
| 流式响应重连后内容缺一段且无报错 | 新版已移除 SSE 断线续传（无 `Last-Event-ID`） | 断流按失败处理，用新 request id 重发 | 2.3 |
| MCP 客户端偶发"JSON 解析失败"，重试又好了 | 日志/print 写进了 stdout，污染协议通道 | `configure_logging(stream=sys.stderr)`；stdout 只写协议 | 2.4 |
| 模型参数写错导致整个对话中断 | 工具失败被包成 JSON-RPC `error` 而不是 `isError` | 工具层失败返回 `isError: true` + 错误文本 | 2.4 |
| 客户端行为异常但无报错 | 返回结果漏了 `resultType` | 所有结果统一 `{"resultType": "complete", ...}` | 2.4 |
| `KeyError: 'content'`，响应里是工具列表 | 网关按 `Mcp-Method` 头缓存，头与 body 不一致 | 服务端校验头与 body 一致；网关缓存 key 含 body 指纹 | 2.5 |
| 订阅期间状态变更"丢了"，重连后也没补 | 新版已移除续传，断流期间事件永久丢失 | 通知只做提示，收通知后主动拉取 + 定期对账 | 2.5 |
| `RuntimeError: asyncio.run() cannot be called from a running event loop` | 在异步框架里又调了 `asyncio.run` | 改成 `await`；传输层与逻辑层分离 | 2.5 |
| `UnicodeEncodeError: 'latin-1' codec can't encode`（在 `send_header`） | 把中文塞进了 `WWW-Authenticate` 等响应头 | 头里只放 ASCII；人话放响应体；加 `isascii()` 回归测试 | 2.6 |
| 权限不足却返回 401，客户端反复跳登录 | `InsufficientScope` 沿用了 `status = 401` | 改 403 + `insufficient_scope` + `required`/`granted` | 2.6 |
| MRTR 补完信息后重试，服务端说"缺少参数" | 重试时发了新请求（新 id） | 用 `build_retry_with_inputs` 重试**原请求** | 2.6 |
| 客户端收到响应前连接被重置（`ConnectionResetError`） | 401/403 在客户端还在发 body 时就关了连接 | 响应前先排空请求体（`_drain_body`） | 2.6 |
| `langchain-mcp-adapters` 连 Server 报 `400 Bad Request` | `mcp 1.30.0` 仍发 legacy `initialize`（2025-11-25）且不带 `Mcp-Method` | Server 做 dual-era：接受 initialize 与无头的 legacy 请求 | 2.7 |
| 模型一个工具都不调，直接瞎答（无报错） | 适配器的 `args_schema` 是 dict，代码按"模型类"判断导致工具全被跳过 | 用 `tool.get_input_schema()` 取 pydantic 模型 | 2.7 |
| 工具返回给模型的是三层嵌套 JSON | MCP 返回 `[{type:text,text:...}]`，未拆块直接塞给模型 | 抽出 `text` 再尝试 `json.loads` | 2.7 |
| 兼容 legacy 后路由头校验失效 | 放宽条件写成"没有 `Mcp-Method` 就放行" | 只放行真正的 legacy 流量（initialize / 通知 / 2025-11-25） | 2.7 |
| Node 里 `state["x"].append(...)` 后下游看不到更新 | 原地修改可变对象，不会被识别为增量 | 返回新值：`{"x": [*state.get("x", []), v]}` | 3.1 |
| 某节点的产出覆盖了别的节点写的字段 | Node 返回了**整个** state（把读到的旧值也写回） | 只返回自己改的字段 | 3.1 |
| 早期返回的分支缺字段，下游 KeyError | 手写编排里状态靠参数传递，分支间不同步 | 换状态图：State 集中，节点只做增量更新 | 3.1 |
| 上一节点写的字段下一节点读不到（只剩最后一次） | 该字段没声明 reducer，默认覆盖生效 | 加 `Annotated[..., operator.add]` 等 reducer | 3.2 |
| 多轮对话只剩最后一条消息，模型答非所问 | `messages` 没加 `add_messages`，历史被整个替换 | `Annotated[list, add_messages]` | 3.2 |
| `trace` 里同一步出现两次且越跑越多 | 自定义 reducer 原地 `extend` 了入参 | reducer 只读入参、返回新对象 | 3.2 |
| `TypeError: 'HumanMessage' object is not subscriptable` | `add_messages` 把 dict 转成了 `BaseMessage` | 用 `.content` / `.type` 或写兼容读函数 | 3.2 |
| `GraphRecursionError: Recursion limit of N reached` | 图陷入循环；且默认上限是 10007，等它触发已烧完 | 在 State 里加 `max_attempts` 业务刹车 | 3.3 |
| 用户总被要求"补充信息"，但真实原因是下游故障 | 异常被 `except: docs = []` 吞成"空结果" | 写 `state.error`，条件边导到 fallback | 3.3 |
| 分支跑进了不该去的节点 | router 返回值不在映射 key 里（大小写/None） | 归一化返回值 + 用 `Literal` 标注返回类型 | 3.3 |
| 循环重试次数比预期多 | 在 router 里给 `attempts` 加一（router 可能被重放） | 计数只在节点里做，router 只读 | 3.3 |
| `NotImplementedError: The SqliteSaver does not support async methods` | 异步图配了同步 saver | 换 `AsyncSqliteSaver`（`...sqlite.aio`）+ `await saver.setup()` | 3.4 |
| 异步 saver 下 `get_state` 抛 `NotImplementedError` | 读状态仍用同步接口 | 改用 `aget_state` / `aget_state_history` | 3.4 |
| 恢复时传了 dict，结果从头跑 / 状态被覆盖 | `ainvoke(dict)` 是"新输入"，不是"继续" | 继续要用 `ainvoke(None, config)` | 3.4 |
| 用户收到两条相同通知 | 被中断的节点重跑，副作用非幂等 | 给副作用加幂等键，或把写操作拆到链尾单独节点 | 3.4 |
| checkpoint 表/文件无限增长 | 每个节点都存档且从不清理 | 按 thread + 时间清理；监控体积 | 3.4 |
| `NameError: name 'operator' is not defined`（在 `<string>` 里） | State 用了 `Annotated[..., operator.add]` 但模块没 import | 补 `import operator`；注解里的名字必须模块级可见 | 3.5 |
| `UnicodeDecodeError: 'gbk' codec can't decode` | `subprocess.run(text=True)` 用系统编码解码 UTF-8 输出 | 显式 `encoding="utf-8", errors="replace"` | 3.5 |
| 审批挂起后 thread 永久卡住 | `interrupt` 没有定时器，没人恢复就一直等 | 定时扫描过期 thread 并投递 resume/作废 | 3.5 |
| 人类点一次批准，通知却发了两次 | 副作用写在 `interrupt()` 之前，恢复时节点从头重跑 | 副作用放 `interrupt()` 之后或独立节点 | 3.5 |
| 流式事件攒到最后一次性刷出 | 节点是同步函数 + 同步 I/O，占满线程池 | 节点改 `async def`，I/O 改 `await` | 3.6 |
| 用户界面里出现 `LangGraph` / `__start__` | 未过滤框架内部节点 | 过滤内部名 + `NODE_LABELS` 映射人话 | 3.6 |
| 并发请求日志互相串 trace_id | 忘了 `clear_request_context()` | `bind` 后必须 `finally: clear` | 3.6 |
| 子图挂起后 `pending_interrupt` 拿不到 / 报错 | 用包装函数接带 `interrupt` 的子图，另起了执行 | 带 interrupt 的子图必须直接当节点 | 3.7 |
| 子图字段"写了但父图读不到" | 父子图 schema 不一致，合并时被丢弃 | 共享同一 schema 或用显式映射 | 3.7 |
| 审批通过后前置检索/计费翻倍 | 子图内 interrupt 恢复会重跑前置兄弟节点 | 前置纯读 / 副作用后置 / 不嵌子图 | 3.7 |
| 偶发 `httpx.ReadError`，单跑却通过 | 服务端在 404/401 提前返回前没读请求体，客户端被 RST | 返回前统一排空 body（`_drain_body`，幂等） | 4.1 |
| 评测 100% 通过，上线一半失败 | 判分器只看「有没有输出」，不看内容特征 | 判分看具体特征 + 加「不可能满足」的用例 | 4.1 |
| 调 A2A 返回 `-32601 Method not found` | 用了旧方法名 `tasks/send` | 新代码用 `message/send`；服务端保留旧名别名 | 4.2 |
| 补信息后原任务一直停在 `input-required` | 续谈没带 `taskId`，服务端新建了任务 | 续谈必须带 `taskId`；服务端在状态消息里回显 | 4.2 |
| 服务重启后所有 `working` 任务消失 | 任务表在内存里 | 换 Redis/DB；加 TTL | 4.2 |
| 对端 QPS 被打爆 | 客户端轮询间隔太短 | 指数退避；长任务用 `message/stream` | 4.2 |
| 只有部分任务无限循环 | 用「字段是否为空」推断进度，无资料任务永远为空 | 改显式 `stage` 路由；别拿数据形状当状态机 | 4.3 |
| handoff 里两个角色互相打回十几次 | 没有中心节点，没人发现循环 | 在被反复调用的节点加 `visits` 止损 | 4.3 |
| 架构评审时没人画得出图 | handoff 的跳转写在 `Command` 里，边表为空 | 需要可评审就选 supervisor；handoff 每节点写清 goto 条件 | 4.3 |
| 用户强调过的禁令被 Agent 忘掉 | 压缩只做摘要，关键约束被截掉 | hybrid + 约束抽取钉住；断言 `lost_constraints` 为空 | 4.4 |
| 压缩后 token 几乎没降 | 「要不要」被误判成禁令，pinned 越积越多 | 正则 + 负向前瞻 `(?<!要)不要` | 4.4 |
| 压缩后仍超预算且 `dropped=0` | 单条消息自身就超预算 | 单条截断 / 大结果落盘只留引用 | 4.4 |
| hybrid 也丢约束 | 忘了调 `pin_all_constraints()`，退化成 window | 把 pin 纳入压缩流程 | 4.4 |
| Agent 把一周前的验证码当有效信息 | 一次性信息没设 TTL，召回也没过滤 | 写入设 `ttl_s`；召回**永远**检查 `expired()` | 4.5 |
| 同类问题的召回结果反复横跳 | 打分权重拍脑袋，冠军只赢 0.0014 | 用标注评测集调权重；加召回分数下限 | 4.5 |
| 删了记忆仍能召回内容片段 | 只删主表，向量索引/缓存没删 | 两边一起删，或统一过 `_forgotten` 过滤 | 4.5 |
| 加隔离后 Agent 突然什么都不懂 | 隔离规则把 `user_id="*"` 的公共知识也当泄漏 | 用 `not in (user_id, "*")`；公共知识不算泄漏 | 4.6 |
| 上次失败的结论被当成事实 | `session` 来源权重够高，失败/成功无区分 | 加 `confidence`/`outcome` 字段，低置信丢弃 | 4.6 |
| 同一问题答复时对时错 | 矛盾信息并列进入 prompt，模型随机采信 | 冲突检测 + 显式提示保持两条 | 4.6 |
| 优化后 final_tokens 降了但账单涨了 | 用「最后一轮大小」当成本指标，忽略每轮重发 | 用 `Σ t_i`（累计成本），并计入压缩开销 | 4.7 |
| 短会话测不出约束丢失 | 8 轮内约束还在「最近 4 条」里 | 压测跑到 50 轮并接进 CI | 4.7 |
| 压测两次数字对不上 | 用了 `time.time()` / 随机数 / 字典顺序 | 固定时钟与语料；断言两次结果一致 | 4.7 |
| 评测通过率不低，但越权请求全被答应 | 平均分稀释了安全判据（错 1/5 仍有 0.8） | 安全判据一票否决（`HARD_CRITERIA`） | 5.1 |
| 数据集体检永远「不干净」 | 把「分桶样本不足」当成了数据错误 | 提醒与错误分开：只有重复/泄漏算腐坏 | 5.1 |
| 评测分数越调越高，上线没变化 | 拿 test 集调参 | dev/test 严格隔离；`audit` 检测泄漏 | 5.1 |
| 单跑通过、全量偶发失败 | 断言依赖了跨模块全局状态（如共享计数器） | 断言增量而非绝对值 | 5.1 |
| `TypeError: Faithfulness.__init__() missing 1 required positional argument: 'llm'` | RAGAS 指标必须传 LLM，离线不可跑 | 用确定性代理指标，或配 API key | 5.2 |
| 正确答案被判成很低的分 | 用字符级重叠算「相关性」，中文问句与答句用词天然不重叠 | 换成对参考答案的覆盖，或接真实 LLM 判分 | 5.2 |
| top_k 调大混入噪声，precision 仍是 1.0 | 用了排序加权 AP，它不惩罚尾部噪声 | 改用「相关片段占比」，或另加噪声率指标 | 5.2 |
| 拿代理指标与论文分数对比 | 两者口径不同（token 覆盖 vs LLM 判断） | 只在同一套指标内做纵向对比并显式标注 | 5.2 |
| `DeepEvalError: OpenAI API key is not configured` | DeepEval 4.x 所有指标都走 LLM 路径 | 用确定性判据，或配 API key | 5.3 |
| 修好的 Agent 仍被判注入失守 | 朴素子串匹配把「我不能提供系统提示词」当成泄漏 | 逐句判断 + 拒绝词豁免 | 5.3 |
| 评测全绿但线上重复建单/未审批关单 | 只评了工具名，没评顺序与权限 | 加 `tool_order`（盲写）与 `authorization` | 5.3 |
| 安全阈值 90% 在样本变多后失效 | 百分比阈值随分母增大而变宽松 | 安全类用 100%，或改绝对条数上限 | 5.3 |
| 门禁恒绿（可被绕过） | 把「指标缺失」当通过，删掉评测脚本就能过 | 缺失按失败处理 | 5.4 |
| 门槛被悄悄降低 | 本地跑 `--save-baseline` 并提交 | 显式参数 + baseline diff 走 review + CI 禁用它 | 5.4 |
| CI 红了但看不到评测报告 | 报告步骤没有 `if: always()` | 报告与归档都加 `if: always()` | 5.4 |
| 同一 commit 有时绿有时红 | 评测本身不确定，或指标在阈值边缘抖动 | 评测确定化；质量类留波动余量；抖动指标先降级 warn | 5.4 |
| 调用树拉平成一堆根节点 | 解析 traceparent 时把上游当 parent_id 又生成新 span_id | 解析结果代表**上游跨度**，本地跨度用 `.child()` | 5.5 |
| 下游 span 的 parent 指向不存在的跨度 | `inject` 传了中间 child 而非当前 span | 下发时放**自己**的 span_id | 5.5 |
| 慢请求/失败请求在追踪里看不到 | span 只写在成功分支 | 挂在收敛出口（如统一响应函数） | 5.5 |
| 畸形 traceparent 导致 500 | 解析失败抛异常 | 宽容丢弃、自己起新链路 | 5.5 |
| 某条护栏规则完全不生效 | `split` 配了取反字符类，切出来只剩标点 | 改用 `findall`；打印切分结果自检 | 5.6 |
| 用户问「能不能跳过审批」被拦 | 只匹配关键词，没区分请求与提问 | 请求式标记优先；纯疑问放行 | 5.6 |
| 监控显示 PII 已拦截，输出里仍是原号码 | 多条改写都基于原文，后者覆盖前者 | 链式改写：后一条基于前一条 | 5.6 |
| 以为在测熔断，实际被限流拦了 | 闸门顺序是限流在前，限流容量太小 | 看 `reason` 而不是只看降级后的 tier | 5.7 |
| 下游故障期间 QPS 不降反升 | 外层重试不知道熔断已 OPEN | 重试必须在熔断器之内，先问 `allow()` | 5.7 |
| 降级率 40% 却没人发现 | 监控只看错误率，降级不报错 | 把 `tier_counts` 与降级原因进监控并告警 | 5.7 |
| 检查单永远绿，却频繁出事 | 自动检查只读缓存结果，不真跑 | 检查必须现场执行被测代码 | 5.8 |
| 人工项设成 warning，等于没有 | 「回滚没演练」也能上线 | 关键人工项必须是 blocker | 5.8 |
| 演练通过但上线失败 | 演练与上线用两套判定逻辑 | `overrides` 只替换结果，不改判定路径 | 5.8 |
