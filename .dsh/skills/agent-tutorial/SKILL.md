---
name: agent-tutorial
description: Use when the user wants to continue, start, redo, check, or jump within the on-disk tutorial 《2026 AI Agent 零基础到生产级开发实战教程》 — phrases like 继续教程, 从 3.4 开始, 下一节, 教程进度, 重写 2.5, 跳到第五章. Reads tutorial/.state/progress.json so the session resumes at the exact section instead of guessing, and writes each section as a real file under tutorial/ with code landed in servicedesk-agent/.
---

# 教程断点续跑

教程的**唯一真相在磁盘**，不在聊天记录里：

- `tutorial/00-index.md` —— 人读目录与进度表
- `tutorial/.state/progress.json` —— 机器读状态（本 skill 的锚点）
- 每章 frontmatter —— 节级明细

**永远不要问"我们讲到哪了"，先跑 `status`。**

## 五步工作流

1. **定位根目录**：`status` 会从 cwd 向上找含 `tutorial/` 的目录，找不到就用 cwd。也可 `--root <dir>` 指定。
2. **读状态**：`status` 给出完成度、下一节、缺失文件告警。没有 `tutorial/` 时先 `init`。
3. **解析目标**：用户给了编号就用它；没给就用 `next`；说"重写"则保留其 `status` 但重新生成正文。
4. **生成或覆盖**：严格按 `references/doc-contract.md` 的命名、frontmatter、10 段式模板写文件。**先落盘，再汇报。**
5. **收尾**：`set <编号> done`（脚本会同步 `00-index.md`），再跑一次 `status` 复核，报告下一节。

## 命令

| 目的 | 命令 |
|---|---|
| 建骨架（幂等，不覆盖已有文件） | `python .dsh/skills/agent-tutorial/scripts/tutorial.py init` |
| 看进度与下一节 | `... status` |
| 摘出某节任务卡 | `... start 3.4` |
| 拿下一节任务卡 | `... next` |
| 更新状态并同步索引 | `... set 3.4 done` |
| 只打印应写入的路径 | `... path 3.4` |
| 一致性检查（有错退出码 1） | `... verify` |

状态四值：`planned → drafted → verified → done`。**没跑过代码不许报 `verified`/`done`。**

### 环境提示（Windows 实测踩过的两个坑）

- `python` 可能是 Microsoft Store 的占位别名（报 `Unable to create process`、退出码 1 且无输出）。改用：
  `uv run --no-project python .dsh/skills/agent-tutorial/scripts/tutorial.py status`
- 中文显示成乱码时，先设 `$env:PYTHONIOENCODING = 'utf-8'` 再运行。数据没坏，只是控制台编码不匹配。

脚本本身只用标准库，不需要装任何依赖。

## 硬性规则

- **一节一次**。不要一口气生成多节来"补齐进度"，那必然摊薄深度。
- **覆盖不新增**：同一节重写就覆盖原文件，绝不产生 `-v2.md`；重写前先读旧文件，保留仍成立的部分。
- **代码是真文件**：代码写进 `servicedesk-agent/`，章节里引用路径；不要只在 md 里贴片段。
- **先落盘再汇报**：对话里只给变更清单、3 行摘要、验收命令、下一节建议。用户明确要求才内联正文。
- **前置缺失不拒绝**：目标节的前置未 `done` 时，先给 3 行「前置补丁」，再讲本节。
- **状态机诚实**：只写了正文 → `drafted`；跑通并粘回真实输出 → `verified`；过 16 项自检 → `done`。
- **进度是提交点**：`set` 必须在一节真正完成之后执行，顺序错了会造成"文档说完成、文件不存在"。

## 每节的 10 段式

0 导航 · 1 痛点先行 · 2 概念与架构（类比+图+失效边界）· 3 环境与版本 · 4 完整代码 · 5 逐块拆解 · 6 故意改坏它 · 7 验证与预期输出 · 8 生产踩坑（现象→定位→根因→修法）· 9 自检与作业 · 10 小结与延伸。

## 出错时

- `未知小节` → 编号必须与 `assets/toc.json` 一致；先 `status` 看合法编号。
- `文件缺失` 告警 → 状态被提前置为 `verified/done`，把该节 `set` 回 `drafted` 再补写。
- `frontmatter 与 progress 不一致` → 以 `progress.json` 为准，改 frontmatter；不要反过来改状态迁就文件。
- 想新增一节 → 先改 `assets/toc.json`（唯一来源），再 `init` 补齐目录，最后 `set`。

## 读下一份

需要命名细则、frontmatter 字段、修订记录格式时读 `references/doc-contract.md`。不要凭记忆猜格式。
