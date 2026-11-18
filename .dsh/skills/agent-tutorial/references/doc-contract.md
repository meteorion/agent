# 文档落地契约（doc-contract）

本文件是 `agent-tutorial` skill 的格式细则。只在需要确认命名、frontmatter 或交付顺序时读它。

## 1. 目录布局

```
<仓库根>/
├── tutorial/
│   ├── 00-index.md                  # 人读目录 + 进度表（标记区内由脚本同步）
│   ├── 01-foundation/               # 阶段一
│   ├── 02-protocol/
│   ├── 03-graph/
│   ├── 04-multi-agent/
│   ├── 05-eval-ops/
│   ├── appendix/
│   │   ├── glossary.md              # 术语表
│   │   ├── errors.md                # 报错对照表
│   │   └── versions.md              # 版本矩阵
│   └── .state/progress.json         # 机器读进度（提交点）
├── servicedesk-agent/               # 主线代码仓库（章节代码的真身）
└── .dsh/skills/agent-tutorial/      # 本 skill
```

`assets/toc.json` 是**唯一的小节清单来源**。增删小节改它，脚本与索引都从它派生。

## 2. 命名规则（不可变）

| 对象 | 规则 | 例 |
|---|---|---|
| 阶段目录 | `0<阶段号>-<英文 slug>` | `03-graph` |
| 章节文件 | `0<阶段>-0<节>-<英文 kebab slug>.md` | `03-04-checkpointer-resume.md` |
| 编号 | 与 `toc.json` 严格一致，**永不重排** | `3.4` |
| slug | 小写英文 + 连字符，不用中文、不用空格 | `checkpointer-resume` |

**编号重排是禁止操作**：它会让所有历史链接、进度记录、读者笔记同时失效。新增走 `3.7b`。

## 3. 章节 frontmatter

```yaml
---
section: "3.4"
title: "用 Checkpointer 实现断点续跑"
stage: 3
status: drafted          # planned | drafted | verified | done
depth: L2                # L1 | L2 | L3
difficulty: 4
minutes: 100
milestone: M3
depends_on: ["3.3"]
produces: ["src/servicedesk/graph/checkpoint.py"]
verifies: "杀进程后重启，能从 3.3 中途节点继续"
part: null               # 拆上下时填 1 或 2
updated: "2026-11-17"
---
```

字段必须与 `assets/toc.json` 对应条目一致；`status`、`depth`、`updated`、`part` 是唯一允许由章节文件自行偏离的字段。

## 4. 状态语义

| 状态 | 判定 | 允许的下一步 |
|---|---|---|
| `planned` | 只有标题，无正文 | 写正文 |
| `drafted` | 正文完整，但代码未跑通 / 验收未验 | 去跑代码、补真实输出 |
| `verified` | 代码跑通，**真实输出已粘回 §7** | 过自检门禁 |
| `done` | 通过 16 项自检门禁 | `set done` |

**绝不跳级。** 未跑过代码就标 `done`，等于伪造交付物，属于本 skill 最严重的失败。

## 5. 交付顺序

1. 代码写成真实文件 → `servicedesk-agent/`
2. 跑通，捕获真实输出
3. 写章节 md（frontmatter 先置 `drafted`）
4. 更新 `appendix/glossary.md`、`errors.md`、`versions.md`
5. 过 16 项自检 → `verified` → `done`
6. `set <编号> done` —— 此刻才同步 `progress.json` 与 `00-index.md`
7. 回读刚写的文件，确认 frontmatter 与正文编号一致
8. 对话里只报：变更文件清单 + 3 行摘要 + 验收命令 + 下一节

第 6 步之前的一切失败，都视作本节未完成。

## 6. 幂等与修订

- 覆盖式重写，禁止 `-v2.md`。
- 重写前必读旧文件，做 diff 式修改。
- 文末保留「修订记录」区，每次覆盖追加一行：

```markdown
## 修订记录
- 2026-11-17 初稿
- 2026-11-20 修正 Checkpointer 选型表；补 §8 坑 2
```

- 只改某一段时用最小编辑，不重写全文。

## 7. 10 段式骨架

```
0 本节导航      位置 / 你将获得 / 前置检查 / 难度·时长·费用
1 痛点先行      没有它会出错的真实场景
2 概念与架构    类比（+失效边界）/ ASCII 或 Mermaid 图 / 逐层拆解 / 边界
3 环境与版本    安装命令 / 版本组合 / .env.example / 验证命令+预期输出
4 代码实战      文件路径注释 / 零省略 / 完整可运行
5 逐块拆解      解决什么问题 → 关键行 → 改成别的会怎样
6 故意改坏它    1–2 个错误变体，让读者亲手跑出错误
7 验证与输出    命令 + 真实输出 + pytest 断言
8 生产踩坑      至少 2 个，四段式：现象→定位→根因→修法（含 diff）
9 自检与作业    3 问（折叠答案）+ 1 个动手任务 + 1 个拓展挑战
10 小结与延伸   3 行回顾 + 2–3 条官方链接
```

## 8. 索引标记区

`00-index.md` 里这两行之间的表格由脚本生成，**手改会被覆盖**：

```
<!-- PROGRESS:START -->
...由 set 命令重写的表格...
<!-- PROGRESS:END -->
```

标记区之外的内容（阶段范围、能力矩阵、学习路线）由生成模型维护。
