<div align="center">


**Codini：一个小型、可审计、运行现场留在本地的终端编程 Agent。**

让模型在真实代码仓库中检索、修改、执行与复盘，同时把权限边界、上下文、记忆和运行轨迹留在你的掌控之中。

<img src="assets/codini-branding.svg" alt="Codini 魔术师兔子虚拟形象" width="820">


[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Version 0.1.0](https://img.shields.io/badge/version-0.1.0-7C3AED)](#项目状态)
[![Status: early development](https://img.shields.io/badge/status-early%20development-D97706)](#项目状态)
[![License: MIT](https://img.shields.io/badge/license-MIT-16A34A)](LICENSE)

[快速开始](#快速开始) · [核心能力](#核心能力) · [安全边界](#安全边界) · [实时追踪](#实时追踪) · [参与开发](#参与开发)

</div>


## Codini 是什么

Codini 是一个运行在终端里的轻量 Coding Agent。它读取当前 Git 工作区，把仓库信息、会话历史和相关记忆整理成受预算约束的上下文，调用模型决定下一步，再通过一组显式注册的工具完成检索、编辑和验证。

它关注的不是“让模型拥有尽可能多的权限”，而是让一次编程任务具备清楚的执行边界：

- **范围可控**：文件操作被限制在当前 workspace 内，阻止 `../` 路径逃逸。
- **动作可控**：读取与搜索默认安全；写文件、执行 shell 等高风险动作受审批策略约束。
- **状态可恢复**：会话、任务状态、checkpoint、报告与 trace 都落在仓库本地的 `.codini/` 中。
- **过程可观察**：实时 Viewer 面板展示模型尝试、工具调用、审批结果、diff、耗时和 token 使用情况。
- **上下文可解释**：工作区、工作记忆、相关记忆、历史和当前请求动态分配预算，而不是无限堆叠对话。

## 核心能力

| 能力 | Codini 的处理方式 |
| --- | --- |
| 仓库感知 | 启动时读取 Git 分支、工作区状态、最近提交和关键项目文档 |
| 工具执行 | 使用明确的工具白名单完成文件列表、读取、搜索、shell、写入和精确替换 |
| 风险审批 | 通过 `ask`、`auto`、`never` 三种策略控制高风险工具 |
| 会话恢复 | 保存完整会话与任务 checkpoint，可按 session id 或 `latest` 继续 |
| 分层记忆 | 区分 Working Memory、Episodic Memory 与显式晋升的 Durable Memory |
| 动态执行预算 | 有进展时扩展工具步数，重复或持续无进展时提前停止 |
| 只读委派 | 可把有界调查交给只读子 Agent，并在 trace 中保留父子运行关系 |
| Skills | 从 `.codini/skills/` 加载项目内的 Markdown 指令或 `SKILL.md` 包 |
| 实时追踪 | 本地 HTTP viewer 持续轮询运行数据 |
| 敏感信息保护 | trace/report 写入前进行凭据字段与已知 secret 值脱敏 |

## 工作方式

一次请求大致经过以下过程：

1. 刷新工作区快照，并召回与当前请求相关的记忆。
2. 在总预算内组装 prompt；当前请求始终保留，旧历史按需压缩。
3. 模型返回最终答案，或申请一个工具调用。
4. Codini 校验工具、参数、路径、重复调用和审批策略。
5. 执行结果、工作区变化、diff、checkpoint 与 trace 持续落盘。
6. 达成目标、触及硬限制或检测到持续无进展后结束本次 run。

## 快速开始

### 1. 从源码安装

Codini 推荐使用 uv 管理 Python、虚拟环境和项目依赖。请先从 [uv 官方安装页面](https://docs.astral.sh/uv/getting-started/installation/) 下载或安装 uv。

Windows PowerShell：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS / Linux：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装后执行 `uv --version`，确认终端可以找到 uv。然后获取源码并同步项目环境：

```bash
git clone https://github.com/EliaukoaYoW/Codini.git
cd Codini
uv sync --dev
```

`uv sync` 会创建 `.venv`、安装 Codini 并同步依赖，但不会把 `.venv` 永久加入系统 PATH。每次打开新终端后，只需激活一次项目环境：

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
```

macOS / Linux：

```bash
source .venv/bin/activate
```

激活后可以直接运行 `codini`，后续命令不需要添加 `uv run`。`python -m` 只是让 Python 按模块路径执行代码，本身不负责安装依赖；本项目已经提供 `codini` 命令，因此日常使用不需要 `python -m codini`。

### 2. 配置模型

Codini 会自动读取当前目录下的 `.env`（需要手动创建）。下面两种 provider 任选其一。

兼容 OpenAI API 格式的 Provider：

```dotenv
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=https://api.example.com
OPENAI_MODEL=your-model
```

SiliconFlow：

```dotenv
SILICONFLOW_API_KEY=your_api_key
SILICONFLOW_BASE_URL=https://api.siliconflow.cn
SILICONFLOW_MODEL=deepseek-ai/DeepSeek-V4-Flash
```

每个 Provider 的 API key、base URL 和模型名都是必需配置。`--model` 与 `--base-url` 会覆盖对应环境变量；缺少配置时 CLI 会指出具体变量。API base URL 可以带或不带 `/v1`，Codini 会进行规范化。

> [!CAUTION]
> 不要提交 `.env`。本仓库已忽略 `*.env`，但仍建议使用权限受控的本地凭据管理方式，并定期检查 Git 暂存区。

### 3. 启动

在需要处理的仓库中运行：

```bash
codini --cwd . --provider openai
```

也可以直接执行一次性任务：

```bash
codini --cwd . --provider openai "先分析测试失败的根因，只报告，不修改文件"
```

SiliconFlow：

```bash
codini --cwd . --provider siliconflow
```

Codini 默认使用 `--approval ask`，并在 `127.0.0.1:8765` 启动当前 session 的实时 trace viewer。

## 常用操作

### 交互命令

| 命令 | 作用 |
| --- | --- |
| `/help` | 显示帮助 |
| `/context` | 查看当前 prompt 各部分的上下文用量 |
| `/model [name]` | 查看或切换当前 provider 下的模型 |
| `/memory` | 查看当前分层工作记忆摘要 |
| `/session` | 显示当前 session 文件路径 |
| `/trace` | 显示当前实时 viewer 地址 |
| `/skill [name]` | 列出 skills，或读取指定 skill |
| `/reset` | 清空当前 session 的历史与临时记忆 |
| `/exit` | 退出 |

### 恢复会话

恢复最近一次会话：

```bash
codini --cwd . --provider openai --resume latest
```

恢复指定 session：

```bash
codini --cwd . --provider openai --resume 20260716-161054-85535b
```

恢复时 Codini 会检查 checkpoint schema、workspace fingerprint 和关键文件 freshness，并把不匹配状态暴露给 runtime。Checkpoint 用于恢复任务现场，**不是文件系统回滚或 Git 备份**。

### 调整审批策略

```bash
# 默认：每个高风险动作都询问
codini --cwd . --approval ask

# 拒绝所有高风险动作，适合只读分析
codini --cwd . --approval never

# 自动批准所有高风险动作，仅用于可信环境
codini --cwd . --approval auto
```

### 调整执行与追踪

```bash
codini --cwd . \
  --max-steps 8 \
  --max-new-tokens 4096 \
  --trace-port 9000 \
  --trace-poll-ms 1000
```

使用 `--no-trace-live` 可以关闭随 CLI 自动启动的 viewer。

## 安全边界

Codini 的安全模型由多层护栏组成，但它不是完整的系统级沙箱。

| 策略 | 高风险工具行为 | 适合场景 |
| --- | --- | --- |
| `ask` | 每次执行前请求确认 | 日常开发，默认 |
| `never` | 一律拒绝 | 代码审查、只读调查 |
| `auto` | 一律允许 | 外部已隔离且完全可信的环境 |

当前工具边界：

- `list_files`、`read_file`、`search`、`list_skills`、`read_skill` 为低风险工具。
- `run_shell`、`write_file`、`patch_file`、`delegate` 被标记为高风险工具。
- 文件路径会解析到 workspace 内；越界路径被拒绝。
- 传给 shell 的环境变量来自窄白名单，API key、token、secret、password 不会按默认配置透传。
- trace 与 report 会按敏感字段名、已知 secret 值和常见凭据格式进行脱敏。
- 子 Agent 受深度和步数限制，并以只读模式运行。

> [!WARNING]
> `run_shell` 会经过 `--sandbox` 选定的后端。默认 `none` 仍会在宿主环境执行 shell；Linux 上可选择实验性的 `bubblewrap` 后端，默认隔离系统目录和网络。即使启用了审批，也应先阅读待执行命令；处理不受信任仓库时，仍建议使用容器或虚拟机等外部隔离环境。

## 实时追踪

Viewer 是 Codini 的运行面板，而不是一次性导出的报告。它直接读取 session 中持续更新的工件，并通过 `/data` 轮询刷新。

Viewer 随 CLI 自动启动并读取当前 session，不需要单独启动。

Viewer 用于检查：

- run 与子 Agent 的父子执行关系
- 工具调用与审批结果
- task state、checkpoint、stop reason 与最终报告
- prompt/context 预算、token 使用和缓存命中
- Summary、Markdown 输出与原始 JSON


## 记忆架构

Codini 使用轻量、可检查的文件化记忆，不依赖向量数据库。

| 层级 | 用途 | 生命周期 |
| --- | --- | --- |
| Working memory | 当前任务摘要、近期文件和文件短摘要 | 当前 session |
| Episodic memory | `observation`、`decision`、`constraint`、`preference`、`error_resolution` | session / project / file scope |
| Durable memory | 项目约定、关键决策、依赖事实、用户偏好 | 显式晋升后跨 session 保留 |

文件级记忆带 freshness 信息；原文件变化后，旧笔记会被标为 `stale`。被新事实替代的笔记会进入 `superseded`，默认召回只选择仍然 `active` 的内容。

长期记忆要求用户有明确的“记住 / 保存 / 记录”意图，并使用可识别的稳定事实格式，例如：

```text
请记住这条约定。
项目约定：所有 Python 改动在提交前运行 Ruff 和对应的 focused tests。
```

Durable memory 会写入 `.codini/memory/MEMORY.md` 及对应 topic 文件，便于人工审阅和版本外管理。

## Skills

项目级 skill 放在目标仓库的 `.codini/skills/`：

```text
.codini/skills/
├── review-checklist.md
└── release/
    └── SKILL.md
```

进入 Codini 后使用：

```text
/skill
/skill review-checklist
/skill release
```

Skill 文档会作为按需加载的项目指令；它不会自动获得额外工具或绕过审批策略。

## 运行工件

Codini 默认把本地状态放在 workspace 的 `.codini/`，该目录已被本仓库的 `.gitignore` 忽略：

```text
.codini/
├── sessions/<session-id>/
│   ├── session.json
│   ├── task_state.json
│   ├── task_state_history.jsonl
│   ├── trace.jsonl
│   ├── report.json
│   ├── report_history.jsonl
│   └── trace_manifest.json
├── runs/
│   └── index.jsonl
├── memory/
│   ├── MEMORY.md
│   └── topics/
└── skills/
```

`session.json` 面向恢复，`task_state` / `trace` / `report` 面向审计与复盘；两类数据有意分开。

## 项目结构

```text
codini/
├── cli.py                # CLI 装配、交互循环与启动参数
├── runtime.py            # Agent 控制循环、审批、checkpoint 与报告
├── context_manager.py    # 上下文预算、压缩和相关记忆召回
├── memory.py             # Working / Episodic / Durable memory
├── execution_budget.py   # 动态工具步数与无进展检测
├── tools.py              # 工具白名单、校验与执行
├── run_store.py          # Task state、Trace、Report 的持久化
├── models.py             # 模型 Provider 客户端
└── trace/
    ├── trace.py          # Span 与 Trace 事件
    └── viewer.py         # 实时本地 Viewer
```

## CLI 速查

```text
codini [prompt ...]
  --cwd PATH
  --provider {openai,siliconflow}
  --model NAME
  --base-url URL
  --resume SESSION_ID|latest
  --approval {ask,auto,never}
  --max-steps N
  --max-new-tokens N
  --no-trace-live
  --trace-host HOST
  --trace-port PORT
  --trace-poll-ms MS
```

完整参数以本地代码为准：

```bash
codini --help
```

## 参与开发

安装开发依赖：

```bash
uv sync --dev
```

运行检查：

```bash
ruff check codini tests scripts
python tests/test.py
```

`tests/test.py` 是可选择实验的启动器，不是 pytest 测试文件；运行前请先检查其中的 `ACTIVE_EXPERIMENT`，真实 Provider 实验可能产生 API 调用。

提交 Issue 或 PR 时，建议同时说明：

1. 需要解决的具体行为，而不只是期望的实现方式。
2. 改动影响的权限、会话工件或兼容性边界。
3. 实际执行过的验证命令与结果。

[提交 Issue](https://github.com/EliaukoaYoW/Codini/issues) · [创建 Pull Request](https://github.com/EliaukoaYoW/Codini/pulls)


## 项目状态

- 当前版本：`0.1.0`
- Python：`3.10+`
- 已接通 Provider：兼容 OpenAI API 格式的 Provider、SiliconFlow
- 安装方式：uv 源码环境
- 许可证：MIT

当前阶段更适合用于个人仓库、受控实验和 runtime 设计验证。接口、工件 schema 与 CLI 参数仍可能变化；升级前请备份需要保留的 `.codini/` 数据。

## 许可证

本项目采用 [MIT License](LICENSE)。

---

<div align="center">

**Codini：让 coding agent 的每一步都能被看见、理解和约束。**

</div>
