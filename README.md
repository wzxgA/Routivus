# Routivus

**纯后端** Python Agent 服务：只保留 Agent 核心逻辑与编排能力，**不含任何终端界面**，前端客户端由使用方自行接入。

保留能力：ReAct 直接执行、`/plan` 计划模式、`/team` Multi-Agent 协作，内置文件读写、代码搜索、命令执行与只读联网工具，附带 SmartRouter 智能路由与训练能力（针对不同复杂度任务自动切换四档模型）。

## 定位

- **纯后端**：可 `import routivus`，通过 `routivus.service` 程序化入口驱动执行
- **无界面**：不含 Textual/TUI、inline REPL、终端安装脚本
- **Web Console 服务层**：提供项目、会话、消息、笔记 REST API 和 Agent WebSocket 会话流

## 安装

要求：Python 3.11+。

```bash
cd routivus
uv sync          # 或 pip install -e .
```

> 依赖不含 `textual`（已随 TUI 移除）；`onnxruntime`（语义精判）按需启用。仅语义模型离线导出那套大件（torch 等）仍在可选依赖中，日常使用不需要。

## Web Console Server

启动本地 Server：

```bash
python -m routivus.server
```

默认监听 `127.0.0.1:18765`，健康检查地址为 `GET /healthz`。当前阶段提供项目注册表和项目 CRUD；注册、更新、删除只修改项目元数据，不会删除或改写项目目录。

默认只允许注册 Server 启动目录下的项目。需要管理其他工作区时配置：

```bash
ROUTIVUS_WORKSPACE_ROOTS=D:\\DevProject;D:\\Work
ROUTIVUS_PROJECTS_FILE=~/.routivus/projects.json
ROUTIVUS_DATABASE_PATH=~/.routivus/workspace.sqlite3
ROUTIVUS_SERVER_HOST=127.0.0.1
ROUTIVUS_SERVER_PORT=18765
ROUTIVUS_ALLOWED_HOSTS=localhost,127.0.0.1
ROUTIVUS_ALLOWED_ORIGINS=http://localhost:5173
ROUTIVUS_SERVER_TOKEN=
ROUTIVUS_WS_HEARTBEAT_INTERVAL=30
ROUTIVUS_WS_MAX_MESSAGE_BYTES=1048576
```

项目接口：`GET/POST /api/projects`、`GET/PATCH/DELETE /api/projects/{project_id}`。会话、消息和笔记分别通过 `/api/projects/{project_id}/sessions`、`/api/sessions/{session_id}/messages`、`/api/notes` 访问；`/api/notes?scope=global` 返回工作区全部笔记，而项目笔记入口只返回当前项目笔记。实时会话地址为 `/api/ws/projects/{project_id}/sessions/{session_id}`，支持 `request_id` 幂等、事件序号、断线后的 SQLite 事件恢复和心跳；配置 `ROUTIVUS_SERVER_TOKEN` 后需要 Bearer Token。错误统一返回 `error.code`、`error.message`、`error.request_id`，每个响应也带 `X-Request-ID`。

## 程序化入口

`routivus.service.commands.handle_service_command(agent, settings, manager, raw)` 对所有斜杠命令做、UI 无关的分发，返回 `(output_message, should_exit)`，供前端直接渲染：

```python
from routivus.service.commands import handle_service_command

message, should_exit = handle_service_command(agent, settings, manager, "/model")
print(message)
```

## 配置 Provider

所有 provider 配置（定义、URL、API Key、模型列表）**统一写入 `config.json`**，由后端 `/provider` 命令完成，无需手改文件、无需 `.env`。

```bash
/provider add myproxy https://gateway.my.com/v1 --model deepseek-v4 --key sk_x --set-base
```

`--set-base` 把它设为 base provider。之后 `/provider key <name> <KEY>` 可单独写 Key。

| 命令 | 行为 |
|------|------|
| `/provider` 或 `list` | 列出所有 provider（默认模型、是否 base、来源层） |
| `/provider add <name> <api_base> [--model M] [--label L] [--key K] [--set-base]` | 新增 provider |
| `/provider show <name>` | 查看单个 provider（Key 脱敏） |
| `/provider set <name> <field> <value>` | 修改 api_base / default_model / display_name |
| `/provider switch <name> [model]` | 切换 base provider（立即生效） |
| `/provider key <name> <KEY> [--yes]` | 写入/覆盖 API Key 到 config.json |
| `/provider remove <name> [--yes]` | 删除 provider（base 不可删） |
| `/provider <name> model <model>` | 给该 provider 添加模型到列表 |
| `/provider <name> model rm <model>` | 从该 provider 移除模型 |
| `/model` 或 `list` | 查看当前模型与可用 provider |
| `/model <model-name>` | 在当前 base provider 内切换模型 |

配置最终生成为类似下方的 `~/.routivus/config.json`：

```json
{
  "active_provider": "myproxy",
  "active_model": "deepseek-v4-flash-0731",
  "providers": {
    "myproxy": {
      "api_base": "https://gateway.my.com/v1",
      "default_model": "deepseek-v4-flash-0731",
      "api_key": "sk_x",
      "models": ["deepseek-v4-flash-0731", "deepseek-v4-pro-0813"]
    }
  },
  "ui_language": "zh"
}
```

未配置 provider 也可启动（会提示），随后再通过 `/provider` 与 `/model` 补全即可。API Key 显示时一律脱敏。

## SmartRouter 智能路由

SmartRouter 按任务复杂度动态选择四档模型（Basic / Enhanced / Superior / Ultimate），档位的 provider/model 通过 `/tier` 配置，总开关用 `/smartRouter`：

| 命令 | 行为 |
|------|------|
| `/smartRouter status` | 查看路由状态、四档配置、ML 精判与语义通道指标 |
| `/smartRouter on` / `off` | 开启 / 关闭智能路由 |
| `/smartRouter reset` | 重建共享路由状态（重载 ML 模型等） |
| `/tier list` | 列出四档 provider/model（未配回落主动 active） |
| `/tier show <tier>` | 查看单个档位 |
| `/tier set <tier> <provider> [model]` | 设置档位 provider/model（缺省 model 用该 provider 的 default_model） |
| `/tier clear <tier>` | 清空档位，回落到手动 active |
| `/train [labeled.jsonl] [--yes] [--no-semantic]` | 手动训练 ML 精判模型（需确认） |

- **ML 精判**：默认走 TF-IDF + LightGBM 离线训练，产物写入 `~/.routivus/adaptive/router.lgb`；训练带语义列可选。用户训练的 `router.lgb` 存在时优先使用，否则回落内置通用模型。
- **语义通道**：内存 + 任务特征打分命中一定触发条件后，用 BGE 语义编码器（`router_semantics.onnx`，512 维）增强 ML 路由精度；编码器缺失时静默回落 TF-IDF-only。可通过 `/smartRouter status` 观察语义编码次数与耗时。
- 语义编码器需用 `tools/export_bge_onnx.py` 离线导出，模型文件不随包分发。

## 命令总览

后端斜杠命令（统一经 `handle_service_command` 分发）：

| 命令 | 说明 |
|------|------|
| `/plan <任务>` | 计划模式：先拆解为子任务 DAG，审阅后按轮执行（见下） |
| `/team <任务>` | Multi-Agent 模式：Supervisor 调度隔离 Worker，审查证据并定向修复（见下） |
| `/provider` | 管理服务商（增删改查、切 base、写 Key、维护模型列表，见「配置 Provider」） |
| `/model` | 查看当前模型，或在当前 provider 内切换模型（见「配置 Provider」） |
| `/smartRouter` | 智能路由总开关与状态（on / off / status / reset） |
| `/tier` | 配置四档模型（list / show / set / clear） |
| `/train` | 手动训练 ML 精判模型（需确认） |
| `/init` | 分析当前项目，预览并生成 `Routivus.md` 项目记忆（已有文件不覆盖） |
| `/save <内容>` | 显式保存一条当前项目长期记忆 |
| `/memory` | 管理当前项目的长期记忆（list / search / delete / clear） |
| `/config` | 显示当前生效配置（Key 脱敏）；`list` provider 能力表；`get <key>` / `set <key> <value>` 查改配置项 |
| `/mcp` | 管理 MCP Server（status / restart / logs / enable / disable / resources） |
| `/web` | 只读联网能力（status / providers / search / fetch） |
| `/skill` | 管理任务 Skill（list / load / enable / disable） |
| `/history` | 输入历史（status / clear） |
| `/hitl [on/off]` | 查看 / 开启 / 关闭危险操作审批 |
| `/clear` | 清空当前对话上下文 |
| `/exit` | 请求结束会话（返回 `should_exit=True`，由前端执行关闭） |

## 计划模式

ReAct 之外的第二条执行路径。`/plan <任务>` 把多步任务先拆解为「子任务 + 依赖图」，经审阅后按依赖轮次执行：

1. **拆解**：LLM 独立调用生成结构化 JSON（子任务 + 依赖），自动校验与修复（JSON 解析失败带错误重试上限 2 次；未知依赖/自依赖自动移除；环检测；超上限截断）
2. **轮次生成**：Kahn 拓扑排序产出依赖轮次，无依赖子任务同轮并行
3. **审阅**：前端展示计划供决策——执行 / 查看详情 / 追加要求重规划 / 取消（不执行任何工具）
4. **执行**：子任务以独立迷你 ReAct 循环执行（步数上限默认 10），依赖结果摘要注入下游上下文；子任务失败时错误注入依赖方让其自行调整，失败数超过上限（默认 3）终止剩余轮次
5. **汇总**：`plan_done` / `plan_failed` 展示各子任务状态与结果

子任务执行复用全部安全机制：并行工具、HITL 审批、策略层黑名单/路径越界拒绝、审计（含 `subtask_started` / `subtask_done` 事件）。

## Multi-Agent Team 模式

`/team <任务>` 用于复杂任务。Planner 生成带角色、依赖、资源范围和验收标准的 DAG；用户确认后，Supervisor 调度隔离上下文的 Coder、Tester 等 Worker。Worker 产生的工具结果和最终报告会收集为进程内 Artifact，供 Reviewer 检查工具结果与验证证据；Artifact 不是文件快照，完整 diff/snapshot 审查属于后续增强。

审查失败时只生成针对问题的 Repair 任务，默认最多修复 2 次；存在资源范围冲突的任务会自动串行化。所有 Worker 仍然经过统一的 HITL、PathGuard、CommandGuard、ToolRegistry 和 Audit 链路。简单任务不需要使用 `/team`，`/plan` 的行为保持兼容。

## 安全机制

- **并行执行**：模型一轮返回多个工具调用时并行执行（默认 4 并发），结果按原始顺序回灌
- **HITL 审批**：危险操作（默认 `execute_command` 必审、`write_file` 确认）执行前触发审批，由前端提交决定（批准 / 本会话全部放行 / 拒绝 / 改参后执行）
- **策略层**：路径越界（PathGuard，含 symlink 逃逸）与黑名单命令（CommandGuard）直接拒绝，**不可被审批绕过**
- **审计日志**：所有工具调用/审批/拒绝记录到 `.routivus/audit.log`（JSONL，敏感字段脱敏）

内置工具：`read_file` / `write_file` / `list_dir` / `glob_files` / `grep_code` / `execute_command` / `web_search` / `web_fetch` / `load_skill`（按配置启用）。

## Web 只读联网能力

提供 `web_search` 和 `web_fetch` 两个异步内置工具。搜索支持智谱、SerpAPI、SearXNG 三种 provider；抓取只允许公开 HTTP(S) 网页，逐跳校验 DNS 和重定向，拒绝 localhost、内网/保留 IP、非文本资源、超大响应和登录/动态页面。网页内容会标记为外部不可信资料，不会获得新的工具权限。

默认不启用搜索 provider，后端仍可工作；抓取不依赖搜索配置。可通过环境变量或 `.routivus/web.json` 配置，常用变量见 `.env.example`。使用 `/web status`、`/web providers`、`/web search <query>` 和 `/web fetch <url>`。

## Skill 技能系统

Skill 是可发现、按需加载的本地任务规范，不是脚本、插件或新的执行权限。后端启动时只扫描 `SKILL.md` 的名称和描述并注入有限索引；Agent 或用户执行 `/skill load <name>` 后，才读取正文和明确指定的 `references/` 文件。Skill 中的文字仍是补充资料，不能覆盖系统提示、安全策略、HITL 或工具权限。

Skill 目录按优先级从低到高合并：内置 `routivus/skills/`、用户级 `~/.routivus/skills/`、项目级 `<project>/.routivus/skills/`。同名 Skill 由高层完整覆盖。用户/项目启用状态保存在对应层的 `skills.json`，常用命令为 `/skill list`、`/skill load <name>`、`/skill enable <name>` 和 `/skill disable <name>`。默认的索引、正文和 reference 大小限制见 `.env.example`；`ROUTIVUS_SKILLS_ENABLED=off` 时不会注册 `load_skill`，其他工具仍可用。

## 输入历史

输入历史（用户已提交的输入）默认按项目隔离保存在用户目录 `~/.routivus/input-history/`，敏感输入（如 API Key、密码、Bearer token、`/save` 和 `/config set`）不会写入磁盘。前端可就地读取历史供上下导航；`.env` 中的 `ROUTIVUS_INPUT_HISTORY_*` 变量控制持久化、条目数量与大小限制。可用 `/history status` 与 `/history clear` 管理。

## MCP 外部能力

Routivus 可以通过 MCP 接入外部工具和 resources，支持本地 `stdio` 子进程与 `Streamable HTTP`。用户级配置位于 `~/.routivus/mcp.json`，项目级配置位于 `.routivus/mcp.json`；同名 Server 由项目配置覆盖，敏感值使用 `${VAR}` 从环境变量或 `.env` 展开。

```json
{
  "servers": {
    "local_docs": {
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "my_docs_mcp"],
      "env": {"DOCS_TOKEN": "${DOCS_TOKEN}"}
    },
    "remote": {
      "transport": "streamable_http",
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer ${MCP_TOKEN}"}
    }
  }
}
```

Server 工具会动态注册为 `mcp__{server}__{tool}`，默认经过 HITL 确认并写入 `.routivus/audit.log`。resources 可由 Agent 通过虚拟 list/read 工具读取，也可以在输入中显式引用：

```text
根据 @local_docs:file:///specs/api.md 检查当前实现
```

常用管理命令：`/mcp status`、`/mcp restart <server>`、`/mcp logs <server>`、`/mcp enable <server>`、`/mcp disable <server>`、`/mcp resources [server]`。MCP Server 是外部代码/服务，只应启用可信配置；不要把真实 token 直接写入 `mcp.json`。

## 记忆与上下文

- 项目根目录的 `Routivus.md`（共享）和 `Routivus.local.md`（本地可选）会自动注入每次任务；运行中修改后下一次顶层任务自动热加载。
- `/save <内容>` 将用户明确提供的内容保存到项目 `.routivus/memory.db`，不会自动保存普通聊天；`/clear` 不会清除长期记忆。
- 长对话接近上下文预算时会自动压缩较旧的完整对话轮次，保留最近轮次和工具调用关系；无法安全压缩时才停止并提示。
- `.routivus/memory.db` 是本地明文数据库，项目记忆会发送给当前 LLM provider。不要在 `Routivus.md` 或 `/save` 中放置 API Key、密码等敏感信息。

## 配置项

provider 与 SmartRouter 配置统一存于 `config.json`（见「配置 Provider」与「SmartRouter」章节），对应字段为 `active_provider` / `active_model` / `providers` / `smart_router` / `tier` / `ui_language`。

其余可用环境变量（均为可选进阶项，来自 `.env` / `.env.example`）：

| 环境变量 | 说明 |
|----------|------|
| `ROUTIVUS_CONTEXT_WINDOW` | 上下文窗口（token），覆盖 provider 能力声明 |
| `ROUTIVUS_CONTEXT_BUDGET_RATIO` | 自动压缩前的输入预算比例（默认 0.8，限制 0.5~0.9） |
| `ROUTIVUS_CONTEXT_KEEP_RECENT_TURNS` | 自动压缩保留的最近完整对话轮次（默认 4） |
| `ROUTIVUS_CONTEXT_SUMMARY_MAX_TOKENS` | 摘要输出动态预留上限（默认 4096） |
| `ROUTIVUS_MEMORY_PROMPT_MAX_CHARS` | 自动注入长期记忆的字符上限（默认 8000） |
| `ROUTIVUS_PROJECT_MEMORY_MAX_CHARS` | 单个项目记忆文件读取上限（默认 32000） |
| `ROUTIVUS_TOOL_STEPS` | 单轮工具调用步数上限（默认 20） |
| `ROUTIVUS_LLM_RETRY_ENABLED` | LLM 临时故障自动重试开关（on 默认） |
| `ROUTIVUS_LLM_MAX_RETRIES` | 单次 LLM 请求最大重试次数（默认 2） |
| `ROUTIVUS_LLM_RETRY_BASE_DELAY` | LLM 重试基础退避秒数（默认 1） |
| `ROUTIVUS_LLM_RETRY_MAX_DELAY` | LLM 重试最大单次等待秒数（默认 8） |
| `ROUTIVUS_LLM_RETRY_JITTER` | LLM 重试随机抖动比例（默认 0.25） |
| `ROUTIVUS_LLM_RETRY_TOTAL_TIMEOUT` | 单次 LLM 请求重试总等待上限秒数（默认 30） |
| `ROUTIVUS_LLM_RESPECT_RETRY_AFTER` | 是否遵循服务端 Retry-After（on 默认） |
| `ROUTIVUS_MAX_PARALLEL` | 并行工具执行并发数（默认 4） |
| `ROUTIVUS_TOOL_TIMEOUT` | 单工具执行超时秒数（默认 120） |
| `ROUTIVUS_HITL` | 危险操作审批开关（on 默认 / off 危险模式） |
| `ROUTIVUS_PLAN_MAX_SUBTASKS` | 计划模式子任务数上限（默认 12，超出截断） |
| `ROUTIVUS_PLAN_SUBTASK_STEPS` | 计划模式单个子任务最大工具步数（默认 10） |
| `ROUTIVUS_PLAN_MAX_FAILURES` | 计划级允许失败数（默认 3，超出终止剩余轮次） |
| `ROUTIVUS_TEAM_MAX_AGENTS` | `/team` 同时运行的 Agent 数量上限（默认 4） |
| `ROUTIVUS_TEAM_MAX_REPAIRS` | `/team` 单个任务的定向修复次数上限（默认 2） |
| `ROUTIVUS_TEAM_RESEARCHER_STEPS` | `/team` researcher 步数覆盖值（默认使用角色值 20） |
| `ROUTIVUS_TEAM_REVIEWER_STEPS` | `/team` reviewer 步数覆盖值（默认使用角色值 10） |
| `ROUTIVUS_TEAM_CODER_STEPS` | `/team` coder 步数覆盖值（默认使用角色值 12） |
| `ROUTIVUS_TEAM_TESTER_STEPS` | `/team` tester 步数覆盖值（默认使用角色值 12） |
| `ROUTIVUS_TEAM_REPAIRER_STEPS` | `/team` repairer 步数覆盖值（默认使用角色值 12） |
| `ROUTIVUS_TEAM_SYNTHESIZER_STEPS` | `/team` synthesizer 步数覆盖值（默认使用角色值 8） |
| `ROUTIVUS_TEAM_MAX_STEPS` | `/team` 单个 Agent 的步数硬上限（默认 40） |
| `ROUTIVUS_TEAM_RECOVERY_STEPS` | `/team` 只读任务恢复执行的步数（默认 10） |
| `ROUTIVUS_TEAM_MAX_RECOVERIES` | `/team` 只读任务自动恢复次数（默认 1） |
| `ROUTIVUS_TEAM_REVIEW` | `/team` 任务级证据审查开关（on 默认） |
| `ROUTIVUS_MCP_ENABLED` | MCP 总开关（on 默认） |
| `ROUTIVUS_MCP_STARTUP_TIMEOUT` | MCP Server 初始化超时秒数（默认 15） |
| `ROUTIVUS_MCP_REQUEST_TIMEOUT` | MCP 单请求超时秒数（默认 120） |
| `ROUTIVUS_MCP_MAX_SERVERS` | MCP Server 数量上限（默认 32） |
| `ROUTIVUS_MCP_MAX_TOOLS` | 每个 Server 工具数上限（默认 256） |
| `ROUTIVUS_MCP_MAX_RESOURCES` | 每个 Server resource 数上限（默认 512） |
| `ROUTIVUS_MCP_RESOURCE_MAX_CHARS` | 单 resource 文本上限（默认 32000） |
| `ROUTIVUS_WEB_ENABLED` | Web 工具总开关（默认 on） |
| `ROUTIVUS_WEB_SEARCH_PROVIDER` | 搜索 provider：none / zhipu / serpapi / searxng |
| `ROUTIVUS_WEB_TIMEOUT` | 搜索/抓取超时秒数（默认 15） |
| `ROUTIVUS_WEB_MAX_RESPONSE_BYTES` | 单网页响应字节上限（默认 2 MiB） |
| `ROUTIVUS_WEB_FETCH_MAX_CHARS` | 单网页正文字符上限（默认 32000） |
| `ROUTIVUS_WEB_MAX_REDIRECTS` | 最大重定向次数（默认 5） |
| `ROUTIVUS_WEB_RATE_LIMIT_PER_MINUTE` | 每类 Web 调用每分钟上限（默认 30） |
| `ROUTIVUS_SKILLS_ENABLED` | Skill 总开关（默认 on） |
| `ROUTIVUS_SKILLS_MAX_INDEX_ITEMS` | system prompt 最多展示的 Skill 数（默认 20） |
| `ROUTIVUS_SKILLS_MAX_INDEX_CHARS` | Skill 索引字符上限（默认 4096） |
| `ROUTIVUS_SKILLS_MAX_CHARS` | 单个 Skill 正文字符上限（默认 32000） |
| `ROUTIVUS_SKILLS_MAX_REFERENCE_CHARS` | 单个 reference 字符上限（默认 16000） |
| `ROUTIVUS_SKILLS_MAX_LOADED_CHARS` | 单次 Skill 加载总字符上限（默认 64000） |
| `ROUTIVUS_INPUT_HISTORY_ENABLED` | 输入历史总开关（默认 on） |
| `ROUTIVUS_INPUT_HISTORY_PERSIST` | 是否持久化非敏感输入历史（默认 on） |
| `ROUTIVUS_INPUT_HISTORY_MAX_ENTRIES` | 每个项目保留的历史条数（默认 100） |
| `ROUTIVUS_INPUT_HISTORY_MAX_CHARS` | 单条历史输入字符上限（默认 8000） |
| `ROUTIVUS_INPUT_HISTORY_MAX_BYTES` | 单项目历史文件字节上限（默认 1 MiB） |

provider 的 API Key 存入 `config.json`，`/provider show`、`/config` 显示时脱敏。不再从环境变量读取 provider Key。

## 开发

```bash
uv run pytest -m "not slow"   # 常规回归
uv run pytest                 # 全量测试
```

项目分层：`routivus/agent`（ReAct 循环 + 计划模式）、`routivus/llm`（客户端抽象 + OpenAI 兼容实现 + 工厂）、`routivus/tool`（统一工具注册表 + 内置工具）、`routivus/mcp`（协议、transport、动态工具和 resources）、`routivus/skill`（Skill 发现、解析、按需加载与安全策略）、`routivus/input_history`（输入历史、游标、持久化与隐私策略）、`routivus/memory`（项目/长期记忆 + 上下文压缩）、`routivus/tui`（纯逻辑编排：state / reducer / controller / i18n，无界面）、`routivus/cli`（命令服务层，无 REPL）、`routivus/service`（UI 无关的程序化命令入口）、`routivus/config`（provider/MCP/Web/Skill 配置与运行时快照）、`routivus/router`（SmartRouter 路由、校准与训练）。
