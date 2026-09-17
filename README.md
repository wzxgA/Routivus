# Routivus

**纯后端** Python Agent 服务：只保留 Agent 核心逻辑与编排能力，**Python 包本身不含终端界面**；仓库内另附可选的 Web Console 前端（`frontend/`，Vite + React + TypeScript），也可由使用方自行接入其他客户端。

保留能力：ReAct 直接执行、`/plan` 计划模式、`/team` Multi-Agent 协作，内置文件读写、代码搜索、命令执行与只读联网工具，附带 SmartRouter 智能路由与训练能力（针对不同复杂度任务自动切换四档模型）。

## 定位

- **纯后端**：可 `import routivus`，通过 `routivus.service` 程序化入口驱动执行
- **无界面**：不含 Textual/TUI、inline REPL、终端安装脚本
- **Web Console 服务层**：提供项目、会话、消息、笔记 REST API、Agent WebSocket 会话流、HITL 审批闭环和项目 cwd 绑定的终端通道

## 安装

要求：**Python 3.11+**；使用 Web Console 前端还需要 **Node 18+**（只用后端可以不装 Node）。

```bash
cd Routivus                          # 仓库根目录（Routivus/），不是 routivus/ 子目录
uv sync                              # 或 pip install -e .；Windows 上 pywinpty（终端 ConPTY 后端）随之装好

cd frontend && npm install           # 可选：Web Console 前端
```

> 依赖不含 `textual`（已随 TUI 移除）。SmartRouter 的**运行时**依赖（`numpy` / `scikit-learn` / `lightgbm` / `joblib` / `onnxruntime` / `tokenizers`）、Windows 的 `pywinpty`（终端通道的 ConPTY 后端）与 `websockets`（**对话通道的 WebSocket 实现**，uvicorn 本身只带 http 栈；缺了它每个升级请求都被拒，前端会停在「重连中」）均已并入核心依赖，装完即可用；只有**离线重导出**语义模型那套大件（`optimum` / `transformers` / `torch` / `safetensors`，即 `routivus[semantic]`）仍属可选，日常使用不需要。

## 快速开始

从零到可用界面共 5 步：配置 provider → 启动后端 → 启动前端 → 自检 → 停止。示例为 PowerShell；cmd 把 `$env:X='v'` 换成 `set X=v`。

### 1. 配置 provider（不配就无法执行 Agent 轮次）

后端**没有 REPL，也没有 CLI 脚本入口**（`[project.scripts]` 为空），所以斜杠命令要用 `routivus.cli.commands` 的确定性入口执行：

```powershell
cd Routivus
python -c "from routivus.config.manager import ConfigManager; from routivus.cli.commands import execute_provider_command; print(execute_provider_command(ConfigManager(), None, '/provider add myproxy https://gateway.example.com/v1 --model deepseek-v4-pro-0813 --key sk-xxx --set-base')[0])"
```

配置写入 `~/.routivus/config.json`，**API Key 是明文存储**，不要提交或外传。校验（应列出 provider，Key 脱敏）：

```powershell
python -c "from routivus.config.manager import ConfigManager; from routivus.cli.commands import execute_provider_command; print(execute_provider_command(ConfigManager(), None, '/provider')[0])"
```

不配置 provider 也能启动服务、浏览项目与笔记，但会话里发消息会立刻返回 `agent_error`：`Provider 未配置，无法启动 Agent`。

### 2. 启动后端

```powershell
$env:ROUTIVUS_WORKSPACE_ROOTS='D:\DevProject'          # 允许注册的项目根目录；不设则只允许启动目录
$env:ROUTIVUS_ALLOWED_ORIGINS='http://localhost:5183'  # 终端通道要求；或改设 ROUTIVUS_SERVER_TOKEN
python -m routivus.server
```

监听 `http://127.0.0.1:18765`，自检 `GET /healthz`。

### 3. 启动前端

```powershell
cd frontend
npm run dev
```

打开 `http://localhost:5183`；`/api` 与 `/healthz` 由 Vite 代理到后端。

### 4. 自检

| 检查 | 期望结果 |
|---|---|
| 首页出现 Routivus 品牌与项目列表 | 前后端连通正常 |
| 新建项目 | 路径须落在 `ROUTIVUS_WORKSPACE_ROOTS` 内，否则 422 |
| 顶栏模型选择器显示实际模型名 | provider 配置已生效 |
| 会话里发送一句话 | 出现流式回复 / 工具卡；报 `Provider 未配置` 说明第 1 步未生效 |
| 顶栏「终端」按钮能执行命令 | Origin 白名单生效；报 `terminal_auth_required` 说明第 2 步未生效 |

### 5. 停止

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*routivus.server*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Get-CimInstance Win32_Process -Filter "Name='node.exe'"   | Where-Object { $_.CommandLine -like '*vite*' }         | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

端口冲突时：后端改 `$env:ROUTIVUS_SERVER_PORT`，前端改 `$env:ROUTIVUS_SERVER_URL` 指向新地址（`vite.config.ts` 读取该变量）。

## Web Console Server

启动本地 Server：

```bash
python -m routivus.server
```

默认监听 `127.0.0.1:18765`，健康检查地址为 `GET /healthz`。已实现项目注册与 CRUD（只修改元数据，不会删除或改写项目目录）、会话与消息持久化、全局/项目笔记的范围隔离与搜索、Agent WebSocket 实时事件流、HITL 审批闭环、项目 cwd 绑定的终端通道。前端见下节「Web Console 前端」。

默认只允许注册 Server 启动目录下的项目。需要管理其他工作区时配置：

```bash
ROUTIVUS_WORKSPACE_ROOTS=D:\\DevProject;D:\\Work
ROUTIVUS_PROJECTS_FILE=~/.routivus/projects.json
ROUTIVUS_DATABASE_PATH=~/.routivus/workspace.sqlite3
ROUTIVUS_SERVER_HOST=127.0.0.1
ROUTIVUS_SERVER_PORT=18765
ROUTIVUS_ALLOWED_HOSTS=localhost,127.0.0.1
ROUTIVUS_ALLOWED_ORIGINS=http://localhost:5183
ROUTIVUS_SERVER_TOKEN=
ROUTIVUS_WS_HEARTBEAT_INTERVAL=30
ROUTIVUS_WS_MAX_MESSAGE_BYTES=1048576
ROUTIVUS_APPROVAL_TIMEOUT=300
```

> **注意：这些是「服务端」变量，必须作为真实环境变量传入，项目根的 `.env` 对服务端无效。**
> `ServerConfig.from_env()` 只读 `os.environ`，`python -m routivus.server` 不会调用 `load_dotenv()`（已实测：`.env` 里写 `ROUTIVUS_SERVER_PORT=19999`，服务端仍监听 18765）。
> PowerShell 示例：`$env:ROUTIVUS_WORKSPACE_ROOTS='D:\DevProject'`；cmd 示例：`set ROUTIVUS_WORKSPACE_ROOTS=D:\DevProject`。
> 只有 **Agent 侧**配置（`routivus/config`）才经 `ConfigManager` 读取 `.env`，两者不要混用。

项目接口：`GET/POST /api/projects`、`GET/PATCH/DELETE /api/projects/{project_id}`。会话、消息和笔记分别通过 `/api/projects/{project_id}/sessions`、`/api/sessions/{session_id}/messages`、`/api/notes` 访问；`/api/notes?scope=global` 返回工作区全部笔记，而项目笔记入口只返回当前项目笔记。实时会话地址为 `/api/ws/projects/{project_id}/sessions/{session_id}`，支持 `request_id` 幂等、事件序号、断线后的 SQLite 事件恢复和心跳；配置 `ROUTIVUS_SERVER_TOKEN` 后需要 Bearer Token。错误统一返回 `error.code`、`error.message`、`error.request_id`，每个响应也带 `X-Request-ID`。

### HITL 审批闭环

需要审批的工具（默认 `write_file` 为 `confirm`、`execute_command` 为 `always`）在服务端不再自动拒绝，而是挂起等待客户端决策。会话状态在此期间变为 `waiting_approval`，结束后恢复 `running`。

服务端事件：

```json
{"type":"approval.requested","data":{"kind":"approval","approval_id":"ap-1a2b3c4d","tool_name":"write_file","level":"confirm","arguments":{"path":"a.txt"},"timeout":300}}
{"type":"approval.resolved","data":{"kind":"approval","approval_id":"ap-1a2b3c4d","tool_name":"write_file","decision":"approve","reason":"user_approved","modified":false}}
```

客户端回执（`request_id` 用于幂等，`approval_id` 必须与当前待决项一致）：

```json
{"type":"approve","request_id":"req-2","approval_id":"ap-1a2b3c4d"}
{"type":"reject","request_id":"req-3","approval_id":"ap-1a2b3c4d"}
{"type":"approve","request_id":"req-4","approval_id":"ap-1a2b3c4d","args":{"path":"b.txt"}}
{"type":"approve","request_id":"req-5","approval_id":"ap-1a2b3c4d","scope":"session"}
```

语义分别是：批准一次、拒绝、**改参后执行**（`args` 整体覆盖原参数）、**本会话放行**（`scope="session"`，等价于 `HITLPolicy.allow_all()`，此后同类工具不再请求审批）。两者可以同时给出。`ask_user` 走同一通道，事件里 `kind` 为 `ask`、载荷在 `ask` 字段，客户端用 `{"type":"ask_answer","approval_id":...,"answers":{"字段":"值"}}` 应答，`{"type":"ask_cancel"}` 表示跳过。

fail-closed 规则：`ROUTIVUS_APPROVAL_TIMEOUT`（默认 300 秒）内无人应答 → 按拒绝处理（`reason=approval_timeout`）；同一会话存在待决项时新的审批请求直接拒绝（`auto_deny_busy`）；取消会话会解开挂起的审批。

**注意**：`allow_all()` 只让策略层放行，**不能**绕过 `CommandGuard` / `PathGuard` —— 黑名单命令与越界路径始终拒绝。

### 终端通道

地址为 `/api/ws/projects/{project_id}/terminal`，与会话 socket 相互独立（终端生命周期与 Agent 轮次无关，且终端输出不会写库）。需要 Windows 上的 ConPTY 后端，`pywinpty` **已随核心依赖安装**（带 `sys_platform == 'win32'` 标记），不需要额外装什么；历史写法 `pip install "routivus[terminal]"` 仍然可用（该 extra 已留空以兼容旧脚本）。

```bash
ROUTIVUS_TERMINAL_ENABLED=on
ROUTIVUS_TERMINAL_BACKEND=auto      # auto | conpty | oneshot
ROUTIVUS_TERMINAL_SHELL=            # 空 = 自动探测（pwsh.exe → powershell.exe → %COMSPEC% → cmd.exe）；设 cmd.exe 可回到 cmd
ROUTIVUS_TERMINAL_MAX_SESSIONS=4
ROUTIVUS_TERMINAL_MAX_PER_PROJECT=2
ROUTIVUS_TERMINAL_IDLE_TIMEOUT=900
ROUTIVUS_TERMINAL_COMMAND_TIMEOUT=120
ROUTIVUS_TERMINAL_MAX_INPUT_BYTES=65536
ROUTIVUS_TERMINAL_MAX_OUTPUT_BYTES=262144
ROUTIVUS_TERMINAL_CHUNK_BYTES=8192
ROUTIVUS_TERMINAL_FLUSH_INTERVAL=0.033
ROUTIVUS_TERMINAL_QUEUE_MAX=256
ROUTIVUS_TERMINAL_KILL_GRACE=3
ROUTIVUS_TERMINAL_COLS=120
ROUTIVUS_TERMINAL_ROWS=30
```

客户端消息：`terminal.open`（须为第一条，可带 `cols`/`rows`）、`terminal.input`（`data`）、`terminal.resize`、`terminal.clear`（纯客户端操作，服务端只回 ack）、`terminal.close`，以及 `ping`/`pong`。

服务端事件：`terminal.opened`（含 `cwd`/`shell`/`backend`）、`terminal.output`（`seq` 递增供丢块检测，`encoding` 为 `utf8` 或 `base64`）、`terminal.input.ack`、`terminal.resized`、`terminal.cleared`、`terminal.output.dropped`（背压丢块计数）、`terminal.exit`、`terminal.closed`（`reason` 为 `client_closed` / `idle_timeout` / `limit` / `server_shutdown`）。

`ROUTIVUS_TERMINAL_BACKEND=auto` 在缺 pywinpty 时会**明确报错**（`terminal_unavailable`）而不是静默降级 —— 静默把终端换成命令框会让 `cd`、环境变量、venv 激活悄悄失效。确实想要无状态的受限命令执行器时显式设 `oneshot`；注意 `oneshot` **不接受** `ROUTIVUS_TERMINAL_SHELL`（它每条命令都走 `cmd /c`，与持久 shell 不等价）。

**启动的 shell**：**默认就是 PowerShell**（Windows 上不设任何变量即可）。优先级为

```
ROUTIVUS_TERMINAL_SHELL  →  pwsh.exe        # PowerShell 7，优先（UTF-8 / VT 支持更好）
                         →  powershell.exe  # Windows PowerShell 5.1，Win10/11 必带
                         →  %COMSPEC%       # 都没有才回退
                         →  cmd.exe
```

探测只在 Windows 上做（终端通道依赖 pywinpty，本身就是 Windows-only），用的是 `shutil.which`，返回裸文件名交给 PATH 解析（PS 7 装在 `C:\Program Files\...`，裸名可以绕开引号问题）。想回 cmd：`ROUTIVUS_TERMINAL_SHELL=cmd.exe`（显式配置永远优先）。

启动参数由程序按 shell 类型给，**不要自己把开关写进这个变量**：cmd 用 `/Q /D`，PowerShell 用 `-NoLogo -NoProfile`，其他 shell 不加参数。`-NoProfile` 与 cmd 的 `/D` 同源（不执行机器 / 用户级自动脚本），代价是自定义 profile 不加载，但 `ls` / `dir` / `copy` / `del` / `cls` 是 PowerShell **内置别名**，不依赖 profile。反过来说，把 cmd 的 `/Q` 传给 PowerShell 会让它当成命令名报错后直接退出（界面表现为「终端打开了却什么都没有」），所以类型判断是必需的。

前端抽屉的标题与提示符按 `terminal.opened.shell` 显示（PowerShell 会显示成 `PowerShell` 与 `PS D:\x>`），不是写死的 `cmd`。

#### 安全边界（务必阅读）

**终端通道强制要求鉴权**：必须配置 `ROUTIVUS_SERVER_TOKEN` 或 `ROUTIVUS_ALLOWED_ORIGINS` 之一，否则端点直接以 `terminal_auth_required` 拒绝。原因是 Origin 校验在 `allowed_origins` 为空时一律放行，而浏览器发起 WebSocket 不受 CORS 约束 —— 不加这道限制就等于给任意网页一个 shell。

**终端不是沙箱。** 进程由服务端绑定项目根目录启动，客户端无法指定路径，每条命令经过 `CommandGuard` 黑名单、`PathGuard`（含 `cwd` 越界检查）、超时限制与审计。但黑名单只匹配命令字符串，看不到 shell 的当前目录，因此：

- 持久化 shell 里 `cd ..` 之后再执行命令，即可操作项目根之外的文件；
- `type C:\Users\...\.ssh\id_rsa`（PowerShell 下是 `Get-Content C:\...\id_rsa`）这类用绝对路径读取外部文件的方式不在黑名单内；
- `subst` / `mklink /J` 可以把外部目录映射成根内路径。

黑名单同时覆盖 cmd / POSIX 写法（`del C:\`、`rd /s`、`rm -rf /`）与 **PowerShell 原生写法**（`Remove-Item -Recurse -Force C:\`、`ri -r C:\`、`Stop-Computer`、`Restart-Computer`、`Format-Volume`、`Clear-Disk`），两类都拦。但它仍然只是「命令字符串层面的尽力而为」：目标是变量（`$env:USERPROFILE`）、命令写在脚本文件里、或绕过别名直接调 .NET API 等情形都不在覆盖范围。

Windows 上没有非特权 chroot 类原语，所以这里保证的是「**客户端无法指定路径**」，**不是**「操作系统阻止进程访问根外资源」。终端以服务端用户身份运行，可读写该用户能触及的任何资源（含网络）。请只在本来就信任浏览器客户端的机器上启用，用 `ROUTIVUS_TERMINAL_ENABLED=off` 可完全关闭。真正的 OS 级隔离（AppContainer 或低权限账户）尚未实现。

断开连接、空闲超时或服务退出时，会通过 Windows Job Object（`KILL_ON_JOB_CLOSE`）加 `taskkill /F /T` 兜底回收整棵进程树，避免留下孤儿 shell。

### 行为变更：`execute_command` 的 cwd 校验

`guard_tool_call` 此前对 `execute_command` **只**跑 `command_guard`，`path_guard` 里针对 `cwd` 的检查因分派提前返回而不可达。现已修正为两者都执行：Agent 的 `execute_command` 工具若把 `cwd` 指向项目根之外将被拒绝（`path_outside_root`）。这是有意的收紧。

## Web Console 前端

前端位于 `frontend/`（Vite + React 18 + TypeScript，无 UI 框架依赖），实现计划见 `plans/routivus-implementation-plan.md`。

```bash
cd frontend
npm install
npm run dev        # 开发服务器 http://localhost:5183，/api 与 /healthz 代理到后端
npm run lint       # ESLint（eslint.config.js，仅覆盖 src/）
npm run typecheck  # tsc --noEmit
npm run build      # 产出 frontend/dist
npm run check      # lint + typecheck + build，提交前的质量门禁
```

先启动后端，再启动前端（完整步骤与自检见「快速开始」）。`vite.config.ts` 通过 `ROUTIVUS_SERVER_URL`（默认 `http://127.0.0.1:18765`）指定后端地址；后端若设置了 `ROUTIVUS_SERVER_TOKEN`，前端需在 `frontend/.env` 中配置同名 `VITE_ROUTIVUS_TOKEN`。

已接入能力：

- **两级导航**：全局态（首页 / 笔记 / 配置）与项目态（会话 / 笔记）由 hash 路由驱动，刷新后按 URL 恢复项目、会话与视图；项目内无会话时自动创建首个会话。**会话可删除**：左侧会话项右键 →「删除会话」，确认后连同它的消息与事件一起从数据库删除（`DELETE /api/sessions/{id}`，外键级联）；**运行中的会话会被拒绝**（`409 session_busy`，先停止再删）；删掉地址栏指向的那个会话时，界面自动切到列表里的下一个，最后一个也删掉时会重新自动建一个新会话。
- **首页**：项目卡（会话 / 笔记 / 今日调用 / 今日与累计 token）+ 全部项目活动热力图（52 周 × 7 天，未来日期不渲染）+ token 用量面板 + 新建项目；项目卡支持**重命名**（只改显示名，根目录不变）与**移除**——移除只摘 `projects.json` 注册，不删除磁盘文件与会话 / 笔记数据（重新添加同一路径即可恢复可见），仍有运行中会话的项目会被拒绝移除（`409 project_busy`）。
- **Token 用量面板**（`GET /api/usage/summary?days=30`）：今日 / 近 7 天 / 近 30 天 / 累计四张 KPI 卡 + 按档位分布（四档各一色，绿 → 蓝 → 琥珀 → 砖红，日/夜两套色板随主题切换）。**两条口径刻意分开**：按天 / 按区间 / 按档位来自逐轮 `session.usage` 事件，**累计**来自 `sessions` 的 token 快照求和；两者差值超过 5% 时面板会明说（早期事件可能被裁剪），而不是给一个看起来精确的数。所有"日"都是**本地日**（含热力图与项目卡的「今日调用」，此前按 UTC 分组，东八区 00:00–08:00 会算到前一天）。档位归因字段（tier / provider / model）从本版本开始写进用量事件，此前的事件由后端归入「未标注」，但**面板不画这一项**——历史存量会把它顶到满格、把真正要看的四档压成一条线；接口照旧返回全部档位，前端只画四档，也不回填。金额不做：provider 返回的用量里没有缓存命中字段，折算出来的钱会系统性偏高。
- **智能路由数据看板**（`#/router`）：从配置页 SmartRouter 卡片的「详情 →」进入（导航里不单列）——产物来源与原因码、每轮路由分布与耗时、校准与自学习规则、样本积累与本地进化门槛进度，纯只读。详见「SmartRouter 智能路由」一节的「数据看板」。
- **笔记**：全局入口显示全部笔记及项目归属，项目入口只显示当前项目笔记；搜索、新建、编辑、标签、置顶、删除均走服务端；版本冲突返回 409 时提示「用当前内容覆盖」，不静默丢失。**会话内的 agent 可读写当前项目笔记**：`notes_list` 列目录、`notes_read` 按 id 读正文与版本号（长笔记按字符分段续读，默认 8000 字、单页上限随工具输出上限自动收窄，保证"继续读"的提示不被截断）、`notes_create` 新建、`notes_update` 修改（`append=true` 为追加）、`notes_delete` 删除。范围由服务端在构造 agent 时钉在当前项目，模型**无法指定**别的项目——工具参数里根本没有这个字段；**全局笔记对 agent 完全不可见**（读不到也写不到：读它会把所有项目混进当前上下文，写它会影响别的项目），越权、全局、不存在三种情况返回**同一句**提示。改与删必须先 `notes_read` 拿到版本号：`expected_version` 不一致会被拒绝并提示重读（**错误里不给新版本号**，避免拿它把旧写重放一遍），删除还要求**原样回显标题**（既防呆，也让审批卡上有一行人能看懂的内容）。写操作走 HITL：`notes_create` / `notes_update` 确认、`notes_delete` 必审；正文上限 20000 字、标签最多 10 个，超限**报错而不是静默截断**。审计只记 id / 标题 / 标签 / 长度，**正文不落 audit.log**。笔记属用户内容，正文前会带一行「资料而非指令」的声明。开关：`ROUTIVUS_NOTES_WRITE=off` 可只保留两个只读工具。界面暂不自动刷新——agent 写完，正开着的笔记页要切走再回来才更新。
- **团队卡**：展示 Worker 与角色进度，失败时逐任务给出原因分类；**可续跑**（方案 15）——中断后卡上出现「继续」（跳过已完成的，重跑其余），`needs_scope` 的失败给「选择修改范围并继续」（候选勾选 + 自由输入 + 「将授权：…」回显）。在会话里说一句"继续"只会**把按钮亮一下**（6 秒），不会自动执行——"继续"歧义太大（也可能是在聊上一个问题），执行永远要你点。
- **会话视图**：WebSocket 事件流渲染消息、思考块、工具卡、计划 / 团队任务卡、审批与提问卡。**思考块**：provider 返回推理内容（reasoning）时按段落独立显示——流式期间展开，段收尾自动折叠成「思考 · N 字」一行，点击展开；刷新 / 重连后仍在（默认折叠）。**时间线顺序**：思考 / 正文 / 工具卡严格按事件时序交错显示（正文按段落落库，角色 `thinking` 仅用于展示、不参与模型上下文）；重连后按时间戳归并重建，顺序与在线一致。`/team` 并行 worker 的输出按来源分桶并带短标签（如 `a1b2c3d4`），不会互相黏连。`/plan <任务>` 与 `/team <任务>` 在会话内直接可用（生成计划后弹出审阅卡：批准执行 / 重新规划 / 取消）；四页签信息侧栏（Session / Plan / Memory / Safety）；Composer 支持 `Enter` 发送、`↑↓` 历史、`Tab` 应用命令补全、运行中停止。

- **命令执行与补全**：会话内可直接执行 slash 命令——`/help`、`/model`、`/smartrouter`、`/tier`、`/provider`、`/config`、`/hitl`、`/memory`、`/save`、`/lang`、`/clear`、`/skill`（复用 TUI 的 `CommandService`，回执以消息落库；`/cancel` 等价取消按钮；`/exit` 已移除，按未知命令处理）。命令切模型 / 开关智能路由会实时同步顶栏与配置页，`/save`、`/memory` 改完长期记忆会刷新侧栏 Memory 页签。补全浮层覆盖上述全部命令（`↑↓` 选择、`Tab` 应用、`Esc` 关闭），`/model model <前缀>` 提示真实模型名，`/skill load <前缀>` 提示 Skill 名。运行中的会话不接受命令（先停止或取消）。
- **Skill（任务规范）**：独立管理页（导航「技能」，路由 `#/skills`）——顶部项目选择器切换「全局（内置 + 用户级）/ 某项目」，支持列表、正文预览、新建与编辑（写入 `SKILL.md`）、启用/禁用；会话内也可用 `/skill list|load|enable|disable`，两者共用同一份配置。规范放 `<用户目录>/skills/<名称>/SKILL.md` 或项目 `.routivus/skills/` 下即被自动发现；索引注入 system prompt（开关变更后下一轮生效），正文由模型按需调用 `load_skill` 加载，参考资料受路径白名单与字数上限约束。
- **Markdown 渲染**：会话正文支持标题 / 列表（含嵌套）/ 表格 / 任务列表 / 删除线 / 引用 / 链接 / 图片 / 围栏代码块；代码块带语言角标、一键复制与语法着色（暖白与夜间各一套配色，均经对比度校核）。三重取舍：流式输出期间先不着色、这一轮结束后再上色；逐 token 的增量先攒 60ms 再合并刷出（把渲染次数封顶）；单块超过 300 行或 20k 字符跳过着色（保滚动与内存，角标与复制仍在）。安全边界不变：不渲染裸 HTML、链接仅 http/https、不使用 `innerHTML`。
- **项目文件**：顶栏「文件」页签进入整页视图——左侧懒加载文件树（逐层请求、可切换显示被忽略目录），右侧查看或编辑；聊天页还可按 `Ctrl+Shift+E` 展开只读抽屉边聊边看。点击文件**默认先预览**（想改再点「编辑」），编辑态可保存（`Ctrl+S`）并显示光标行列；图片直接预览，二进制/超 1MB/含无法解码字节的文件只读。写入边界：只允许项目根内（`..` 与软链接逃逸一律拒绝）、拒绝写 `.git` 与被忽略目录、超 5MB 拒绝；保存带内容版本号，文件被外部改过会提示「覆盖 / 重新加载」而不是静默覆盖；换行符按原文件保留（Windows 上 CRLF 文件不会因为改一行而整篇 diff）。每次写入都会记入 `.routivus/audit.log`。**文件可删除**（整页文件视图的树里右键，抽屉是只读的、没有右键菜单）：与写入同一套路径护栏，另加四条——项目根与 `.routivus`（项目数据目录：记忆 / 审计 / 项目级 Skill）不能删、被忽略目录里的东西不能删、**软链接只摘链接本身**（指向哪里都不影响目标）、**非空目录必须勾选「递归删除」**（服务端对没带递归标志的非空目录回 `409 directory_not_empty`，这是有意的第二次确认）；每次删除同样落审计（`file_delete`）。
- **终端抽屉**：`Ctrl+\`` 或顶栏按钮展开，走 `/api/ws/projects/{id}/terminal`，服务端绑定项目 cwd；**Windows 上默认就是 PowerShell**（自动探测 `pwsh.exe` → `powershell.exe`，见「终端通道」一节），想用 cmd 设 `ROUTIVUS_TERMINAL_SHELL=cmd.exe`；抽屉标题与提示符跟随实际 shell。
- **智能路由**：配置页开关与四档；普通对话轮按任务复杂度自动换档，顶栏 chip 与信息侧栏显示本轮档位与实际模型。ML 精判走「出厂兜底 → 本地进化」，`/smartRouter status` 会显示 ML 可用性与**原因码**（例：`runtime_missing` 即 onnxruntime 导入失败）、产物来源（语义版 / 无语义兜底版）以及本地进化状态（样本量 / 上次进化 / holdout 对比）；`/smartRouter evolve` 手动触发一次本地重训。
- **主题**：暖白 / 夜间双主题（含夜空动效），偏好存 `localStorage`。

已知限制：

- 会话断线重连以服务端 `session.snapshot` 重建：文本（含思考段）来自 messages 表（**最近 500 条**，更早的需要翻 REST 分页），**卡片（命令 / 工具 / 计划 / 团队 / 换档）来自快照带回的 `replay` 事件回放**（按类型取最近 1000 张卡片，流式增量不占窗口），两者按时间戳归并，顺序与在线一致（换档卡由回放里的 `router.updated` 重建，冷渲染不重播动画）；仍挂起的审批 / 计划审阅由 `pending` 字段恢复，重连后可直接继续应答。**行为变更**：`message.completed` 自 07 起只作"本轮结束"信号，不再携带整段正文（正文由 `message.segment` 逐段下发）；消费消息流的脚本需按此适配。服务进程重启后内存桥不再存在，待决交互无法恢复（计划审阅的"重新规划"同样依赖内存桥）。
- 配置页可编辑（Provider 增删改 / Key / 模型列表 / 四档 / SmartRouter 开关，走 `/api/config`）；Skill 有独立页面（走 `/api/skills`，可切换全局 / 项目上下文并新建、编辑、启停）；Memory 条目在会话侧栏 Memory 页展示（走 `/api/sessions/{id}/memory`）。
- `/plan`、`/team` 在会话内**可用**，但审阅是**阻塞式**的：等待决策期间不接受新指令，客户端需用 `plan_decision`（`action` = `execute` / `cancel` / `replan`）应答；超时按取消落地（`ROUTIVUS_APPROVAL_TIMEOUT`，默认 300s）。协议级测试见 `tests/test_server_plan_team.py`。
- **`/team` 可以在团队卡上续跑**（方案 15）：中断（失败 / 取消）后不必重新发起——卡上给「继续」，点它就从断点接着跑，**已完成的子任务跳过、不重新规划**。没有挂起等待态（失败即终态），续跑是用户从终态发起的显式动作，上限 `ROUTIVUS_TASK_MAX_RESUMES`（默认 3，计数存在快照里、跨重启有效）。**注意**：续跑要读服务端的可恢复快照（`team.snapshot`，写在 events 表里），快照被裁剪或服务重启后 Artifact 已丢时是**降级可用**——已完成任务不会重跑，但它们只有结果文本可用（工具调用明细不可得，磁盘上的改动仍在）。
- **Reviewer 无法安全确定写入范围时**，任务**直接判失败**（`repair_scope_missing` / `repair_scope_unsafe` / `review_execution_failed`）并标记 `needs_scope`，卡上给出「选择修改范围并继续」：候选来自该任务已声明的 claims 与同伴任务的 write 范围（服务端保守提取，不猜路径），**勾选/填入即授权**，确认后才启动 Repairer；越界、黑名单、把只读升成写入一律被服务端拒绝（fail closed），授权动作与最终生效范围都进 `audit.log`。修好后会自动把剩余批次跑完，不必再点一次。
- `/team resume` **命令形态不存在**（发了会回报 `unknown_command`）：续跑走卡片上的结构化消息（`team_resume`），因为授权需要勾选那样的结构化输入，命令行形态只能手抄路径。
- Memory 页签展示的是**项目级**长期记忆（`<项目根>/.routivus/memory.db`，同一项目的所有会话共享同一份）；会话快照的 `memory` 段与 `/api/sessions/{id}/memory` 都直接读它，`/save`、`/memory delete` 执行后会推送 `memory.updated` 让页签重拉。库里没有条目时为空态（不会为了看一眼记忆就在项目里建库）；条目默认只回传最近 20 条（`limit` 上限 100），超出会在页签里提示总数。
- 终端通道要求鉴权：Vite 开发端口是 `5183`，需把后端 `ROUTIVUS_ALLOWED_ORIGINS` 设为 `http://localhost:5183`（或配置 `VITE_ROUTIVUS_TOKEN`），否则终端会以 `terminal_auth_required` 拒绝。
- 本地进化的节奏：隐式信号是**弱标签**（`interrupt` 可能是手滑），可用样本只产在"档位被判定不合适"的轮次，桌面端一天可能只有几条 → **首次自动进化通常落在第 2–4 周**；在那之前让你觉得"越来越贴合"的是 L1（校准 + 局部规则，秒级生效）。另外：随包 `router.lgb` 由 34 条演示样本训出（`val_accuracy` ≈ 0.29，接近四分类随机水平），更像"接口占位"；满足门槛（首次 ≥120 条）的本地重训大概率会明显超过它——这也是"不劣门"只在**本地产物之间**比较的原因（本地 holdout 没有原文，TF-IDF 列全零，拿出厂产物比不公平）。
- 语义编码器 `router_semantics.onnx` 随包 **22.8MB**（int8 量化），所以 wheel 体积约 26MB；`tools/export_bge_onnx.py` 可离线重导出，`tools/distill_nosem_router.py` 可复现无语义兜底产物。
- 未实现「开发环境 mock adapter」：前端全部走真实 REST / WebSocket，没有离线可视化回归模式，视觉回归依赖真实后端。

## 数据目录与迁移

| 路径 | 内容 | 由谁创建 |
|---|---|---|
| `~/.routivus/projects.json` | 项目注册表（原子写入） | `ROUTIVUS_PROJECTS_FILE` |
| `~/.routivus/workspace.sqlite3` | 会话、消息、笔记、事件、幂等 request 表 | `ROUTIVUS_DATABASE_PATH` |
| `~/.routivus/config.json` | Provider / 模型 / SmartRouter / 主题无关的后端配置 | `/provider`、`/tier` 等命令 |
| `~/.routivus/adaptive/router.lgb` | SmartRouter ML 精判产物（出厂语义版 → 可被本地进化替换；`router.lgb.prev` 为上一版，供回滚） | 随包落位、`/smartRouter evolve`、`/train` |
| `~/.routivus/adaptive/router.lgb.nosem` | **无语义兜底产物**：缺 onnxruntime 的环境仍有一层 ML 精判 | 随包落位（`tools/distill_nosem_router.py` 可复现） |
| `~/.routivus/adaptive/router_semantics.onnx` + `.json` | 语义编码器（bge-small-zh，int8）与伴生 tokenizer | 随包落位；`tools/export_bge_onnx.py` 可离线重导出 |
| `~/.routivus/adaptive/calibration.json`、`learned_rules.json` | 本地校准偏置与自学习规则（L1，秒级生效） | 启动时聚合 `feedback.log` |
| `~/.routivus/adaptive/sem_samples.jsonl` | 本地样本库：`text_hash` + 512 维语义向量（**不存原文**） | 路由时自动采集（编码器可用时） |
| `~/.routivus/adaptive/evolve_state.json`、`evolve.log`、`evolve.lock` | 进化状态（上次尝试/成功、增量计数）、每次尝试的决策记录（含被"不劣门"丢弃的原因）与演化子进程互斥锁（残留超 1 小时可被接管） | `/smartRouter evolve`、自动触发 |
| `~/.routivus/adaptive/semantic_head.json` | L3 本地语义头（每档质心，方案 10 §4.7） | 进化时训练 |
| `~/.routivus/input-history/` | 按项目隔离的输入历史 | 自动 |
| `<项目>/.routivus/memory.db` | 项目长期记忆 | `/save`、`/memory` |
| `<项目>/.routivus/audit.log` | 审计 JSONL（工具、审批、终端命令） | 自动 |
| `<项目>/.routivus/tmp` | 终端子进程的 `TEMP`/`TMP`（让终端临时文件留在项目内） | 终端通道 |
| `<项目>/Routivus.md`、`Routivus.local.md` | 项目记忆文件，每次任务自动注入 | `/init` 或手动 |

迁移行为：`workspace.sqlite3` 用 `PRAGMA user_version` 版本化，`WorkspaceStore._initialize()` 在打开时按版本增量建表（当前 `VERSION = 4`；v4 只加了 `idx_events_type_time` 索引供用量面板按"事件类型 + 时间窗"查询，**不动表结构**）。**新增一种事件类型不算迁移**：Team 的可恢复快照（方案 15）就是复用 `events` 表存 `team.snapshot`，读取时按类型取最近一条，既不改表结构也不抬 `VERSION`。**只支持向上迁移**：若文件版本高于当前代码版本会直接抛错，不做降级。项目注册表带 `version` 字段，版本不匹配同样拒绝读取。删除数据请直接删文件（会话例外：界面右键或 `DELETE /api/sessions/{id}` 即可，消息与事件由外键级联一并清掉）；**删除项目注册关系不会删除项目目录**（`DELETE /api/projects/{id}` 只改元数据）。

### 本地进化与隐私（SmartRouter）

ML 精判是「**出厂兜底 + 本地进化**」的分层结构：出厂基线人人相同，之后各人不同。

| 层 | 内容 | 谁在变 |
|---|---|---|
| L0 出厂兜底 | 随包的语义编码器、`router.lgb`（语义版）与 `router.lgb.nosem`（无语义兜底）。首启复制到数据目录，**已存在则不覆盖**（不会冲掉你的进化成果） | 所有人相同 |
| L1 在线微调 | `calibration.json`（每档偏置，±0.15 夹紧）+ `learned_rules.json`（±1 档局部规则）；单档样本 ≥20 才生效 | **已经每人不同**，秒级、每次启动重算 |
| L2 本地重训 | `/smartRouter evolve`（或自动触发）用本机 feedback 重训 `router.lgb`，产物含 L3 语义头 | 每人不同 |

**自动演化何时触发**：启动后与每 50 轮各检查一次，**全部**满足才训练 —— 距上次尝试 ≥7 天、自上次成功新增可用样本 ≥20、每档有效标签 ≥20 且总量 ≥60（首次 ≥120）、没有其他演化进程在跑。**不劣门**：新产物在同一 holdout（按时间切分）上的加权准确率不得低于当前在用产物，否则**丢弃**并把原因写进 `evolve.log`。训练在子进程里跑（`python -m routivus.adaptive.evolve`），不阻塞交互；只有真实服务进程会触发（`ROUTIVUS_SERVER_RUNTIME=1`），测试与库调用永远不会偷偷训练。开关：`ROUTIVUS_ADAPTIVE_EVOLVE=off` 关掉自动演化（手动命令仍可用）。门槛本身也可微调：`ROUTIVUS_ADAPTIVE_COOLDOWN_DAYS`（默认 7）、`ROUTIVUS_ADAPTIVE_MIN_NEW`（默认 20）、`ROUTIVUS_ADAPTIVE_CHECK_ROUNDS`（默认 50 轮检查一次）。

**隐私**：反馈与样本**全部留在本机**，不联网、不上传。`feedback.log` 只存输入的短哈希与特征快照；`sem_samples.jsonl` 存的是 512 维语义向量（int8 量化，基本不可逆推原文）——**整条训练链路不需要原文**。清空入口：`/smartRouter reset`（清校准与规则）或 `/smartRouter reset --hard`（连本地产物与样本一起，下次启动回到出厂基线）。

## MVP 冻结与验证记录

MVP 版本冻结为 **0.1.0**（`pyproject.toml` 与 `frontend/package.json` 一致，`/healthz` 返回 `version: 0.1.0`）。Phase 7 验证结果：

| 项 | 命令 / 方式 | 结果 |
|---|---|---|
| 后端全量测试 | `python -m pytest` | 728 passed, 7 skipped（47s） |
| 前端质量门禁 | `npm run check` | lint 0 问题、tsc 0 错误、build 成功（63 模块，≈211 kB JS / ≈39 kB CSS） |
| 路径与归属安全 | 真实 HTTP 调用 | 越界路径 422、不存在路径 422、重复注册 409、跨项目读会话/笔记 404、跨项目改归属 422、未知项目 404 |
| 笔记范围隔离 | 真实 HTTP 调用 | 全局列表含各项目笔记；项目列表只含本项目笔记；陈旧 `version` 写入 409 |
| 会话事件契约 | 真实 WebSocket | 快照 `sequence` 单调递增；断线重连后同一条用户消息**不重复**；重复 `request_id` 返回 `duplicate_request` |
| 终端通道 | 真实 WebSocket（ConPTY） | `terminal.opened.cwd` 绑定项目根、`echo` 正常、`format c: /y` 被 `command_blocked` 拦截 |
| 前端验收用例 | 无头 Chrome 驱动（§8.3 十条） | 23/25 + 夜间 6/6 全通过（含两级导航、笔记范围、项目切换隔离、搜索清空、快照恢复） |

Phase 7 期间发现并修复的前端缺陷：

1. **终端事件协议不匹配**：终端事件的字段是扁平的（`{type, terminal_id, cwd, data, ...}`），前端却按会话事件的 `data` 信封读取，导致终端输出、错误提示、`terminal.closed` 全部静默丢失。已改为独立的 `TerminalEnvelope` 类型。
2. **主题按钮文案语义错误**：原型 `ttLabel` 显示**当前**主题，前端显示成了「切换目标」。
3. **「新建项目」卡未跨两列**：与原型 `grid-column:1 / -1` 不一致。

**未覆盖 / 未验证**：本机未配置 provider，因此**没有跑通一次真实 LLM 的 Agent 轮次**——会话流、工具卡、审批卡的渲染是按事件契约实现并由协议级测试覆盖的，但「真实模型流式回复」需要在配置 provider 后人工确认（步骤见「快速开始」第 1 步）。`/plan`、`/team` 同理：**会话通道（前缀分派、卡片载荷、审阅往返、超时 fail closed）由 `tests/test_server_plan_team.py` 以替身执行器覆盖**，但由真实模型生成计划 / 调度 Worker 的端到端链路仍未验证。

## 程序化入口

`routivus.service.commands.handle_service_command(agent, settings, manager, raw)` 对所有斜杠命令做、UI 无关的分发，返回 `(output_message, should_exit)`，供前端直接渲染：

```python
from routivus.service.commands import handle_service_command

message, should_exit = handle_service_command(agent, settings, manager, "/model")
print(message)
```

## 配置 Provider

所有 provider 配置（定义、URL、API Key、模型列表）**统一写入 `config.json`**，由后端 `/provider` 命令完成，无需手改文件、无需 `.env`。

**怎么执行这些命令**：本仓库没有 REPL / TUI，`[project.scripts]` 也为空，所以要用 `routivus.cli.commands` 的确定性入口；下面示例里的 `raw` 就是斜杠命令原文，把 `--yes` 视作「确认」：

```powershell
python -c "from routivus.config.manager import ConfigManager; from routivus.cli.commands import execute_provider_command; print(execute_provider_command(ConfigManager(), None, '/provider add myproxy https://gateway.example.com/v1 --model deepseek-v4 --key sk_x --set-base')[0])"
```

需要 Agent 对象才能分发的命令（`/mcp`、`/web`、`/skill`、`/model` 等）走 `routivus.service.commands.handle_service_command(agent, settings, manager, raw)`；纯配置类命令（`/provider`、`/tier`）可直接用上面这种 `ConfigManager()` 形式。

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

SmartRouter 按任务复杂度动态选择四档模型（Basic / Enhanced / Superior / Ultimate）。

**在 Web Console 会话里怎么生效**：普通对话轮在执行前先路由，再按结果切换本轮实际使用的模型（`router.updated` 事件 / 会话快照的 `router` 字段）。界面分四层：

- **顶栏 chip**：**四态**——`⚡ 待路由`（开关开着但还没路由过，⚡ 缓慢呼吸）/ `⚡ 路由中…`（普通消息已发出、`router.updated` 未返回，文字 shimmer 流光）/ 档位徽章 `⚡ Superior` / `⚠ 路由失败`。档位徽章带**档位色 + 4 段电量条**（1–4 格对应 Basic–Ultimate，颜色沿用 token 面板那套 `--tier-*`），文字保持中性色（11px 彩字在浅/深底上对比度都不稳，色只上图形元素）。状态切换有一次性动画：出结果弹跳 + 逐格点亮、换档颜色渐变 + 光晕外扩、同档重路由只有极轻的亮度脉冲。**点击展开浮层**。
- **「路由中」是前端本地推导的瞬态**：只对**普通消息**（非斜杠输入，斜杠是命令 / `/plan` / `/team`）且开关开启时置位，三条回落——① 收到 `router.updated`；② 收到证明本轮不路由的事件（`plan.updated` / `plan.review` / `team.updated` / `command.executed` / 首个 `message.delta` 等）；③ 2s 兜底超时。缺了 ②③，一次 `/plan` 之后 chip 会永远停在"路由中"。
- **浮层**：① **四档竖排电平表**（高档在上）——每档一行，当前档的滑块高亮；换档时滑块从旧档**平滑推到**新档（打开浮层后若刚换过档，能看见这个位移）；未配置、或该 provider 缺 Key 的档位标虚线 tag 与"回落 → 实际模型"；② **本轮判定路径流水线**（特征提取 → 规则打分 → ML 精判 → 校准偏置 → 后处理）——`notes` 是有序因果链，命中节点逐级点亮，被拦的环节（ML 低置信 / 不可用）灰掉划线下沉；③ **置信度环**（620ms 扫到实际值，`confidence < 0.5` 改虚线灰）；④ 依据列表按 primary / muted 两档主次错开入场。
- **信息侧栏 Session**：拆成「会话默认」（手动配置的 provider/model）与「本轮实际」（档位徽标 + 依据 + 是否真的换了模型 + 耗时），不再让同一个 key 一会儿是配置、一会儿是结果；档位徽标同样取档位色。
- **对话流换档卡**：只在**档位变化 / 回落 / 失败 / 迟滞冻结 / 防降级**时出现（判据与旧提示完全一致，不新增频率）。卡上画出档位位移（`Enhanced ━━▶ Superior`，箭头划过 + 旧档淡出、新档以档位色淡入）+ 依据 + 规则总分 count-up；**冻结 / 防降级不画箭头**（档位没变，箭头自相矛盾），改为「停在档 + 🔒/🛡 + 冷却色调」。刷新 / 重连后卡片仍在原来的轮次位置（由 `router.updated` 全量回放重建），且回放是冷渲染、**不重播动画**。
- **配置页四档表**：新增「当前生效」列，并给出每档"实际会用哪个模型"（未配置、或该 provider 缺 Key 时都会整档回落，界面标为"已配置 · 实际回落"）。

以上动画全部是 CSS `transition` / `@keyframes`，零新依赖；跟随系统的 `prefers-reduced-motion` 时会整体降级为**瞬时终态**（滑块直接到位、环直接画满、换档卡只显示新档）。

判定依据（载荷里的 `notes`）是**结构化枚举**：`hard_rule:arch`、`score:6.5`、`ml:idx=2,p=0.71` / `ml:skipped:low_conf(p=…)`、`calibration:+1`、`rule:debug`、`learned:+1:num_bugfix_kw>=1`、`anti_downgrade:Ultimate→Superior`、`hysteresis:frozen:Superior`。中文文案统一由前端 `utils/routerNotes.ts` 渲染（后端只记录、不参与任何判定）。开关与四档在**配置页**维护（`/api/config/smart-router`、`/api/config/tiers/{tier}`），配置档位会自动打开总闸。

边界：

- **只有普通对话轮路由**：`/plan`、`/team` 不参与（与 TUI 的门禁一致）。计划与团队的子任务沿用执行器拿到的那份 LLM 配置。
- **路由在工作线程里执行**：校准 / 自学习 / ML 精判（含 23.9 MB 语义 ONNX 会话）是同步重活，首次加载可能数秒；服务端把它丢到工作线程并带超时（`ROUTIVUS_ROUTER_TIMEOUT`），所以**不会阻塞事件循环**（否则表现为「一对话就卡住」、心跳与其它 HTTP 全部停响应）。重资产是进程级单例，只加载一次，后续会话零成本。加载或路由偏慢时会打 WARNING 日志（含耗时），便于排查。
- 路由结果**只改内存**中的 provider/model，不写回 `active_provider` / `active_model`；重连后档位可从会话快照回显，**服务重启后不保留**。
- **手动优先**：在顶栏显式切换模型会关闭智能路由（与 `/model` 的行为一致），避免下一轮路由立刻覆盖刚选的模型。
- 档位未显式配置、或该 provider 缺 API Key 时会回落 active 模型（界面会标出"实际回落 → 具体模型"）；换模型失败只会**沿用当前模型**并回报错误，不阻断对话。

档位的 provider/model 用 `/tier` 配置，总开关用 `/smartRouter`（以下命令经 `handle_service_command` 程序化分发，Web Console 请走配置页）：

| 命令 | 行为 |
|------|------|
| `/smartRouter status` | 查看路由状态、四档配置、ML 精判（产物来源 + 不可用原因码）、语义通道指标与本地进化状态（样本量 / 上次进化 / holdout 对比） |
| `/smartRouter on` / `off` | 开启 / 关闭智能路由 |
| `/smartRouter evolve [--force]` | 手动触发一次本地重训（后台子进程，不阻塞界面；`--force` 跳过门槛但仍守不劣门），完成后用 `status` 看结果 |
| `/smartRouter reset [--hard]` | 清空校准与自学习规则并重建共享路由状态（重载 ML 模型等）；`--hard` 连本地产物、样本库与上一版备份一起清除，下次启动回出厂基线 |
| `/tier list` | 列出四档 provider/model（未配回落主动 active） |
| `/tier show <tier>` | 查看单个档位 |
| `/tier set <tier> <provider> [model]` | 设置档位 provider/model（缺省 model 用该 provider 的 default_model） |
| `/tier clear <tier>` | 清空档位，回落到手动 active |
| `/train [labeled.jsonl] [--yes] [--no-semantic]` | 手动训练 ML 精判模型（需确认） |

- **ML 精判的回落链**：按 `语义版` → `无语义兜底版` → `不可用` 依次尝试。语义版（`~/.routivus/adaptive/router.lgb`，声明 512 维语义列）要求编码器可用；环境里缺 / 装坏 `onnxruntime` 时会落到随包的 `router.lgb.nosem`（只用数值特征），不再出现"整层 ML 精判消失"。两级都不行才判为不可用。`/smartRouter status` 显示**产物来源**（`semantic` / `nosem`）与**不可用原因码**（`no_artifact` / `no_semantic` / `runtime_missing` / `load_failed` / `bad_artifact` / `version_mismatch`），界面提示按码给出可操作文案。本地产物（`/train` 或本地进化产出）始终优先于随包基线。
- **训练**：默认走 TF-IDF + LightGBM 离线训练；本地重训用本机样本、可带语义列（见「本地进化与隐私」）。进化替换前会把旧产物备份为 `router.lgb.prev`，供回滚。
- **语义通道**：内存 + 任务特征打分命中一定触发条件后，用 BGE 语义编码器（`router_semantics.onnx`，512 维 int8）增强 ML 路由精度。编码器不可用时**不再"静默"**回落：原因码会进路由依据、`status` 与前端提示。可通过 `/smartRouter status` 观察语义编码次数与耗时。
- 语义编码器（`router_semantics.onnx` + 伴生 tokenizer）与无语义兜底产物都**随包分发**，无需手动导出。`tools/export_bge_onnx.py` 仅用于离线重导出（如更换量化档位），`tools/distill_nosem_router.py` 可复现兜底产物。

### 数据看板：`#/router`

配置页「SmartRouter 四档」卡片右上角的**「详情 →」**进入独立页面（`#/router`，导航里不单列）。一页回答三件事：**现在是什么状态、为什么、学习在推进吗**。数据全部来自本机（不联网、无额外埋点）：

| 块 | 来源 | 能看到 |
|---|---|---|
| 结论条 | 后端 `server/insights.py` 生成 | 一句结论 + 可操作建议，带原因码（如 `ml_nosem` / `gate_not_met` / `no_samples`） |
| 当前状态 | 运行中的共享资产（与对话同源）+ 产物文件 | 产物来源（`semantic` / `nosem` / `unavailable`）与原因码、产物内嵌的样本数与验证准确率、语义编码器可用性与原因码、上一版备份是否在位 |
| 每轮路由 | `events` 表的 `router.updated`（近 7 / 30 / 90 天） | 四档命中分布（与首页 token 面板**同一套档位色**）、置信度分布、路由耗时 p50/p95（首次加载与每轮分开）、强规则占比、换模型次数、失败原因 Top5、最近 30 轮明细（依据码按中文渲染） |
| 学习数据 | `calibration.json` / `learned_rules.json` / `feedback.log` / `sem_samples.jsonl` | 四档偏置（带 ±0.15 夹紧线与"样本量 / 20"进度）、自学习规则表、四类隐式信号的升降次数、样本库条数与体积、样本去噪明细 |
| 本地进化 | `evolve_state.json` / `evolve.log` | **五道门槛逐条进度**（还差多少条、几天后可再试）、决策历史（替换 / 被不劣门拒绝 / 门槛跳过 + 原因） |

三点要知道：

- 这一页**只读**：没有任何按钮能改状态。想改状态走 `/smartRouter evolve`（立刻试一次）或 `/smartRouter reset [--hard]`，结论条的建议会把你指到该去的命令。
- **空态是常态不是故障**：隐式信号只有四类行为会产生，首次自动进化通常在第 2-4 周；空态下页面会把"还差什么"逐条列成门槛进度，而不是显示"暂无数据"。
- 运行态统计**只看得到事件还在的部分**：事件会随时间被裁剪，接口会返回统计起点（`events_since`），达到抓取上限时页面会标注。
- 看板**不展示任何原文**：`feedback.log` 只有输入哈希与特征快照、样本库只有量化向量，接口也只返回计数与结构化码。

## 命令总览

后端斜杠命令（统一经 `handle_service_command` 分发）：

| 命令 | 说明 |
|------|------|
| `/plan <任务>` | 计划模式：先拆解为子任务 DAG，审阅后按轮执行（见下） |
| `/team <任务>` | Multi-Agent 模式：Supervisor 调度隔离 Worker，审查证据并定向修复（见下） |
| `/provider` | 管理服务商（增删改查、切 base、写 Key、维护模型列表，见「配置 Provider」） |
| `/model` | 查看当前模型，或在当前 provider 内切换模型（见「配置 Provider」） |
| `/smartRouter` | 智能路由总开关、状态、本地进化与重置（on / off / status / evolve / reset，见「SmartRouter 智能路由」） |
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
- **HITL 审批**：危险操作（默认 `execute_command` 与 `notes_delete` 必审、`write_file` / `notes_create` / `notes_update` 确认）执行前触发审批，由前端提交决定（批准 / 本会话全部放行 / 拒绝 / 改参后执行）。注意**「本会话全部放行」会跳过包括必审在内的所有审批**，这是既有语义；改参执行对笔记写工具尤其有用（可以就地修掉 agent 写错的措辞再批准）
- **策略层**：路径越界（PathGuard，含 symlink 逃逸）与黑名单命令（CommandGuard）直接拒绝，**不可被审批绕过**
- **审计日志**：所有工具调用/审批/拒绝记录到 `.routivus/audit.log`（JSONL，敏感字段脱敏）

内置工具：`read_file` / `write_file` / `list_dir` / `glob_files` / `grep_code` / `execute_command` / `web_search` / `web_fetch` / `load_skill` / `notes_list` / `notes_read` / `notes_create` / `notes_update` / `notes_delete`（按配置启用：web 两个需 web 配置、`load_skill` 需 Skill 启用、笔记五个需服务端注入笔记数据源（写工具还可用 `ROUTIVUS_NOTES_WRITE=off` 收掉）—— 笔记工具只作用于**当前项目**，读写边界见「笔记」一节）。

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

### Provider 能力上限（窗口 / 输出上限）

每个 provider 的能力上限存在 `config.json` 的 `providers.<name>`，**配置页可以直接改**（也可以手写文件）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `context_window` | `128000` | provider 默认窗口（token），用于上下文预算与界面使用率 |
| `model_limits` | `{}` | 按模型的覆盖 `{"<模型>": {"window": N, "max_output": M}}`；**只写例外**，没配的模型走 `context_window` |
| `max_output_tokens` | `0` | 单次输出上限；**0 = 不限制（不下发）** |
| `max_tokens_field` | `"max_tokens"` | 输出上限用哪个请求字段名发送；可换 `max_completion_tokens`，或留空表示该 provider 不发送 |

窗口解析优先级：`ROUTIVUS_CONTEXT_WINDOW` 环境变量 > `model_limits[模型].window` > `providers.<name>.context_window` > `128000`。
**窗口是模型级属性**，所以同一 provider 下不同模型可以有自己的值；换模型（`/model`）或智能路由换档后，界面上的窗口与使用率分母会随之变化，这是预期行为。

两点提醒：

- **输出上限默认不发送**：只有 `max_output_tokens > 0` 才会写进请求体。配了之后模型可能被硬截断（"说到一半停"），这是该配置的预期代价。
- **网关不认 `max_tokens`** 时请求会返回 400，错误信息里会直接给出改法（换成 `max_completion_tokens`，或选择不发送）。若请求成功但输出没被限制，可能是网关忽略了未知字段——会话侧栏会显示"实际发送的字段名"，可对照网关文档确认。

其余可用环境变量（均为可选进阶项，来自 `.env` / `.env.example`）：

| 环境变量 | 说明 |
|----------|------|
| `ROUTIVUS_CONTEXT_WINDOW` | 上下文窗口（token）；优先级最高，压过模型覆盖与 provider 默认（见「Provider 能力上限」） |
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
| `ROUTIVUS_ROUTER_TIMEOUT` | 智能路由（含首次模型加载）超时秒数（默认 120，下限 5）；超时只降级为「本轮不换档」 |
| `ROUTIVUS_ADAPTIVE_EVOLVE` | 本地自动演化开关（on 默认，off 关闭；手动 `/smartRouter evolve` 不受影响）。服务端进程里需为真实环境变量（见「Web Console Server」的警告），另见「本地进化与隐私」 |
| `ROUTIVUS_NOTES_WRITE` | 笔记写工具开关（on 默认，off 只剩两个只读笔记工具）。写仍需 HITL 审批，见「笔记」一节 |
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

项目分层：`routivus/agent`（ReAct 循环 + 计划模式）、`routivus/llm`（客户端抽象 + OpenAI 兼容实现 + 工厂）、`routivus/tool`（统一工具注册表 + 内置工具）、`routivus/mcp`（协议、transport、动态工具和 resources）、`routivus/skill`（Skill 发现、解析、按需加载与安全策略）、`routivus/input_history`（输入历史、游标、持久化与隐私策略）、`routivus/memory`（项目/长期记忆 + 上下文压缩）、`routivus/safety`（PathGuard / CommandGuard 与审计日志）、`routivus/web`（只读搜索与抓取，含 DNS / 重定向逐跳校验）、`routivus/ask`（`ask_user` 的载荷模型）、`routivus/tui`（纯逻辑编排：state / reducer / controller / i18n，无界面）、`routivus/cli`（命令服务层，无 REPL）、`routivus/service`（UI 无关的程序化命令入口）、`routivus/server`（REST + WebSocket 服务层、HITL 通道与项目终端通道）、`routivus/config`（provider/MCP/Web/Skill 配置与运行时快照）、`routivus/router`（SmartRouter 路由、校准与训练）、`routivus/adaptive`（本地样本库、训练内核与本地进化）、`routivus/assets`（随包 SmartRouter 产物：语义编码器、语义版与无语义兜底产物）。
