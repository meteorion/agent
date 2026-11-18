# 术语表

> 教程内所有术语的中英对照与统一译名。新术语首次出现时必须登记。

| 中文（English） | 一句话定义 | 出现小节 |
|---|---|---|
| 包（package） | 一个含 `__init__.py` 的目录，能被 `import` | 1.1 |
| 模块（module） | 单个 `.py` 文件，也能被 `import` | 1.1 |
| 虚拟环境（virtual environment，venv） | 项目专属的依赖隔离目录，可随时删掉重建 | 1.1 |
| src 布局（src layout） | 把包放进 `src/`，逼你必须正确安装才能 import | 1.1 |
| 可编辑安装（editable install） | 以"指向源码"的方式安装本项目，改代码后无需重装 | 1.1 |
| 版本区间（version range） | 如 `>=2.9,<3.0`，表达兼容范围而非精确版本 | 1.1 |
| 依赖锁定（lock） | 把区间解析成精确版本并写入 `uv.lock`，保证可复现 | 1.1 |
| 依赖组（dependency group） | 只在开发期需要的依赖，如 pytest / ruff | 1.1 |
| 构建后端（build backend） | 负责把源码打成 wheel 的工具，本节用 hatchling | 1.1 |
| 环境变量（environment variable） | 进程启动时从外部注入的键值对，用来放配置与密钥 | 1.1 |
| 空字符串陷阱（blank-string trap） | `""` 被当成"有值"，导致配置校验说谎 | 1.1 |
| 冒烟测试（smoke test） | 只验证"环境是否活着"的最小测试集 | 1.1 |
| 协程（coroutine） | `async def` 定义的函数，调用后返回未执行的"待执行物" | 1.2 |
| 事件循环（event loop） | 调度所有协程的循环，负责"谁可以跑" | 1.2 |
| await | 协程"我要等了，先把位置让出来"的动作，也是唯一的切换点 | 1.2 |
| 任务（Task） | 被事件循环正式受理、可独立跑完的协程 | 1.2 |
| 并发（concurrency） | 多个任务交替推进，单线程即可实现 | 1.2 |
| 并行（parallelism） | 多个任务真同时在多核上执行，需要多进程 | 1.2 |
| GIL（全局解释器锁） | 同一时刻只允许一个线程执行 Python 字节码 | 1.2 |
| 取消（cancellation） | 通知任务停止，会抛 `CancelledError` 但仍执行 `finally` | 1.2 |
| 结构化并发（structured concurrency） | 用 `TaskGroup` 保证"一起开始、一起收尾、失败连坐" | 1.2 |
| 异常组（ExceptionGroup） | 把多个异常打包成一个，配合 `except*` 逐个处理 | 1.2 |
| base_url | 服务地址根，含版本前缀如 `/v1`；配错就是 404 | 1.3 |
| 角色（role） | 消息的作者身份：system / user / assistant / tool | 1.3 |
| token | 模型计费与上下文长度的最小单位，约等于"字/词片段" | 1.3 |
| usage | 响应里的 token 统计：prompt / completion / total | 1.3 |
| 流式（streaming） | 服务端边生成边下发，客户端边收边渲染 | 1.3 |
| SSE（Server-Sent Events） | 一种"服务端单向推送"的文本协议，每行 `data: ...` | 1.3 |
| 首字延迟（TTFT） | 从发出请求到收到第一个字符的时间，流式体验的关键指标 | 1.3 |
| 连接池（connection pool） | 复用 TCP/TLS 连接，省掉每次握手 | 1.3 |
| include_usage | 流式请求里要求返回 token 统计的开关，不传就静默拿不到 | 1.3 |
| 数据契约（data contract） | 一份"字段、类型、约束"的声明，让数据在跨模块前先被校验 | 1.4 |
| BaseModel | pydantic 的模型基类，负责字段解析与校验 | 1.4 |
| 归一化（normalization） | 把"能猜到意思"的脏值映射成合法值，如"紧急"→P0 | 1.4 |
| field_validator | 单字段校验器；`mode="before"` 在类型转换前跑 | 1.4 |
| model_validator | 模型级校验器；`mode="after"` 用于跨字段规则 | 1.4 |
| JSON Schema | 描述 JSON 结构的标准，function calling 的 `parameters` 就是它 | 1.4 |
| `$defs` | JSON Schema 里的可复用定义区，枚举会以 `$ref` 引用它 | 1.4 |
| 宽松模式（lax） | pydantic 默认模式，会尽力把 `"0.9"` 转成 `0.9` | 1.4 |
| 强制转换（coercion） | 宽松模式下发生的隐式类型转换，会掩盖上游格式漂移 | 1.4 |
| 纠错回喂（repair loop） | 把校验错误翻译成提示再喂给模型，让它重说一次 | 1.4 |
| RetryableError | 暂时性故障（429/5xx/超时/网络）；抛出时代表重试已用尽 | 1.5 |
| NeedsHumanError | 重试无用且必须有人介入（401/403），带 `hint` 说明改什么 | 1.5 |
| FatalError | 重试无意义（400/404/422），通常是代码或 prompt 的问题 | 1.5 |
| 指数退避（exponential backoff） | 每次重试把等待时间翻倍，避免持续打垮上游 | 1.5 |
| 抖动（jitter） | 在退避值上加随机比例，打散同时重试的洪峰 | 1.5 |
| 惊群（thundering herd） | 大量客户端在同一瞬间重试，把刚恢复的服务再次打垮 | 1.5 |
| 重试预算（retry budget） | 累计等待时间的上限，超了就放弃 | 1.5 |
| 幂等键（idempotency key） | 让重复执行产生同一结果的标识，重试安全的前提 | 1.5 |
| 结构化日志（structured logging） | 日志是键值对而非字符串，可被机器聚合统计 | 1.5 |
| 上下文变量（contextvars） | 协程级的隐式上下文，用来给日志自动挂 trace_id | 1.5 |
| 块（chunk） | RAG 的最小检索单元，一段可独立引用的文本 | 1.6 |
| 标题路径（heading_path） | 从一级标题到当前小节的完整路径，小块的主语来源 | 1.6 |
| 重叠（overlap） | 相邻块共享的一段尾巴，用来接住被切断的句子 | 1.6 |
| 原子单元（atomic unit） | 不允许再拆的最小片段，如代码块、表格 | 1.6 |
| 等长占位符 | 用同样长度的字符替换代码块，保证字符偏移不变 | 1.6 |
| embedding_text | 真正拿去算向量的文本（标题路径 + 正文） | 1.6 |
| 断句处 | 块边界落在句子中间的位置，是"两边都读不懂"的根因 | 1.6 |
| IDF（逆文档频率） | 越少见的词权重越高，用来压掉"的""我们"这类泛词 | 1.6 |
| 向量化（embedding） | 把文本映射成定长数值向量，语义相近则向量相近 | 1.7 |
| 向量维度（dim） | 向量的长度；换模型常导致维度变化，必须重建索引 | 1.7 |
| 余弦相似度（cosine） | 两向量夹角的余弦，越接近 1 越相似，与长度无关 | 1.7 |
| top-k 检索 | 取相似度最高的 k 条；"最近邻"永远有结果 | 1.7 |
| 相似度阈值（min_score） | 低于它的命中被丢弃，允许返回空 | 1.7 |
| rerank | 对已召回的候选重新排序；不增加召回 | 1.7 |
| 召回率 / 精确率 | 前者=该找到的有没有找到，后者=找到的有多准 | 1.7 |
| payload | 向量库中随点存储的元数据，用于过滤与回填 | 1.7 |
| MMR（最大边际相关性） | 在相关性与多样性间折中，避免返回重复内容 | 1.7 |
| Protocol（结构化类型） | 只看"有没有这些方法"，不要求继承，便于替换实现 | 1.7 |
| RAG（检索增强生成） | 先检索资料，再让模型基于资料回答 | 1.8 |
| 引用（citation） | 回答里指向具体资料编号的标注，用于溯源与审计 | 1.8 |
| 接地（grounded） | 回答有真实资料支撑且带有效引用 | 1.8 |
| 证据闸门（evidence gate） | 证据分低于阈值就不调用模型，直接回"查不到" | 1.8 |
| 拒答（abstention） | 明确说"没有相关信息"，是正确行为而非失败 | 1.8 |
| 幻觉（hallucination） | 模型生成看似合理但无依据的内容 | 1.8 |
| 归因（attribution） | 结论与引用内容的对应关系；"引用对但内容不对"就是归因失败 | 1.8 |
| 函数调用（Function Calling） | 模型返回"我想调哪个函数、参数是什么"，由你的代码执行 | 2.1 |
| 工具（tool） | 以 schema 形式暴露给模型的可调用能力 | 2.1 |
| tool_calls | assistant 消息里表示"要调用这些工具"的字段 | 2.1 |
| arguments | 工具参数，**是 JSON 字符串**而非对象，可能不合法 | 2.1 |
| tool_call_id | tool 消息里用来对上某次调用的标识，缺了模型对不上号 | 2.1 |
| 工具循环（tool loop） | 要工具 → 执行 → 回喂 → 直到给出最终回答；Agent 的最小形态 | 2.1 |
| 严格模式（strict） | 服务端对 schema 的强约束，不接受 `$ref` 等写法 | 2.1 |
| `$ref` 内联 | 把 `$defs` 里的定义递归展开，满足 strict 模式要求 | 2.1 |
| tool_choice | 控制模型"必须调 / 不许调 / 自己决定"的参数 | 2.1 |
| MCP（Model Context Protocol） | 把工具从"进程内"搬到"进程外"的统一协议 | 2.2 |
| M×N 集成问题 | M 个应用 × N 个系统 = M×N 份重复适配器 | 2.2 |
| Host（宿主） | 面向用户的宿主应用，拥有 UI 与最终决策权 | 2.2 |
| Client（客户端） | Host 内为**每个** Server 建立的一条连接 | 2.2 |
| Server（服务端） | 暴露能力的一方，只回答"我能提供什么" | 2.2 |
| 原语（primitive） | MCP 的三种能力形态：Tools / Resources / Prompts | 2.2 |
| Resources | 只读数据，按 URI 定位，由 Host 决定挂载 | 2.2 |
| Prompts（模板） | 预置提示模板，由用户主动选择 | 2.2 |
| 协议成本 | 学规范、调握手、维护版本兼容的一次性投入 | 2.2 |
| 信任边界 | 跨组织访问时的隔离要求，往往压倒成本算数 | 2.2 |
| 无状态（stateless） | 请求自带全部上下文，服务端不依赖会话记忆 | 2.3 |
| Modern / Legacy / Dual-era | 逐请求元数据版 / 握手版 / 两者都支持的实现 | 2.3 |
| `_meta` | 请求参数里的元数据区，承载版本、身份、能力、日志级别、trace | 2.3 |
| 显式句柄（server-minted handle） | 服务端签发的 id，替代 session 承载跨调用状态 | 2.3 |
| `resultType` | 结果类型：`complete` / `input_required`；缺失按 `complete` | 2.3 |
| MRTR（多轮往返） | 服务端返回 `input_required`，客户端**重试原请求**补 `inputResponses` | 2.3 |
| `server/discover` | Server 必须实现的能力发现 RPC；Client 可选调用 | 2.3 |
| `UnsupportedProtocolVersionError` | 版本不匹配错误，必须列出支持的版本 | 2.3 |
| `subscriptions/listen` | 取代 HTTP GET 端点与 `resources/subscribe` 的单流订阅；响应即一条长连接流，按类型 opt-in | 2.3 / 2.5 |
| 粘性路由（sticky session） | 把同一客户端固定到同一实例；无状态后可去掉 | 2.3 |
| `tools/list` / `tools/call` | 列出工具 / 调用工具的 RPC 方法 | 2.4 |
| `inputSchema` | 工具参数 schema 在 MCP 里的字段名（对应 function calling 的 `parameters`） | 2.4 |
| `isError` | 工具层失败标记；协议层仍是成功响应 | 2.4 |
| `ttlMs` / `cacheScope` | 列表结果的新鲜度与缓存范围提示 | 2.4 |
| `resources/read` | 按 URI 读取资源内容（只读） | 2.4 |
| `prompts/get` | 按名字与参数渲染提示模板 | 2.4 |
| JSON-RPC 错误码 | `-32700` 解析错、`-32600` 请求非法、`-32601` 方法不存在、`-32602` 参数非法 | 2.4 |
| stdio 传输 | 一行一条 JSON-RPC 的本地传输；stdout 是协议通道 | 2.4 |
| Streamable HTTP | 单端点 `POST /mcp` 承载 JSON-RPC 的传输方式 | 2.5 |
| `Mcp-Method` / `Mcp-Name` | HTTP 路由头，让网关不解析 body 就能路由与计量 | 2.5 |
| 路由欺骗 | 头与 body 方法不一致时，网关可能把调用当列表缓存 | 2.5 |
| `subscriptionId` | 服务端签发的订阅标识，通知靠它归属 | 2.5 |
| opt-in（按类型订阅） | 客户端显式声明要哪些通知；未知类型进 `rejected` | 2.5 |
| 水平扩容 | 多实例 + 普通轮询负载均衡，无需粘性路由 | 2.5 |
| 对账（reconciliation） | 推送只做提示，数据靠拉取 + 定期全量同步 | 2.5 |
| `inputRequests` | MRTR 里服务端索要的信息清单，含 `id`/`prompt`/`schema` | 2.6 |
| `inputResponses` | 客户端重试时回填的答案，按 `id` 对应 | 2.6 |
| `NeedsInput` | 工具内部表示"缺信息"的异常，协议层转成 `input_required` | 2.6 |
| scope | 令牌里声明的权限范围，如 `tools:write` | 2.6 |
| `insufficient_scope` | 403 的错误码：身份清楚但权限不够 | 2.6 |
| 动态 scope | 按工具**参数**追加的权限要求（如建 P0 需 `admin:approve`） | 2.6 |
| Bearer token | `Authorization: Bearer <token>` 形式的承载令牌 | 2.6 |
| `WWW-Authenticate` | 401/403 响应头，告知认证方案与错误码（**必须 ASCII**） | 2.6 |
| 适配器（adapter） | 把 MCP 工具转成框架工具对象的胶水层：发现/转换/代理 | 2.7 |
| 命名空间（namespace） | `mcp__<server>__<tool>`，避免多 Server 同名工具串台 | 2.7 |
| 白名单（whitelist） | 只暴露当前任务需要的工具，控 token 且降低误选 | 2.7 |
| 工具路由（tool routing） | 先按任务挑选工具子集再加载，是白名单的动态版 | 2.7 |
| 工具 schema 预算 | 工具定义占用的 prompt 空间，需可计算、可裁剪（见「上下文 token 预算」） | 2.7 |
| 状态图（StateGraph） | 用 State/Node/Edge 声明控制流，编译成可执行图 | 3.1 |
| State | 图里共享、可逐步补全的状态容器（TypedDict） | 3.1 |
| Node | 纯函数：读 State，返回"要更新的字段" | 3.1 |
| Edge | 显式的执行顺序；分支用条件边（3.3） | 3.1 |
| CompiledStateGraph | `compile()` 的产物：持有节点表/边表/入口的执行器 | 3.1 |
| 增量返回 | Node 只返回自己改的字段，由 LangGraph 合并 | 3.1 |
| `__start__` / `__end__` | LangGraph 自动加的虚拟入口/出口节点 | 3.1 |
| reducer | 字段的合并函数 `(left, right) -> 新值`；不声明则默认覆盖 | 3.2 |
| 默认覆盖 | 后写的值整个替换先前值；对标量字段正确，对列表是灾难 | 3.2 |
| `add_messages` | 消息专用 reducer：追加 + 按 `id` 替换（upsert） | 3.2 |
| upsert | 有则更新、无则插入；`add_messages` 的核心语义 | 3.2 |
| 派生字段 | 能由别的字段算出来的值；应现算而不入库 | 3.2 |
| `Annotated[T, reducer]` | 在 TypedDict 里声明字段 reducer 的写法 | 3.2 |
| 条件边（conditional edge） | router 读 State 返回标签，按映射决定下一站 | 3.3 |
| 自环边（self-loop） | 把边连回自己，等价于"重试"；没有特殊语法 | 3.3 |
| 递归上限（recursion_limit） | 图的最大步数，**实测默认 10007**，超了抛 `GraphRecursionError` | 3.3 |
| 业务刹车 | 存在 State 里的 `max_attempts`，能解释"为什么放弃" | 3.3 |
| 错误边 | 惯例：节点内 try/except 写 `state.error`，条件边导到兜底节点 | 3.3 |
| `Command(goto=...)` | 节点自己决定下一站，可同时 `update` 状态 | 3.3 |
| 兜底节点（fallback） | 任何异常在此收口，保证图永远返回可展示的答案 | 3.3 |
| Checkpointer | 每个节点执行完自动存档的组件 | 3.4 |
| `thread_id` | 会话/任务标识，决定"哪段历史"（存档槽位） | 3.4 |
| `checkpoint_id` | 单个快照的标识，用于时间旅行与重放 | 3.4 |
| `next` | 下一步要执行的节点元组；恢复的起点 | 3.4 |
| `aget_state` / `aget_state_history` | 异步 saver 下的读状态接口（同步版会报错） | 3.4 |
| 时间旅行 | 回看历史快照、从旧快照重放（会形成新分支） | 3.4 |
| 幂等副作用 | 重跑节点时外部写操作不重复发生的能力 | 3.4 |
| `interrupt(payload)` | 把图挂在当前节点，payload 即交给人的审批单 | 3.5 |
| `Command(resume=...)` | 从挂起点继续，值是 `interrupt()` 的返回值 | 3.5 |
| 审批单（payload） | 纯 dict：ticket / draft / risk / expires_at / ask | 3.5 |
| `state.interrupts` | 当前挂起项列表，用于"待办查询" | 3.5 |
| 会签 | 多个角色都批准才放行；本节未实现 | 3.5 |
| 超时扫描器 | 外部定时任务，对过期挂起投递 resume/作废 | 3.5 |
| `astream(stream_mode="updates")` | 节点级流：每个节点跑完产出 1 条增量 | 3.6 |
| `astream_events(v2)` | 细粒度流：节点开始/结束、工具调用、token | 3.6 |
| `ProgressEvent` | 扁平事件模型（at/kind/node/detail/trace_id） | 3.6 |
| 感知延迟 | 用户对"还要等多久"的不确定感；流式解决的是它 | 3.6 |
| 单调时钟 | `perf_counter`，用于算耗时；墙上时钟会倒退 | 3.6 |
| `bind_request_context` | 把 trace_id 绑到 contextvars，日志自动携带 | 3.6 |
| 子图（subgraph） | 编译好的图当作父图的一个节点；state 直接透传 | 3.7 |
| 包装函数接法 | `await subgraph.ainvoke(state)`；可做映射，但会丢掉内部 trace | 3.7 |
| xray 视图 | `get_graph(xray=True)` 展开子图内部，节点名带 `子图:` 前缀 | 3.7 |
| interrupt 冒泡 | 子图内挂起上报为"父节点未完成"，`next` 指向该子图节点 | 3.7 |
| 前置重跑 | 子图内 interrupt 恢复后，父图会重跑前置兄弟节点（实测） | 3.7 |
| 交接（handoff） | Agent 之间把上下文序列化后传递；主要成本所在 | 4.1 |
| supervisor 模式 | 一个调度 Agent 轮流派活给 worker，worker 干完回报 | 4.1 |
| 交接上下文量 | 每次交接序列化的字节数，token 成本的代理指标 | 4.1 |
| 架构先行 | 没量收益就按"业界都在用"拆架构 | 4.1 |
| 判分器自检 | 用「不可能满足的任务」验证评测尺子能判错 | 4.1 |
| A2A（Agent2Agent） | Agent 之间派任务的协议（Linux Foundation，v1.0） | 4.2 |
| Agent Card | A2A 的能力卡片，`/.well-known/agent-card.json` | 4.2 |
| `message/send` | 派活并立刻返回 `taskId`（取代旧名 `tasks/send`） | 4.2 |
| `taskId` | 任务标识；续谈时必须带上，否则会新建任务 | 4.2 |
| `contextId` | 对话标识，跨任务复用于理解上下文 | 4.2 |
| 任务状态机 | submitted / working / input-required / completed / canceled / failed | 4.2 |
| 终态保护 | completed 等终态任务不能再续，防重复交付 | 4.2 |
| artifact | 任务产出（文本/文件/结构化数据） | 4.2 |
| supervisor 拓扑 | worker 干完回中心，由中心派下一跳；轮次约 2 倍 | 4.3 |
| handoff 拓扑 | worker 用 `Command(goto=)` 直接交接，无中心；边表为空 | 4.3 |
| 踢皮球 | handoff 里两个角色互相打回，无人发现循环 | 4.3 |
| 止损（guard） | 在被反复调用的节点里计数并主动结束 | 4.3 |
| 显式阶段（stage） | 用字段记录进度，而不是用「字段是否为空」推断 | 4.3 |
| 短期记忆 | 当前会话的上下文窗口管理与压缩 | 4.4 |
| 上下文 token 预算 | 按 token 而非消息条数决定裁多少；与「工具 schema 预算」是两笔账 | 4.4 |
| window 裁剪 | 丢最旧、留最近 N 条；连约束也不保 | 4.4 |
| summary 压缩 | 把旧消息压成一句；**摘要不等于保真** | 4.4 |
| hybrid 压缩 | 约束钉住 + 中段摘要 + 近处原样 | 4.4 |
| 约束（constraint） | 工单号/审批状态/禁令/必须项；丢了会出事故 | 4.4 |
| pinned 消息 | 压缩时永不丢弃的消息 | 4.4 |
| 长期记忆 | 跨会话保存的用户偏好与历史事件 | 4.5 |
| 语义记忆 | 偏好/身份/长期约束；几乎每次都要带上 | 4.5 |
| 情景记忆 | 某天发生了什么的记录；按相似度召回 | 4.5 |
| 重要性衰减 | `importance × exp(-age/270d)`，越旧权重越低 | 4.5 |
| 新鲜度 | `exp(-age/90d)`，用于情景记忆排序 | 4.5 |
| TTL | 条目存活时长；召回时过滤过期项 | 4.5 |
| 被遗忘权 | 用户级删除（`forget_user`），GDPR 第 17 条 | 4.5 |
| 上下文污染 | 塞进 prompt 的信息本身有错/不该出现 | 4.6 |
| 命名空间隔离 | 按 `user_id`/scope 过滤；`"*"` 表示公共知识 | 4.6 |
| 来源权重 | policy > kb > session > memory > unknown | 4.6 |
| 版本取代 | 同 topic 只保留最高 `version` | 4.6 |
| 冲突显式化 | 矛盾条目都保留并显式提示，而不是替模型选一个 | 4.6 |
| 时效标注 | 给条目加「90 天前」「可能过期」标签 | 4.6 |
| 累计成本 | Σ(每轮上下文大小)，LLM 的真实成本口径 | 4.7 |
| 约束保留率 | 注入的约束最后还剩几条 | 4.7 |
| 成本曲线 | 每轮上下文大小随时间的变化 | 4.7 |
| 压测夹具 | 固定轮数 + 固定语料 + 固定时钟的可复现场景 | 4.7 |
| golden set | 带标注的评测集，工程化的前提 | 5.1 |
| `must_contain` | 答复必须出现的关键词 | 5.1 |
| `must_not_contain` | 绝不能出现的内容（抓「讨好型失败」） | 5.1 |
| 一票否决（hard criteria） | 安全/正确性判据失败即判不通过，不被平均分稀释 | 5.1 |
| Cohen's kappa | 扣掉随机一致后的一致性指标 | 5.1 |
| 期望失败用例 | 设计上就该失败的用例，保证评测集有区分度 | 5.1 |
| dev/test 隔离 | 调参只看 dev，test 只在发布前看 | 5.1 |
| faithfulness | 答复的每句话能否被检索到的资料支持 | 5.2 |
| context_precision | 检索片段中相关片段所占比例 | 5.2 |
| context_recall | 关键事实被检索覆盖的比例 | 5.2 |
| answer_correctness | 答复对参考答案要点的覆盖（替代 RAGAS 的 answer_relevancy） | 5.2 |
| 病根定位 | 按指标组合模式反推「检索错」还是「生成错」 | 5.2 |
| 代理指标 | 无法调模型时用确定性算法替代 LLM 判断；不可与公开分数对比 | 5.2 |
| 行为评测 | 检查工具选择、调用顺序、审批，而不只看答复 | 5.3 |
| 盲写 | 写操作前没有前置查询（如未查资产就建单） | 5.3 |
| 越权调用 | 危险操作未经审批或 scope 不足就执行 | 5.3 |
| 注入失守 | 答复里实际吐出了系统提示或被诱导内容 | 5.3 |
| PII 泄漏 | 输出了他人的手机号/身份证等敏感信息 | 5.3 |
| 安全门禁 | CI 里对安全判据设 100% 阈值，不过不许发布 | 5.3 |
| 假阳性代价 | 把「正确拒绝」判成失败会让团队关掉门禁 | 5.3 |
| 基线快照 | `eval/baseline.json`，定义「合格水平」，进 git 并走 review | 5.4 |
| 指标回退 | 当前值相对基线下跌超过容忍度 | 5.4 |
| 绝对下限（floor） | 与基线无关的硬底线，防「基线一路下探」 | 5.4 |
| 缺失即失败 | 基线有、本次没跑出来的指标按失败处理 | 5.4 |
| 门禁退出码 | 0 通过 / 1 质量不合格 / 2 配置错误 | 5.4 |
| `traceparent` | W3C 头：version-traceid-spanid-flags | 5.5 |
| `trace_id` | 整条链路唯一标识，**跨进程一路不变** | 5.5 |
| `span_id` | 单跳标识，每跳换新；`parent_id` 指回上一跳 | 5.5 |
| 采样标志 | `flags=00` 表示不采样：**透传但不记录** | 5.5 |
| span 树 | 把多个进程的 span 按 parent 还原成调用树 | 5.5 |
| 孤儿 span | 找不到父的 span，按根处理（不丢数据） | 5.5 |
| 护栏（guardrail） | 请求/答复在进出时按规则检查与处置 | 5.6 |
| BLOCK | 整条拦下，返回体面话术 | 5.6 |
| REWRITE | 改写后放行（脱敏、补提示） | 5.6 |
| 误杀率 | 正常请求被拦的比例，护栏的核心成本指标 | 5.6 |
| 请求式标记 | 「帮我/直接/麻烦」等；用于区分指令与提问 | 5.6 |
| 链式改写 | 后一条规则基于前一条的结果，避免覆盖 | 5.6 |
| 熔断（circuit breaker） | 连续失败后**不发起调用**，给下游恢复机会 | 5.7 |
| 三态机 | CLOSED / OPEN / HALF_OPEN | 5.7 |
| 半开探测 | 冷却后只放极少量请求试探下游是否恢复 | 5.7 |
| 令牌桶 | 容量 + 匀速补充的限流算法，平滑且容忍突发 | 5.7 |
| 成本闸 | 按 token 预算拦截请求，跨天重置 | 5.7 |
| 降级链 | 缓存 → 模板 → 兜底话术，按「像原答案」排序 | 5.7 |
| 降级率 | 走降级的请求占比；高说明主路径有故障 | 5.7 |
| 上线检查单 | 发布前必须逐项通过的检查集合 | 5.8 |
| auto / manual 检查 | 代码可判定 vs 必须人工签字 | 5.8 |
| blocker | 不通过就不许上线的检查项 | 5.8 |
| 假检查 | 有名字有输出但不检查任何东西的检查（如读缓存结果） | 5.8 |
| 演练（drill） | 用与上线相同的判定逻辑模拟某项失败 | 5.8 |
