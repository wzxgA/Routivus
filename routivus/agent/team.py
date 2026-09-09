"""Multi-Agent Team 编排（第 10 期 MVP）。

Team 是建立在现有 Plan/ReAct 之上的协作控制面：
- Planner 生成带角色、资源范围和验收标准的任务 DAG；
- Supervisor 按依赖和资源冲突调度隔离上下文的 Worker；
- Reviewer 基于 Artifact 和执行证据做任务级审查；
- 失败任务生成有边界的 Repair Worker，最多重试两次。

Worker 仍然通过 ReActAgent -> ToolRegistry -> Guard/HITL -> Audit 执行，
Team 层不提供绕过现有安全链路的内部通道。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Literal, Protocol

from routivus.agent.plan import PlanError, ReviewDecision, build_batches
from routivus.agent.react import AgentEvent, DEFAULT_SYSTEM_PROMPT, ReActAgent
from routivus.config.settings import Settings
from routivus.llm.client import LlmClient, LlmError
from routivus.llm.types import Message, ToolCall, ToolResult, Usage
from routivus.memory.context import ConversationContext
from routivus.memory.manager import MemoryManager
from routivus.safety.hitl import HITLPolicy
from routivus.tool.registry import ToolRegistry

if TYPE_CHECKING:
    from routivus.mcp.manager import McpManager


TEAM_MAX_RETRIES = 2
TEAM_MAX_RECOVERIES = 1
TEAM_RECOVERY_STEPS = 10
TEAM_RESULT_LIMIT = 2000
TEAM_ARTIFACT_LIMIT = 4000
TEAM_REVIEW_LIMIT = 4000
TEAM_PLAN_MAX_RETRIES = 2
TEAM_REVIEW_OUTPUT_RETRIES = 1
RESOURCE_SCOPED_TOOLS = {"read_file", "write_file", "list_dir", "glob_files", "grep_code"}
READ_DISCOVERY_TOOLS = {"read_file", "list_dir", "glob_files", "grep_code"}
READ_DISCOVERY_ROLES = {"researcher", "reviewer"}
CANONICAL_TEAM_TOOLS = frozenset({
    "read_file", "write_file", "list_dir", "glob_files", "grep_code",
    "execute_command", "web_search", "web_fetch", "load_skill",
})
TEAM_TOOL_ALIASES = {
    "find": "glob_files",
    "glob": "glob_files",
    "grep": "grep_code",
    "read": "read_file",
    "write": "write_file",
}
DEFAULT_RESOURCE_DENY_PATTERNS = (
    ".env",
    ".env.*",
    "**/*.pem",
    "**/*.key",
    "**/*secret*",
    "**/*credential*",
    "**/*password*",
    ".xg/memory.db",
    ".xg/audit.log",
)


TEAM_PLANNER_PROMPT = (
    "你是 XG 的团队任务规划器。将用户任务拆解为可执行的 DAG，"
    "为每个任务指定角色、工具范围、资源范围和可验证的验收标准。"
    "只输出一个 JSON 对象，不要输出其他文本或 markdown，格式：\n"
    "{\"tasks\": [{\"id\": \"t1\", \"title\": \"一句话标题\", "
    "\"description\": \"执行说明\", \"deps\": [], "
    "\"owner_role\": \"coder\", "
    "\"allowed_tools\": [\"read_file\", \"write_file\"], "
    "\"resource_scope_mode\": \"targeted\", "
    "\"resource_claims\": [{\"pattern\": \"src/*\", "
    "\"access\": \"write\", \"exclusive\": false}], "
    "\"acceptance_criteria\": [\"可验证条件\"]}]}\n"
    "规则：\n"
    "- id 全局唯一，形如 t1/t2；deps 只能引用其他任务 id；\n"
    "- 依赖必须是无环 DAG；\n"
    "- owner_role 使用 coder、researcher、tester、reviewer 或 repairer；\n"
    "- allowed_tools 必须使用 XG 注册的精确工具名：read_file、write_file、list_dir、glob_files、grep_code、execute_command、web_search、web_fetch、load_skill；\n"
    "- 不要输出 find、glob、grep、cat、shell、bash、terminal、read 或 write 作为工具名；\n"
    "- 读取文件使用 read_file，查看目录使用 list_dir，按模式查找文件使用 glob_files，搜索代码使用 grep_code，执行测试/命令使用 execute_command；\n"
    "- 只读任务不得声明 write 工具；\n"
    "- researcher/reviewer 需要先探索项目结构时使用 resource_scope_mode=read_discovery；\n"
    "- coder/tester/repairer 使用 resource_scope_mode=targeted，写入范围必须声明；\n"
    "- read_discovery 只允许项目根目录内的只读工具，不得声明 write 资源；\n"
    "- 无法判断命令副作用时使用 exclusive=true；\n"
    "- 每个任务必须有至少一条 acceptance_criteria。"
)

TEAM_REVIEWER_PROMPT = (
    "你是严格的任务审查 Agent。你不能修改文件，只能根据任务验收标准、"
    "实际工具结果和任务产物判断是否通过。只输出 JSON："
    '{"verdict":"pass|fail|needs_input","findings":["问题"],'
    '"required_fixes":["定向修复要求"],'
    '"repair_scope":[{"pattern":"path/to/file","access":"write"}],'
    '"evidence":["证据"]}。'
    "verdict 为 fail 时，尽量提供最小的 repair_scope；"
    "不要把原任务的只读范围自动升级为写入范围。"
    "不要把 Worker 的主观汇报当成测试通过证据。"
)


@dataclass
class ResourceClaim:
    """任务对项目资源的访问声明。"""

    pattern: str
    access: Literal["read", "write"] = "read"
    exclusive: bool = False

    def normalized(self) -> str:
        pattern = self.pattern.replace("\\", "/").strip()
        while pattern.startswith("./"):
            pattern = pattern[2:]
        return pattern or "**"


@dataclass
class AgentProfile:
    """一个可注册的 Agent 角色配置。"""

    name: str
    system_prompt: str
    allowed_tools: tuple[str, ...] = ()  # 空 tuple 表示使用所有已注册工具
    default_model: str | None = None
    max_steps: int | None = None
    can_write: bool = False
    is_reviewer: bool = False


@dataclass
class Artifact:
    """Worker 产生的可传递、可验证任务产物。"""

    id: str
    task_id: str
    kind: str
    uri: str = ""
    summary: str = ""
    checksum: str = ""
    producer_agent_id: str = ""
    version: int = 1
    attempt: int = 1
    parent_artifacts: list[str] = field(default_factory=list)
    verification_records: list[str] = field(default_factory=list)


@dataclass
class ReviewResult:
    task_id: str
    verdict: Literal["pass", "fail", "needs_input"]
    findings: list[str] = field(default_factory=list)
    required_fixes: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    repair_scope: list[ResourceClaim] = field(default_factory=list)
    category: str = ""


@dataclass(frozen=True)
class ReviewOutputError:
    """Reviewer 输出无法安全转换为 ReviewResult 时的结构化错误。"""

    category: str
    message: str


@dataclass
class TeamTask:
    id: str
    title: str
    description: str
    deps: list[str]
    owner_role: str = "coder"
    allowed_tools: list[str] = field(default_factory=list)
    allowed_tools_declared: bool = False
    invalid_tools: list[str] = field(default_factory=list)
    tool_warnings: list[str] = field(default_factory=list)
    resource_claims: list[ResourceClaim] = field(default_factory=list)
    resource_scope_mode: Literal["targeted", "read_discovery"] = "targeted"
    resource_deny_patterns: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    input_artifacts: list[str] = field(default_factory=list)
    output_artifacts: list[str] = field(default_factory=list)
    status: str = "pending"
    attempts: int = 0
    result: str = ""
    artifacts: list[str] = field(default_factory=list)
    failure_category: str = ""
    blocked_by: list[str] = field(default_factory=list)
    recovery_attempts: int = 0
    repair_attempts_started: int = 0
    repair_attempts_blocked: int = 0
    pending_input_category: str = ""
    pending_input_message: str = ""
    pending_repair_scope: list[ResourceClaim] = field(default_factory=list)
    pending_review: ReviewResult | None = None


@dataclass
class TeamPlan:
    goal: str
    tasks: list[TeamTask]
    batches: list[list[str]]

    def task_by_id(self, task_id: str) -> TeamTask | None:
        return next((task for task in self.tasks if task.id == task_id), None)


@dataclass
class TeamEvent:
    """Team 层事件；内部 AgentEvent 通过 agent_event 嵌套转发。"""

    kind: Literal[
        "team_started", "team_plan_generated", "team_review", "approved",
        "replanned", "batch_started", "task_started", "task_done",
        "task_failed", "agent_started", "agent_done", "agent_failed",
        "task_blocked",
        "task_retry_started",
        "subtask_event", "artifact_produced", "task_review_started",
        "task_review_done", "repair_requested", "team_done", "team_failed",
        "review_output_invalid", "review_output_retry", "repair_scope_required",
        "repair_scope_validated", "task_needs_input", "task_resume_requested",
        "cancelled", "team_resume_requested",
    ]
    team_id: str = ""
    plan: TeamPlan | None = None
    batch: list[str] = field(default_factory=list)
    task: TeamTask | None = None
    agent_id: str = ""
    role: str = ""
    artifact: Artifact | None = None
    review: ReviewResult | None = None
    agent_event: AgentEvent | None = None
    message: str = ""
    usage: Usage | None = None
    attempt: int = 0
    effective_steps: int = 0
    failure_category: str = ""
    retryable: bool = False
    previous_steps: int = 0
    retry_steps: int = 0
    preserved_artifacts: list[str] = field(default_factory=list)
    scope_claims: list[ResourceClaim] = field(default_factory=list)
    repair_attempts_started: int = 0
    repair_attempts_blocked: int = 0


class Planner(Protocol):
    async def create_plan(self, goal: str) -> TeamPlan: ...


class AgentFactory(Protocol):
    def create(self, profile: AgentProfile, task: TeamTask) -> ReActAgent: ...


class ArtifactStore(Protocol):
    async def publish(self, artifact: Artifact) -> None: ...
    async def get(self, artifact_id: str) -> Artifact | None: ...
    async def for_task(self, task_id: str) -> list[Artifact]: ...


class Reviewer(Protocol):
    async def review(self, task: TeamTask, artifacts: list[Artifact]) -> ReviewResult: ...


class Scheduler(Protocol):
    async def schedule(self, plan: TeamPlan) -> list[list[str]]: ...


class InMemoryArtifactStore:
    """MVP 的进程内 ArtifactStore；后续可替换为 SQLite 或文件实现。"""

    def __init__(self) -> None:
        self._items: dict[str, Artifact] = {}

    async def publish(self, artifact: Artifact) -> None:
        self._items[artifact.id] = artifact

    async def get(self, artifact_id: str) -> Artifact | None:
        return self._items.get(artifact_id)

    async def for_task(self, task_id: str) -> list[Artifact]:
        return [item for item in self._items.values() if item.task_id == task_id]

    async def get_many(self, artifact_ids: list[str]) -> list[Artifact]:
        return [self._items[item_id] for item_id in artifact_ids if item_id in self._items]


class ScopedToolRegistry:
    """给 Worker 暴露工具和资源范围的受限视图。"""

    def __init__(self, base: ToolRegistry, task: TeamTask, project_root: Path, profile: AgentProfile) -> None:
        self._base = base
        self._task = task
        self._project_root = project_root.resolve()
        self._profile = profile

    def schemas(self) -> list[dict]:
        schemas = self._base.schemas()
        allowed = self._allowed_tools()
        if self._task.allowed_tools_declared and not allowed:
            return []
        if not allowed:
            return schemas
        return [schema for schema in schemas if schema.get("name") in allowed]

    async def aexecute_calls(self, calls: list[ToolCall], concurrency: int = 4, timeout: float = 120.0) -> list[ToolResult]:
        allowed = self._allowed_tools()
        executable: list[ToolCall] = []
        rejected: dict[str, ToolResult] = {}
        for call in calls:
            if self._task.allowed_tools_declared and not allowed:
                rejected[call.id] = ToolResult(
                    tool_call_id=call.id, name=call.name, ok=False,
                    error=f"任务未允许任何工具调用: {call.name}",
                )
                continue
            if allowed and call.name not in allowed:
                rejected[call.id] = ToolResult(
                    tool_call_id=call.id, name=call.name, ok=False,
                    error=f"角色 {self._profile.name} 不允许调用工具: {call.name}",
                )
                continue
            if not self._resource_allowed(call):
                path = call.parsed_arguments().get("path", "<项目根目录>")
                rejected[call.id] = ToolResult(
                    tool_call_id=call.id, name=call.name, ok=False,
                    error=(
                        f"任务资源范围拒绝工具调用: {call.name} "
                        f"(path={path}, mode={self._task.resource_scope_mode})"
                    ),
                )
                continue
            executable.append(call)
        results = await self._base.aexecute_calls(executable, concurrency=concurrency, timeout=timeout)
        by_id = {result.tool_call_id: result for result in results}
        by_id.update(rejected)
        output: list[ToolResult] = []
        for call in calls:
            result = by_id.get(call.id) or ToolResult(
                tool_call_id=call.id, name=call.name, ok=False, error="工具未执行"
            )
            if result.ok and self._task.resource_scope_mode == "read_discovery":
                result = self._filter_discovery_result(call, result)
            output.append(result)
        return output

    def _allowed_tools(self) -> set[str]:
        profile_tools = set(self._profile.allowed_tools)
        task_tools = set(self._task.allowed_tools)
        if self._task.allowed_tools_declared and not task_tools:
            return set()
        if profile_tools and task_tools:
            return profile_tools & task_tools
        return profile_tools or task_tools

    def _resource_allowed(self, call: ToolCall) -> bool:
        claims = self._task.resource_claims
        if call.name not in RESOURCE_SCOPED_TOOLS:
            return True
        args = call.parsed_arguments()
        relative = self._normalize_target(args.get("path"))
        if relative is None:
            return False
        if self._task.resource_scope_mode == "read_discovery":
            return (
                self._profile.name in READ_DISCOVERY_ROLES
                and not self._profile.can_write
                and call.name in READ_DISCOVERY_TOOLS
                and not self._is_denied(relative)
            )
        if not claims:
            return False
        wants_write = call.name == "write_file"
        for claim in claims:
            pattern = claim.normalized()
            matches = self._claim_matches(relative, pattern)
            if matches and (not wants_write or claim.access == "write"):
                return True
        return False

    def _normalize_target(self, raw_value: object) -> str | None:
        """Normalize an optional tool path relative to the project root."""
        raw = "" if raw_value is None else str(raw_value).strip()
        if not raw or raw in {".", "./", ".\\"}:
            return ""
        path = Path(raw)
        try:
            resolved = path.resolve() if path.is_absolute() else (self._project_root / path).resolve()
            return resolved.relative_to(self._project_root).as_posix()
        except ValueError:
            return None

    @staticmethod
    def _claim_matches(relative: str, pattern: str) -> bool:
        if not relative:
            return pattern in {"*", "**"}
        if fnmatch.fnmatch(relative, pattern) or (
            pattern.startswith("**/") and fnmatch.fnmatch(relative, pattern[3:])
        ):
            return True
        prefix = pattern.rstrip("/*").rstrip("/")
        if prefix and (relative == prefix or relative.startswith(prefix + "/")):
            return True
        return fnmatch.fnmatch(relative, pattern.rstrip("/") + "/**")

    def _is_denied(self, relative: str) -> bool:
        patterns = (*DEFAULT_RESOURCE_DENY_PATTERNS, *self._task.resource_deny_patterns)
        return any(
            self._claim_matches(relative, pattern.replace("\\", "/"))
            for pattern in patterns
        )

    def _filter_discovery_result(self, call: ToolCall, result: ToolResult) -> ToolResult:
        """Remove sensitive paths from discovery tool output before refeeding it."""
        if call.name == "list_dir":
            root = self._normalize_target(call.parsed_arguments().get("path"))
            lines = []
            for line in result.output.splitlines():
                name = line.removeprefix("[dir] ").strip()
                candidate = f"{root}/{name}" if root else name
                if not self._is_denied(candidate):
                    lines.append(line)
            return replace(result, output="\n".join(lines) or "(结果已按安全策略过滤)")
        if call.name in {"glob_files", "grep_code"}:
            lines = []
            for line in result.output.splitlines():
                candidate = line
                if call.name == "grep_code" and ":" in line:
                    candidate = line.split(":", 1)[0]
                if not self._is_denied(candidate.replace("\\", "/").strip()):
                    lines.append(line)
            return replace(result, output="\n".join(lines) or "(结果已按安全策略过滤)")
        return result

    def __getattr__(self, name: str):
        return getattr(self._base, name)


def build_repair_scope(
    original_task: TeamTask,
    review: ReviewResult,
) -> tuple[list[ResourceClaim], list[str]]:
    """根据审查结果生成 Repairer 的最小写入范围。

    Reviewer 明确给出的范围优先。为了兼容旧版 Reviewer，原任务已有的
    write claim 可以作为回退；原任务的 read claim 永远不会被升级为 write。
    """
    explicit = [
        ResourceClaim(claim.pattern, "write", claim.exclusive)
        for claim in review.repair_scope
        if claim.access == "write" and claim.pattern.strip()
    ]
    if explicit:
        return explicit, []

    inherited = [
        ResourceClaim(claim.pattern, "write", claim.exclusive)
        for claim in original_task.resource_claims
        if claim.access == "write" and claim.pattern.strip()
    ]
    if inherited:
        return inherited, ["Reviewer 未提供 repair_scope，已兼容使用原任务的 write claim"]

    return [], ["Reviewer 未提供可安全写入的 repair_scope"]


def _resource_claims_from_json(raw: object) -> list[ResourceClaim]:
    """Parse the optional structured repair scope from Reviewer JSON."""
    if not isinstance(raw, list):
        return []
    claims: list[ResourceClaim] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pattern = item.get("pattern")
        access = item.get("access")
        if isinstance(pattern, str) and isinstance(access, str):
            pattern = pattern.strip()
            access = access.strip().lower()
        if isinstance(pattern, str) and pattern and access in {"read", "write"}:
            claims.append(ResourceClaim(pattern, access, bool(item.get("exclusive", False))))
    return claims


def _safe_claim_pattern(pattern: str) -> bool:
    """Return whether a claim pattern can stay within the project root."""
    normalized = pattern.replace("\\", "/").strip()
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return False
    return not any(part == ".." for part in normalized.split("/"))


def _claim_overlaps_pattern(claim: str, protected: str) -> bool:
    """Conservatively detect a claim that may include a protected path."""
    return (
        fnmatch.fnmatch(claim, protected)
        or fnmatch.fnmatch(protected, claim)
        or ScopedToolRegistry._claim_matches(claim, protected)
        or ScopedToolRegistry._claim_matches(protected, claim)
    )


def validate_task_resource_policy(
    task: TeamTask,
    profile: AgentProfile,
    project_root: Path | None = None,
) -> list[str]:
    """Validate the role/tool/resource combination before starting a Worker."""
    errors: list[str] = []
    mode = task.resource_scope_mode
    profile_tools = set(profile.allowed_tools)
    task_tools = set(task.allowed_tools)

    if task.invalid_tools:
        errors.append(f"计划包含无效工具：{', '.join(task.invalid_tools)}")
    if mode not in {"targeted", "read_discovery"}:
        errors.append(f"未知资源模式：{mode}")
    if mode == "read_discovery":
        if profile.name not in READ_DISCOVERY_ROLES or profile.can_write:
            errors.append(f"角色 {profile.name} 不能使用 read_discovery")
        if any(claim.access == "write" for claim in task.resource_claims):
            errors.append("read_discovery 不能包含 write claim")
        if task_tools and any(tool not in READ_DISCOVERY_TOOLS for tool in task_tools):
            errors.append("read_discovery 只能使用只读发现工具")
    elif profile.can_write and task.owner_role in {"coder", "tester", "repairer"}:
        # Writable roles must never be put into the discovery-only policy.
        if mode != "targeted":
            errors.append(f"可写角色 {profile.name} 必须使用 targeted")

    if profile_tools and task_tools:
        unknown = sorted(task_tools - profile_tools)
        if unknown:
            errors.append(f"任务工具超出角色权限：{', '.join(unknown)}")

    write_claims = [claim for claim in task.resource_claims if claim.access == "write"]
    if task.owner_role == "repairer" and not write_claims:
        errors.append("repair_scope_missing：Repairer 没有明确的 write claim")

    protected_patterns = DEFAULT_RESOURCE_DENY_PATTERNS + tuple(task.resource_deny_patterns)
    for claim in task.resource_claims:
        normalized = claim.normalized()
        if not _safe_claim_pattern(normalized):
            errors.append(f"资源声明越出项目根目录：{claim.pattern}")
        if claim.access == "write" and any(
            _claim_overlaps_pattern(normalized, protected.replace("\\", "/"))
            for protected in protected_patterns
        ):
            errors.append(f"write claim 命中受保护资源：{claim.pattern}")
        if task.owner_role == "repairer" and normalized in {"*", "**"}:
            errors.append("Repairer 不允许使用全项目写入范围")

    # Keep the optional parameter part of the validation contract so callers can
    # pass the project root now and path-specific checks can be extended without
    # changing the Worker startup API.
    _ = project_root
    return list(dict.fromkeys(errors))


def normalize_team_tool_names(
    raw_tools: object,
    profile: AgentProfile,
) -> tuple[list[str], list[str]]:
    """Normalize Planner tool names to registered XG names.

    Only aliases with an unambiguous, non-escalating meaning are accepted.
    Role and resource policy checks remain separate and are applied afterwards.
    """
    if not isinstance(raw_tools, list):
        return [], []
    tools: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for raw in raw_tools:
        original = str(raw).strip()
        if not original:
            continue
        name = original.lower()
        canonical = TEAM_TOOL_ALIASES.get(name, name)
        if canonical != name:
            warnings.append(f"{original} 已转换为 {canonical}")
        if canonical not in CANONICAL_TEAM_TOOLS:
            warnings.append(f"{original} 不是已注册工具")
            continue
        if profile.allowed_tools and canonical not in profile.allowed_tools:
            warnings.append(f"{canonical} 超出角色 {profile.name} 的工具权限")
        if canonical not in seen:
            tools.append(canonical)
            seen.add(canonical)
    return tools, warnings


def default_profiles() -> dict[str, AgentProfile]:
    all_tools = ()
    readonly = ("read_file", "list_dir", "glob_files", "grep_code", "web_search", "web_fetch", "load_skill")
    writable = ("read_file", "write_file", "list_dir", "glob_files", "grep_code", "execute_command", "web_search", "web_fetch", "load_skill")
    return {
        "coder": AgentProfile(
            name="coder",
            system_prompt="你是一名谨慎的代码实现 Agent。只处理当前任务，先阅读相关代码，再实现并验证；不要处理其他任务。",
            allowed_tools=writable,
            max_steps=12,
            can_write=True,
        ),
        "researcher": AgentProfile(
            name="researcher",
            system_prompt="你是一名研究 Agent。只读取和分析资料，输出有来源的结论，不修改项目文件。",
            allowed_tools=readonly,
            max_steps=20,
        ),
        "tester": AgentProfile(
            name="tester",
            system_prompt="你是一名测试 Agent。负责编写或执行当前任务的测试，并准确报告测试命令和结果。",
            allowed_tools=writable,
            max_steps=12,
            can_write=True,
        ),
        "reviewer": AgentProfile(
            name="reviewer",
            system_prompt=TEAM_REVIEWER_PROMPT,
            allowed_tools=readonly,
            max_steps=10,
            is_reviewer=True,
        ),
        "repairer": AgentProfile(
            name="repairer",
            system_prompt="你是一名定向修复 Agent。只修复 Reviewer 列出的 required_fixes，不扩大任务范围。",
            allowed_tools=writable,
            max_steps=12,
            can_write=True,
        ),
        "synthesizer": AgentProfile(
            name="synthesizer",
            system_prompt="你是一名结果汇总 Agent。只汇总已经验证的任务产物，不修改项目文件。",
            allowed_tools=all_tools,
            max_steps=8,
        ),
    }


def _strip_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[\w-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def _review_string_list(data: dict[str, object], field_name: str) -> list[str]:
    raw = data.get(field_name, [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f"{field_name} 必须是字符串数组")
    return [item.strip() for item in raw if item.strip()]


def parse_review_output(
    task_id: str,
    raw: str,
) -> ReviewResult | ReviewOutputError:
    """严格解析 Reviewer JSON，避免非法输出触发 Repairer。"""
    if not raw.strip():
        return ReviewOutputError("review_output_empty", "Reviewer 返回了空内容")
    try:
        data = json.loads(_strip_json(raw))
    except json.JSONDecodeError as exc:
        return ReviewOutputError("review_output_not_json", f"Reviewer 输出不是合法 JSON：{exc}")
    if not isinstance(data, dict):
        return ReviewOutputError("review_output_wrong_shape", "Reviewer 输出顶层必须是 JSON 对象")

    verdict = data.get("verdict")
    if verdict not in {"pass", "fail", "needs_input"}:
        return ReviewOutputError("review_verdict_invalid", "verdict 必须是 pass、fail 或 needs_input")
    try:
        findings = _review_string_list(data, "findings")
        required_fixes = _review_string_list(data, "required_fixes")
        evidence = _review_string_list(data, "evidence")
    except ValueError as exc:
        return ReviewOutputError("review_output_wrong_shape", str(exc))

    raw_scope = data.get("repair_scope", [])
    if not isinstance(raw_scope, list):
        return ReviewOutputError("review_scope_invalid", "repair_scope 必须是对象数组")
    claims: list[ResourceClaim] = []
    for index, item in enumerate(raw_scope):
        if not isinstance(item, dict):
            return ReviewOutputError("review_scope_invalid", f"repair_scope[{index}] 必须是对象")
        pattern = item.get("pattern")
        access = item.get("access")
        exclusive = item.get("exclusive", False)
        if not isinstance(pattern, str) or not pattern.strip():
            return ReviewOutputError("review_scope_invalid", f"repair_scope[{index}].pattern 无效")
        if access not in {"read", "write"}:
            return ReviewOutputError("review_scope_invalid", f"repair_scope[{index}].access 无效")
        if not isinstance(exclusive, bool):
            return ReviewOutputError("review_scope_invalid", f"repair_scope[{index}].exclusive 必须是布尔值")
        claims.append(ResourceClaim(pattern.strip(), access, exclusive))

    return ReviewResult(
        task_id=task_id,
        verdict=verdict,  # type: ignore[arg-type]
        findings=findings,
        required_fixes=required_fixes,
        evidence=evidence,
        repair_scope=claims,
    )


def parse_team_tasks(
    raw: str,
    max_tasks: int = 12,
    profiles: dict[str, AgentProfile] | None = None,
) -> tuple[list[TeamTask], list[str]]:
    """解析并校验 Team Planner 输出。"""
    try:
        data = json.loads(_strip_json(raw))
    except json.JSONDecodeError as exc:
        raise PlanError(f"Team 计划 JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
        raise PlanError('Team 计划顶层结构必须是 {"tasks": [...]} 且不能为空')

    profiles = profiles or default_profiles()
    tasks: list[TeamTask] = []
    warnings: list[str] = []
    for index, item in enumerate(data["tasks"]):
        if not isinstance(item, dict):
            raise PlanError(f"tasks[{index}] 必须是对象")
        task_id = str(item.get("id", "")).strip()
        title = str(item.get("title", "")).strip()
        if not task_id or not title:
            raise PlanError(f"tasks[{index}] 缺少 id 或 title")
        deps_raw = item.get("deps", [])
        if not isinstance(deps_raw, list):
            raise PlanError(f"tasks[{index}].deps 必须是数组")
        deps = list(dict.fromkeys(str(dep).strip() for dep in deps_raw if str(dep).strip()))
        role = str(item.get("owner_role") or "coder").strip().lower()
        if role not in profiles:
            warnings.append(f"任务 {task_id} 使用未知角色 {role}，已回退为 coder")
            role = "coder"
        allowed_raw = item.get("allowed_tools", [])
        allowed, tool_warnings = normalize_team_tool_names(allowed_raw, profiles[role])
        warnings.extend(f"任务 {task_id}：{warning}" for warning in tool_warnings)
        invalid_tools: list[str] = []
        if isinstance(allowed_raw, list):
            for raw_name in allowed_raw:
                original = str(raw_name).strip()
                canonical = TEAM_TOOL_ALIASES.get(original.lower(), original.lower())
                if canonical not in CANONICAL_TEAM_TOOLS or (
                    profiles[role].allowed_tools and canonical not in profiles[role].allowed_tools
                ):
                    if original and original not in invalid_tools:
                        invalid_tools.append(original)
        claims_raw = item.get("resource_claims", [])
        claims: list[ResourceClaim] = []
        if isinstance(claims_raw, list):
            for claim in claims_raw:
                if not isinstance(claim, dict):
                    continue
                pattern = str(claim.get("pattern", "")).strip()
                access = str(claim.get("access", "read")).strip().lower()
                if pattern and access in {"read", "write"}:
                    claims.append(ResourceClaim(pattern, access, bool(claim.get("exclusive", False))))
        mode_raw = str(item.get("resource_scope_mode", "")).strip().lower()
        if mode_raw not in {"targeted", "read_discovery"}:
            if mode_raw:
                warnings.append(f"任务 {task_id} 使用未知资源模式 {mode_raw}，已回退为 targeted")
            mode = (
                "read_discovery"
                if role in READ_DISCOVERY_ROLES and not any(claim.access == "write" for claim in claims)
                else "targeted"
            )
        else:
            mode = mode_raw
        if mode == "read_discovery" and (
            role not in READ_DISCOVERY_ROLES
            or any(claim.access == "write" for claim in claims)
        ):
            warnings.append(f"任务 {task_id} 的 read_discovery 与角色或写入声明冲突，已回退为 targeted")
            mode = "targeted"
        deny_raw = item.get("resource_deny_patterns", [])
        deny_patterns = (
            [str(pattern).strip() for pattern in deny_raw if str(pattern).strip()]
            if isinstance(deny_raw, list) else []
        )
        criteria_raw = item.get("acceptance_criteria", [])
        criteria = [str(value).strip() for value in criteria_raw if str(value).strip()] if isinstance(criteria_raw, list) else []
        if not criteria:
            criteria = [f"完成任务：{title}"]
            warnings.append(f"任务 {task_id} 缺少验收标准，已使用标题作为最低验收标准")
        description = str(item.get("description") or title).strip()
        tasks.append(TeamTask(
            id=task_id,
            title=title,
            description=description,
            deps=deps,
            owner_role=role,
            allowed_tools=allowed,
            allowed_tools_declared="allowed_tools" in item,
            invalid_tools=invalid_tools,
            tool_warnings=tool_warnings,
            resource_claims=claims,
            resource_scope_mode=mode,  # type: ignore[arg-type]
            resource_deny_patterns=deny_patterns,
            acceptance_criteria=criteria,
            input_artifacts=[str(value) for value in item.get("input_artifacts", []) if str(value)] if isinstance(item.get("input_artifacts", []), list) else [],
            output_artifacts=[str(value) for value in item.get("output_artifacts", []) if str(value)] if isinstance(item.get("output_artifacts", []), list) else [],
        ))

    if len({task.id for task in tasks}) != len(tasks):
        raise PlanError("Team 计划存在重复的任务 id")
    if len(tasks) > max_tasks:
        warnings.append(f"Team 任务数 {len(tasks)} 超过上限 {max_tasks}，已截断")
        kept = {task.id for task in tasks[:max_tasks]}
        tasks = tasks[:max_tasks]
        for task in tasks:
            task.deps = [dep for dep in task.deps if dep in kept]
    known = {task.id for task in tasks}
    for task in tasks:
        for dep in list(task.deps):
            if dep == task.id:
                task.deps.remove(dep)
                warnings.append(f"任务 {task.id} 自依赖，已移除")
            elif dep not in known:
                task.deps.remove(dep)
                warnings.append(f"任务 {task.id} 引用了不存在的依赖 {dep}，已移除")
    try:
        build_batches(tasks)  # TeamTask 使用同样的 id/deps 接口
    except PlanError as exc:
        raise PlanError(f"Team 依赖无效：{exc}") from exc
    return tasks, warnings


def _resource_conflicts(left: TeamTask, right: TeamTask) -> bool:
    for a in left.resource_claims:
        for b in right.resource_claims:
            if not (a.exclusive or b.exclusive or a.access == "write" or b.access == "write"):
                continue
            pa, pb = a.normalized(), b.normalized()
            if fnmatch.fnmatch(pa, pb) or fnmatch.fnmatch(pb, pa) or pa.startswith(pb.rstrip("*").rstrip("/")) or pb.startswith(pa.rstrip("*").rstrip("/")):
                return True
    # 未声明资源的写入任务保守处理：无法知道它是否修改了相同资源。
    return bool(not left.resource_claims and not right.resource_claims and _task_may_write(left) and _task_may_write(right))


def _task_may_write(task: TeamTask) -> bool:
    if task.owner_role in {"coder", "tester", "repairer"}:
        return True
    return any(claim.access == "write" for claim in task.resource_claims)


def conflict_safe_batches(tasks: list[TeamTask]) -> list[list[str]]:
    """在 DAG 批次上进一步按资源冲突做确定性串行化。"""
    raw_batches = build_batches(tasks)
    by_id = {task.id: task for task in tasks}
    output: list[list[str]] = []
    for batch in raw_batches:
        safe: list[str] = []
        for task_id in batch:
            task = by_id[task_id]
            conflict_index = next(
                (index for index, existing_id in enumerate(safe) if _resource_conflicts(task, by_id[existing_id])),
                None,
            )
            if conflict_index is None:
                safe.append(task_id)
                continue
            # 把冲突任务放入新批次；保持确定性和原有排序。
            output.append(safe)
            safe = [task_id]
        if safe:
            output.append(safe)
    return output


TaskReviewCallback = Callable[[TeamTask, list[Artifact]], Awaitable[ReviewResult]]


class TeamExecutor:
    """Team 计划生成、调度、Worker 执行、审查和修复。"""

    def __init__(
        self,
        llm: LlmClient,
        tools: ToolRegistry,
        settings: Settings,
        reviewer: Callable[[TeamPlan], Awaitable[ReviewDecision]] | None = None,
        task_reviewer: TaskReviewCallback | None = None,
        approval_policy: HITLPolicy | None = None,
        audit=None,
        memory_manager: MemoryManager | None = None,
        mcp_manager: "McpManager | None" = None,
        profiles: dict[str, AgentProfile] | None = None,
        artifact_store: ArtifactStore | None = None,
        project_root: Path | None = None,
        team_id: str | None = None,
        agent_factory: AgentFactory | None = None,
        ask_requester=None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.settings = settings
        self.reviewer = reviewer
        self.task_reviewer = task_reviewer
        self.approval_policy = approval_policy
        self.audit = audit
        self.memory_manager = memory_manager
        self.mcp_manager = mcp_manager
        self.ask_requester = ask_requester
        self.profiles = profiles or default_profiles()
        self.artifacts: ArtifactStore = artifact_store or InMemoryArtifactStore()
        self.project_root = (project_root or Path.cwd()).resolve()
        self.team_id = team_id or f"team-{uuid.uuid4().hex[:8]}"
        self.agent_factory = agent_factory
        self._last_plan: TeamPlan | None = None

    async def run(self, goal: str) -> AsyncIterator[TeamEvent]:
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.ensure_started()
                goal = await self.mcp_manager.expand_references(goal)
            except Exception as exc:
                yield TeamEvent(kind="team_failed", team_id=self.team_id, message=f"MCP resource 处理失败: {exc}")
                return

        yield TeamEvent(kind="team_started", team_id=self.team_id, message=goal)
        self._audit("team_started", team_id=self.team_id, goal=goal)
        try:
            plan, warnings = await self._generate_plan(goal)
        except LlmError as exc:
            yield TeamEvent(kind="team_failed", team_id=self.team_id, message=f"团队计划生成失败: {exc}")
            return
        if plan is None:
            yield TeamEvent(kind="team_failed", team_id=self.team_id, message="团队计划生成失败，建议改用 /plan 或普通 ReAct。")
            return

        yield TeamEvent(kind="team_plan_generated", team_id=self.team_id, plan=plan, message="；".join(warnings))
        if self.reviewer is None:
            yield TeamEvent(kind="cancelled", team_id=self.team_id, plan=plan, message="无计划审阅回调，Team 自动取消（fail closed）")
            return
        yield TeamEvent(kind="team_review", team_id=self.team_id, plan=plan, message="等待用户审阅团队计划")
        decision = await self.reviewer(plan)
        if decision.action == "cancel":
            yield TeamEvent(kind="cancelled", team_id=self.team_id, plan=plan, message="用户取消团队计划")
            return
        if decision.action == "replan":
            # MVP 只在首次审阅阶段支持一次显式重规划循环，保持和 /plan 一致。
            yield TeamEvent(kind="replanned", team_id=self.team_id, plan=plan, message=decision.feedback)
            try:
                plan, warnings = await self._generate_plan(goal, decision.feedback, plan)
            except LlmError as exc:
                yield TeamEvent(kind="team_failed", team_id=self.team_id, message=f"团队重规划失败: {exc}")
                return
            if plan is None:
                yield TeamEvent(kind="team_failed", team_id=self.team_id, message="团队重规划失败，建议改用 /plan 或普通 ReAct。")
                return
            yield TeamEvent(kind="team_plan_generated", team_id=self.team_id, plan=plan, message="；".join(warnings))
            yield TeamEvent(kind="team_review", team_id=self.team_id, plan=plan, message="等待用户审阅重规划")
            decision = await self.reviewer(plan)
            if decision.action != "execute":
                yield TeamEvent(kind="cancelled", team_id=self.team_id, plan=plan, message="团队计划未获批准")
                return

        yield TeamEvent(kind="approved", team_id=self.team_id, plan=plan, message="团队计划已批准，开始执行")
        self._last_plan = plan
        plan.batches = conflict_safe_batches(plan.tasks)
        async for event in self._execute_plan_batches(plan):
            yield event

    async def _execute_plan_batches(self, plan: TeamPlan, instruction: str = "") -> AsyncIterator[TeamEvent]:
        """按批次执行团队任务；遇 needs_input/failed/超限终止。instruction 注入每个被执行 worker。"""
        for batch_number, batch in enumerate(plan.batches, 1):
            yield TeamEvent(
                kind="batch_started", team_id=self.team_id, plan=plan, batch=batch,
                message=f"第 {batch_number} 轮 / 共 {len(plan.batches)} 轮",
            )
            async for event in self._run_batch(plan, batch, instruction=instruction):
                yield event
            waiting = [task for task in plan.tasks if task.status == "needs_input"]
            if waiting:
                return
            failed = [task for task in plan.tasks if task.status == "failed"]
            if failed:
                blocked = self._block_dependents(plan, {task.id for task in failed})
                for task in blocked:
                    yield TeamEvent(
                        kind="task_blocked",
                        team_id=self.team_id,
                        plan=plan,
                        task=task,
                        role=task.owner_role,
                        message=task.result,
                    )
                if len(failed) > self.settings.plan_max_failures:
                    message = (
                        f"失败任务数 {len(failed)} 超过上限 "
                        f"{self.settings.plan_max_failures}；阻塞任务数 {len(blocked)}"
                    )
                else:
                    message = (
                        f"Team 因 {len(failed)} 个任务失败而停止；"
                        f"阻塞任务数 {len(blocked)}"
                    )
                yield TeamEvent(
                    kind="team_failed", team_id=self.team_id, plan=plan,
                    message=message,
                )
                return

        done = sum(task.status == "done" for task in plan.tasks)
        if done != len(plan.tasks):
            yield TeamEvent(kind="team_failed", team_id=self.team_id, plan=plan, message=f"Team 未完成：{done}/{len(plan.tasks)} 个任务通过")
            return
        yield TeamEvent(kind="team_done", team_id=self.team_id, plan=plan, message=f"Team 完成：{done}/{len(plan.tasks)} 个任务通过")

    # ---- 断点续跑（V3）----

    async def resume(self, instruction: str = "") -> AsyncIterator[TeamEvent]:
        """Team 级断点续跑：从失败/阻塞/待办任务继续。needs_input 任务需用户先补充范围。"""
        plan = self._last_plan
        if plan is None:
            yield TeamEvent(kind="team_failed", team_id=self.team_id, message="没有可恢复的 Team 计划")
            return
        waiting = [task for task in plan.tasks if task.status == "needs_input"]
        if waiting:
            task = waiting[0]
            yield TeamEvent(
                kind="task_needs_input", team_id=self.team_id, plan=plan, task=task,
                role=task.owner_role, failure_category=task.pending_input_category,
                message="；".join(
                    filter(None, [task.pending_input_message, "任务等待输入，请先通过 /team resume 或自然语言补充范围后再继续"])
                ),
            )
            return
        done = sum(task.status == "done" for task in plan.tasks)
        if done == len(plan.tasks):
            yield TeamEvent(kind="team_failed", team_id=self.team_id, plan=plan, message="团队计划已全部完成，无需恢复")
            return
        # 重置 failed/blocked/pending/running 任务为 pending（done 保留 Artifact/result 供复用）
        for task in plan.tasks:
            if task.status in ("failed", "blocked", "pending", "running"):
                task.status = "pending"
                task.result = ""
                task.blocked_by = []
                task.failure_category = ""
        plan.batches = conflict_safe_batches(plan.tasks)
        skipped = [task.id for task in plan.tasks if task.status == "done"]
        yield TeamEvent(
            kind="team_resume_requested", team_id=self.team_id, plan=plan,
            message=(
                f"Team 恢复执行：跳过 {len(skipped)} 个已完成任务，"
                f"重跑 {sum(1 for b in plan.batches for _ in b)} 个任务"
                + (f"；补充指令：{instruction}" if instruction else "")
            ),
        )
        async for event in self._execute_plan_batches(plan, instruction=instruction):
            yield event

    async def _generate_plan(
        self, goal: str, feedback: str = "", previous: TeamPlan | None = None
    ) -> tuple[TeamPlan | None, list[str]]:
        context = ConversationContext(
            TEAM_PLANNER_PROMPT,
            self.settings,
            shared_provider=self.memory_manager.shared_sections if self.memory_manager else None,
        )
        parts = [f"任务：{goal}", f"请拆解为不超过 {self.settings.plan_max_subtasks} 个团队任务。"]
        if previous is not None:
            parts.append("上一版计划任务：\n" + "\n".join(f"- {task.id}: {task.title}" for task in previous.tasks))
        if feedback:
            parts.append(f"用户反馈（必须满足）：{feedback}")
        context.append(Message(role="user", content="\n\n".join(parts)))
        budget = await context.ensure_budget(self.llm)
        if not budget.proceed:
            raise LlmError(budget.message or "团队规划上下文超出模型窗口")
        messages = context.build_messages()
        warnings: list[str] = []
        for _ in range(1 + TEAM_PLAN_MAX_RETRIES):
            raw = await self._llm_text(messages)
            try:
                tasks, task_warnings = parse_team_tasks(
                    raw, self.settings.plan_max_subtasks, profiles=self.profiles
                )
            except PlanError as exc:
                messages.extend([
                    Message(role="assistant", content=raw),
                    Message(role="user", content=f"上面的 Team JSON 无效：{exc}。请修复并重新输出完整 JSON。"),
                ])
                continue
            warnings.extend(task_warnings)
            return TeamPlan(goal=goal, tasks=tasks, batches=conflict_safe_batches(tasks)), warnings
        return None, warnings

    async def _run_batch(self, plan: TeamPlan, batch: list[str], instruction: str = "") -> AsyncIterator[TeamEvent]:
        # 断点续跑时跳过已完成（done）任务，避免重复执行
        pending = [tid for tid in batch if plan.task_by_id(tid).status != "done"]
        if not pending:
            return
        queue: asyncio.Queue[TeamEvent | None] = asyncio.Queue()

        async def runner(task_id: str) -> None:
            try:
                await self._run_task(plan, task_id, queue, instruction=instruction)
            finally:
                queue.put_nowait(None)

        semaphore = asyncio.Semaphore(max(1, getattr(self.settings, "team_max_agents", 4)))

        async def limited_runner(task_id: str) -> None:
            async with semaphore:
                await runner(task_id)

        jobs = [asyncio.create_task(limited_runner(task_id)) for task_id in pending]
        completed = 0
        while completed < len(jobs):
            item = await queue.get()
            if item is None:
                completed += 1
            else:
                yield item
        await asyncio.gather(*jobs, return_exceptions=True)

    @staticmethod
    def _block_dependents(plan: TeamPlan, failed_ids: set[str]) -> list[TeamTask]:
        """Mark only downstream tasks as blocked; they were never executed."""
        blocked: list[TeamTask] = []
        changed = True
        while changed:
            changed = False
            for task in plan.tasks:
                if task.status != "pending" or not any(
                    dep in failed_ids or dep in {item.id for item in blocked}
                    for dep in task.deps
                ):
                    continue
                blockers = [
                    dep for dep in task.deps
                    if dep in failed_ids or any(item.id == dep for item in blocked)
                ]
                task.status = "blocked"
                task.blocked_by = blockers
                task.result = f"依赖任务未通过，未执行：{', '.join(blockers)}"
                task.failure_category = "dependency_blocked"
                blocked.append(task)
                changed = True
        return blocked

    def _pause_task_for_input(
        self,
        plan: TeamPlan,
        task: TeamTask,
        queue: asyncio.Queue[TeamEvent | None],
        *,
        category: str,
        message: str,
        review: ReviewResult | None = None,
        scope_claims: list[ResourceClaim] | None = None,
    ) -> None:
        """Pause safely instead of converting an unresolved decision to failure."""
        task.status = "needs_input"
        task.failure_category = category
        task.pending_input_category = category
        task.pending_input_message = message
        task.pending_repair_scope = list(scope_claims or [])
        task.pending_review = review
        task.result = message[:TEAM_RESULT_LIMIT]
        queue.put_nowait(TeamEvent(
            kind="repair_scope_required" if category.startswith("repair_scope") else "task_needs_input",
            team_id=self.team_id, plan=plan, task=task, role=task.owner_role,
            failure_category=category, scope_claims=list(task.pending_repair_scope),
            repair_attempts_started=task.repair_attempts_started,
            repair_attempts_blocked=task.repair_attempts_blocked,
            message=message,
            review=review,
        ))

    async def resume_task_with_repair_scope(
        self,
        task_id: str,
        claims: list[ResourceClaim],
    ) -> AsyncIterator[TeamEvent]:
        """Resume a paused task after revalidating an explicit write scope."""
        plan = self._last_plan
        if plan is None:
            yield TeamEvent(
                kind="team_failed", team_id=self.team_id,
                message="没有可恢复的 Team 计划",
            )
            return
        task = plan.task_by_id(task_id)
        if task is None or task.status != "needs_input" or task.pending_review is None:
            yield TeamEvent(
                kind="task_needs_input", team_id=self.team_id, plan=plan, task=task,
                failure_category="resume_invalid", message="任务不存在或当前不处于等待输入状态",
            )
            return

        queue: asyncio.Queue[TeamEvent | None] = asyncio.Queue()
        review = task.pending_review
        repair_claims = [
            ResourceClaim(claim.pattern, "write", claim.exclusive)
            for claim in claims
            if claim.access == "write" and claim.pattern.strip()
        ]
        repair = replace(
            task,
            id=f"{task.id}-repair-{task.repair_attempts_started + 1}",
            title=f"修复：{task.title}",
            description="\n".join(review.required_fixes or review.findings) or "根据审查结果修复任务",
            owner_role="repairer",
            allowed_tools=[],
            allowed_tools_declared=False,
            invalid_tools=[],
            tool_warnings=[],
            resource_scope_mode="targeted",
            resource_claims=repair_claims,
            resource_deny_patterns=list(task.resource_deny_patterns),
            deps=[],
            status="pending",
            result="",
            artifacts=[],
            failure_category="",
            blocked_by=[],
            recovery_attempts=0,
        )
        repair_profile = self.profiles.get("repairer") or self.profiles["coder"]
        policy_errors = validate_task_resource_policy(repair, repair_profile, self.project_root)
        if policy_errors:
            task.repair_attempts_blocked += 1
            self._pause_task_for_input(
                plan, task, queue,
                category="repair_scope_missing" if not repair_claims else "repair_scope_unsafe",
                message="；".join(policy_errors), review=review, scope_claims=repair_claims,
            )
            while not queue.empty():
                event = queue.get_nowait()
                if event is not None:
                    yield event
            return

        task.status = "running"
        task.pending_input_category = ""
        task.pending_input_message = ""
        task.pending_repair_scope = list(repair_claims)
        task.repair_attempts_started += 1
        task.attempts = task.repair_attempts_started
        queue.put_nowait(TeamEvent(
            kind="task_resume_requested", team_id=self.team_id, plan=plan, task=task,
            role="repairer", scope_claims=list(repair_claims),
            message="已确认修复范围，继续执行 Repairer",
        ))
        queue.put_nowait(TeamEvent(
            kind="repair_scope_validated", team_id=self.team_id, plan=plan, task=task,
            role="repairer", scope_claims=list(repair_claims),
            message="Repairer 写入范围校验通过",
        ))
        queue.put_nowait(TeamEvent(
            kind="repair_requested", team_id=self.team_id, plan=plan, task=repair,
            role="repairer", attempt=task.repair_attempts_started,
            message=repair.description,
        ))
        result, artifacts, agent_id, error, category = await self._execute_worker(
            plan, repair, queue, attempt=task.repair_attempts_started,
        )
        if error:
            task.status = "failed"
            task.failure_category = category or "execution_failed"
            task.result = error[:TEAM_RESULT_LIMIT]
            queue.put_nowait(TeamEvent(
                kind="agent_failed", team_id=self.team_id, plan=plan, task=repair,
                agent_id=agent_id, role="repairer", attempt=task.repair_attempts_started,
                failure_category=task.failure_category, message=error,
            ))
            queue.put_nowait(TeamEvent(
                kind="task_failed", team_id=self.team_id, plan=plan, task=task,
                agent_id=agent_id, role=task.owner_role,
                failure_category=task.failure_category, message=error,
            ))
        else:
            await self._publish_artifacts(
                plan, repair, artifacts, agent_id, queue,
                attempt=task.repair_attempts_started,
            )
            task.result = result[:TEAM_RESULT_LIMIT]
            try:
                next_review = await self._review(plan, task, artifacts, queue)
            except Exception as exc:
                next_review = ReviewResult(
                    task.id, "needs_input", [f"Reviewer 执行失败：{exc}"],
                    ["重新执行任务审查"], [], [], "review_execution_failed",
                )
            if next_review.verdict == "pass":
                task.status = "done"
                task.pending_review = None
                queue.put_nowait(TeamEvent(
                    kind="task_done", team_id=self.team_id, plan=plan, task=task,
                    message=f"修复后通过：{task.result}",
                ))
                if all(item.status == "done" for item in plan.tasks):
                    queue.put_nowait(TeamEvent(
                        kind="team_done", team_id=self.team_id, plan=plan,
                        message=f"Team 完成：{sum(item.status == 'done' for item in plan.tasks)}/{len(plan.tasks)} 个任务通过",
                    ))
            else:
                self._pause_task_for_input(
                    plan, task, queue,
                    category=next_review.category or "review_output_invalid",
                    message="；".join(next_review.findings or next_review.required_fixes) or "Reviewer 需要用户处理",
                    review=next_review,
                )

        while not queue.empty():
            event = queue.get_nowait()
            if event is not None:
                yield event

    async def _run_task(self, plan: TeamPlan, task_id: str, queue: asyncio.Queue[TeamEvent | None], instruction: str = "") -> None:
        task = plan.task_by_id(task_id)
        if task is None:
            return
        dependencies = [plan.task_by_id(dep) for dep in task.deps]
        if any(dep is None or dep.status != "done" for dep in dependencies):
            task.status = "blocked"
            task.blocked_by = [
                dep_id for dep_id, dep in zip(task.deps, dependencies)
                if dep is None or dep.status != "done"
            ]
            task.result = f"依赖任务未通过，未执行：{', '.join(task.blocked_by)}"
            task.failure_category = "dependency_blocked"
            queue.put_nowait(TeamEvent(
                kind="task_blocked", team_id=self.team_id, plan=plan,
                task=task, role=task.owner_role, message=task.result,
            ))
            return

        task.status = "running"
        self._audit("team_task_started", team_id=self.team_id, task_id=task.id, role=task.owner_role)
        queue.put_nowait(TeamEvent(kind="task_started", team_id=self.team_id, plan=plan, task=task, role=task.owner_role))
        profile = self.profiles.get(task.owner_role) or self.profiles["coder"]
        result, artifacts, agent_id, error, failure_category = await self._execute_worker(
            plan, task, queue, attempt=1, instruction=instruction
        )
        first_agent_id = agent_id
        if error:
            queue.put_nowait(TeamEvent(
                kind="agent_failed", team_id=self.team_id, plan=plan, task=task,
                agent_id=agent_id, role=task.owner_role, attempt=1,
                failure_category=failure_category, message=error,
            ))
        max_recoveries = max(0, getattr(self.settings, "team_max_recoveries", TEAM_MAX_RECOVERIES))
        if error and task.recovery_attempts < max_recoveries and self._should_recover(task, profile, failure_category):
            recovery_steps = self._recovery_steps()
            task.recovery_attempts += 1
            preserved_ids = [artifact.id for artifact in artifacts]
            await self._publish_artifacts(plan, task, artifacts, agent_id, queue, attempt=1)
            queue.put_nowait(TeamEvent(
                kind="task_retry_started", team_id=self.team_id, plan=plan, task=task,
                role=task.owner_role, attempt=task.recovery_attempts + 1,
                failure_category=failure_category, retryable=True,
                previous_steps=self._effective_steps(profile), retry_steps=recovery_steps,
                preserved_artifacts=preserved_ids,
                message="只读任务达到步数上限，使用已有证据进行一次受控恢复",
            ))
            recovery_summary = self._recovery_summary(task, artifacts)
            result, recovery_artifacts, recovery_agent_id, recovery_error, recovery_category = await self._execute_worker(
                plan, task, queue, attempt=task.recovery_attempts + 1,
                steps_override=recovery_steps, recovery_summary=recovery_summary,
            )
            artifacts.extend(recovery_artifacts)
            agent_id = recovery_agent_id
            if recovery_error:
                error = f"{error}；恢复执行仍失败：{recovery_error}"
                failure_category = recovery_category or failure_category
            else:
                error = ""
                failure_category = ""
        if error:
            await self._publish_artifacts(plan, task, artifacts, agent_id, queue, attempt=task.recovery_attempts + 1)
            task.status = "failed"
            task.result = error[:TEAM_RESULT_LIMIT]
            task.failure_category = failure_category or "execution_failed"
            self._audit("team_task_failed", team_id=self.team_id, task_id=task.id, role=task.owner_role, error=task.result)
            if agent_id != first_agent_id:
                queue.put_nowait(TeamEvent(
                    kind="agent_failed", team_id=self.team_id, plan=plan, task=task,
                    agent_id=agent_id, role=task.owner_role,
                    attempt=task.recovery_attempts + 1,
                    failure_category=task.failure_category, message=task.result,
                ))
            queue.put_nowait(TeamEvent(
                kind="task_failed", team_id=self.team_id, plan=plan, task=task,
                agent_id=agent_id, role=task.owner_role, failure_category=task.failure_category,
                attempt=task.recovery_attempts + 1, retryable=False, message=task.result,
            ))
            return
        task.result = result[:TEAM_RESULT_LIMIT]
        await self._publish_artifacts(plan, task, artifacts, agent_id, queue, attempt=task.recovery_attempts + 1)
        try:
            review = await self._review(plan, task, artifacts, queue)
        except Exception as exc:
            review = ReviewResult(
                task.id, "needs_input", [f"Reviewer 执行失败：{exc}"],
                ["重新执行任务审查"], [], [], "review_execution_failed",
            )
        if review.verdict == "pass":
            task.status = "done"
            self._audit("team_task_done", team_id=self.team_id, task_id=task.id, role=task.owner_role, result=task.result)
            queue.put_nowait(TeamEvent(kind="task_done", team_id=self.team_id, plan=plan, task=task, message=task.result))
            return
        if review.verdict == "needs_input":
            self._pause_task_for_input(
                plan, task, queue,
                category=review.category or "review_output_invalid",
                message="；".join(review.findings or review.required_fixes) or "Reviewer 需要用户处理",
                review=review,
            )
            return

        max_repairs = max(0, getattr(self.settings, "team_max_repairs", TEAM_MAX_RETRIES))
        for attempt in range(1, max_repairs + 1):
            repair_claims, scope_warnings = build_repair_scope(task, review)
            if not repair_claims:
                self._pause_task_for_input(
                    plan, task, queue,
                    category="repair_scope_missing",
                    message="Repairer 尚未启动：没有明确的 write claim，请确认允许修改的文件范围",
                    review=review,
                )
                return
            repair_description = "\n".join(review.required_fixes or review.findings) or "根据审查结果修复任务"
            if scope_warnings:
                repair_description += "\n\n修复范围提示：" + "；".join(scope_warnings)
            repair = replace(
                task,
                id=f"{task.id}-repair-{task.repair_attempts_started + 1}",
                title=f"修复：{task.title}",
                description=repair_description,
                owner_role="repairer",
                # A role change must not inherit the original role's tool or
                # discovery policy. Empty means use the repairer profile tools.
                allowed_tools=[],
                allowed_tools_declared=False,
                invalid_tools=[],
                tool_warnings=[],
                resource_scope_mode="targeted",
                resource_claims=[
                    ResourceClaim(claim.pattern, "write", claim.exclusive)
                    for claim in repair_claims
                ],
                resource_deny_patterns=list(task.resource_deny_patterns),
                deps=[],
                status="pending",
                result="",
                artifacts=[],
                failure_category="",
                blocked_by=[],
                recovery_attempts=0,
            )
            repair_profile = self.profiles.get("repairer") or self.profiles["coder"]
            policy_errors = validate_task_resource_policy(repair, repair_profile, self.project_root)
            if policy_errors:
                task.repair_attempts_blocked += 1
                category = (
                    "repair_scope_missing"
                    if any(error.startswith("repair_scope_missing") for error in policy_errors)
                    else "repair_scope_unsafe"
                )
                self._pause_task_for_input(
                    plan, task, queue,
                    category=category,
                    message="；".join(policy_errors),
                    review=review,
                    scope_claims=repair_claims,
                )
                return
            task.attempts = task.repair_attempts_started + 1
            task.repair_attempts_started += 1
            queue.put_nowait(TeamEvent(kind="repair_requested", team_id=self.team_id, plan=plan, task=repair, role="repairer", message=repair.description))
            repair_result, repair_artifacts, repair_agent_id, repair_error, repair_category = await self._execute_worker(
                plan, repair, queue, attempt=task.repair_attempts_started
            )
            if repair_error:
                queue.put_nowait(TeamEvent(
                    kind="agent_failed", team_id=self.team_id, plan=plan, task=repair,
                    agent_id=repair_agent_id, role="repairer", attempt=task.repair_attempts_started,
                    failure_category=repair_category or "execution_failed", message=repair_error,
                ))
                review = ReviewResult(task.id, "fail", [repair_error], [repair_error], [], [], repair_category or "execution_failed")
            else:
                await self._publish_artifacts(plan, repair, repair_artifacts, repair_agent_id, queue, attempt=task.repair_attempts_started)
                task.result = repair_result[:TEAM_RESULT_LIMIT]
                try:
                    review = await self._review(plan, task, repair_artifacts, queue)
                except Exception as exc:
                    review = ReviewResult(task.id, "needs_input", [f"Reviewer 执行失败：{exc}"], ["重新执行任务审查"], [], [], "review_execution_failed")
            if review.verdict == "pass":
                task.status = "done"
                queue.put_nowait(TeamEvent(kind="task_done", team_id=self.team_id, plan=plan, task=task, message=f"修复后通过：{task.result}"))
                return
            if review.verdict == "needs_input":
                self._pause_task_for_input(
                    plan, task, queue,
                    category=review.category or "review_output_invalid",
                    message="；".join(review.findings or review.required_fixes) or "Reviewer 需要用户处理",
                    review=review,
                )
                return
        task.status = "failed"
        task.failure_category = "review_failed"
        task.result = "; ".join(review.findings or review.required_fixes)[:TEAM_RESULT_LIMIT] or "审查未通过且修复次数已用尽"
        self._audit("team_task_failed", team_id=self.team_id, task_id=task.id, role=task.owner_role, error=task.result)
        queue.put_nowait(TeamEvent(kind="task_failed", team_id=self.team_id, plan=plan, task=task, review=review, message=task.result))

    def _effective_steps(self, profile: AgentProfile) -> int:
        configured = getattr(self.settings, f"team_{profile.name}_steps", None)
        candidate = configured or profile.max_steps or self.settings.plan_subtask_steps
        maximum = max(1, getattr(self.settings, "team_max_steps", 40))
        return max(1, min(int(candidate), maximum))

    def _recovery_steps(self) -> int:
        maximum = max(1, getattr(self.settings, "team_max_steps", 40))
        configured = max(1, getattr(self.settings, "team_recovery_steps", TEAM_RECOVERY_STEPS))
        return min(configured, maximum)

    @staticmethod
    def _should_recover(task: TeamTask, profile: AgentProfile, failure_category: str) -> bool:
        if failure_category != "step_limit" or profile.name not in READ_DISCOVERY_ROLES:
            return False
        if profile.can_write or "write_file" in profile.allowed_tools or "execute_command" in profile.allowed_tools:
            return False
        if any(claim.access == "write" for claim in task.resource_claims):
            return False
        if task.resource_scope_mode not in {"targeted", "read_discovery"}:
            return False
        if task.allowed_tools and any(tool not in READ_DISCOVERY_TOOLS for tool in task.allowed_tools):
            return False
        return True

    @staticmethod
    def _recovery_summary(task: TeamTask, artifacts: list[Artifact]) -> str:
        evidence = "\n".join(
            f"- {artifact.id}: {artifact.uri} — {artifact.summary[:600]}"
            for artifact in artifacts
        ) or "- 暂无可复用 Artifact"
        criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria) or "- 未提供验收标准"
        return (
            "上一次只读执行已达到步数上限。请复用以下已有证据，只补齐未完成的验收项，"
            "不要重复扫描已经确认的内容，也不要修改项目文件。\n"
            f"任务验收标准：\n{criteria}\n已有证据：\n{evidence}"
        )

    async def _publish_artifacts(
        self,
        plan: TeamPlan,
        task: TeamTask,
        artifacts: list[Artifact],
        agent_id: str,
        queue: asyncio.Queue[TeamEvent | None],
        *,
        attempt: int,
    ) -> None:
        existing = set(task.artifacts)
        for artifact in artifacts:
            if artifact.id in existing:
                continue
            artifact.attempt = attempt
            await self.artifacts.publish(artifact)
            task.artifacts.append(artifact.id)
            existing.add(artifact.id)
            queue.put_nowait(TeamEvent(
                kind="artifact_produced", team_id=self.team_id, plan=plan, task=task,
                agent_id=agent_id, role=task.owner_role, artifact=artifact, attempt=attempt,
            ))

    async def _execute_worker(
        self,
        plan: TeamPlan,
        task: TeamTask,
        queue: asyncio.Queue[TeamEvent | None],
        *,
        attempt: int = 1,
        steps_override: int | None = None,
        recovery_summary: str = "",
        instruction: str = "",
    ) -> tuple[str, list[Artifact], str, str, str]:
        profile = self.profiles.get(task.owner_role) or self.profiles["coder"]
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        policy_errors = validate_task_resource_policy(task, profile, self.project_root)
        if policy_errors:
            category = (
                "repair_scope_missing"
                if any(error.startswith("repair_scope_missing") for error in policy_errors)
                else "resource_policy_invalid"
            )
            return "", [], agent_id, "；".join(policy_errors), category
        effective_steps = steps_override or self._effective_steps(profile)
        queue.put_nowait(TeamEvent(
            kind="agent_started", team_id=self.team_id, plan=plan, task=task,
            agent_id=agent_id, role=profile.name, attempt=attempt,
            effective_steps=effective_steps,
            message=f"预算 {effective_steps} 步" if attempt == 1 else f"恢复预算 {effective_steps} 步",
        ))
        sub_settings = replace(
            self.settings,
            tool_steps=effective_steps,
        )
        scoped_tools = ScopedToolRegistry(self.tools, task, self.project_root, profile)
        prompt = self._worker_system_prompt(plan, task, profile)
        if self.agent_factory is not None:
            agent = self.agent_factory.create(profile, task)
        else:
            agent = ReActAgent(
                llm=self.llm,
                tools=scoped_tools,  # type: ignore[arg-type]
                settings=sub_settings,
                system_prompt=prompt,
                approval_policy=self.approval_policy,
                audit=self.audit,
                memory_manager=self.memory_manager,
                mcp_manager=self.mcp_manager,
                ask_requester=self.ask_requester,
            )
        artifacts: list[Artifact] = []
        try:
            async for event in agent.run(self._worker_user_prompt(task, recovery_summary=recovery_summary, instruction=instruction)):
                if event.kind in {
                    "thinking", "content", "tool_call", "ask_user", "approval", "tool_result", "retrying",
                    "context_compacted", "context_warning", "context_usage", "usage",
                }:
                    queue.put_nowait(TeamEvent(kind="subtask_event", team_id=self.team_id, plan=plan, task=task, agent_id=agent_id, role=profile.name, agent_event=event))
                    if event.kind == "tool_result" and event.tool_result:
                        result = event.tool_result
                        summary = (result.output or result.error)[:TEAM_ARTIFACT_LIMIT]
                        kind = "test" if "test" in result.name.lower() or "pytest" in summary.lower() else "tool"
                        artifacts.append(Artifact(
                            id=f"artifact-{uuid.uuid4().hex[:10]}", task_id=task.id, kind=kind,
                            uri=str(event.tool_result.name), summary=summary,
                            producer_agent_id=agent_id,
                            verification_records=["tool_result:ok" if result.ok else "tool_result:failed"],
                        ))
                elif event.kind == "error":
                    category = event.error_category or "execution_failed"
                    if event.retry_attempts:
                        category = "transient_api_error_exhausted"
                    message = event.text or "Worker 执行失败"
                    if event.retry_attempts:
                        message = f"{message}（已重试 {event.retry_attempts} 次）"
                    return "", artifacts, agent_id, message, category
                    return "", artifacts, agent_id, event.text or "Worker 执行失败"
                elif event.kind == "step_limit":
                    return "", artifacts, agent_id, f"达到 Worker 步数上限（{sub_settings.tool_steps}）", "step_limit"
                    return "", artifacts, agent_id, f"达到 Worker 步数上限（{sub_settings.tool_steps}）"
                elif event.kind in {"budget_exceeded", "context_overflow"}:
                    category = "context_overflow" if event.kind == "context_overflow" else "budget_exceeded"
                    return "", artifacts, agent_id, event.text or "Worker 上下文或预算超限", category
                    return "", artifacts, agent_id, event.text or "Worker 上下文超限"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return "", artifacts, agent_id, f"{type(exc).__name__}: {exc}", "worker_exception"
            return "", artifacts, agent_id, f"{type(exc).__name__}: {exc}"
        final = next((message.content for message in reversed(agent.messages) if message.role == "assistant" and message.content.strip()), "")
        artifacts.append(Artifact(
            id=f"artifact-{uuid.uuid4().hex[:10]}", task_id=task.id, kind="report",
            summary=final[:TEAM_ARTIFACT_LIMIT], producer_agent_id=agent_id,
        ))
        queue.put_nowait(TeamEvent(kind="agent_done", team_id=self.team_id, plan=plan, task=task, agent_id=agent_id, role=profile.name, message=final[:TEAM_RESULT_LIMIT]))
        return final, artifacts, agent_id, "", ""

    async def _review(
        self, plan: TeamPlan, task: TeamTask, artifacts: list[Artifact], queue: asyncio.Queue[TeamEvent | None]
    ) -> ReviewResult:
        queue.put_nowait(TeamEvent(kind="task_review_started", team_id=self.team_id, plan=plan, task=task, role="reviewer"))
        if not getattr(self.settings, "team_review", True):
            result = ReviewResult(task.id, "pass", [], [], ["Team 任务级审查已关闭"])
        elif any(record == "tool_result:failed" for artifact in artifacts for record in artifact.verification_records):
            result = ReviewResult(task.id, "fail", ["存在工具执行失败证据"], ["修复工具失败并重新验证"], [])
        elif self.task_reviewer is not None:
            result = await self.task_reviewer(task, artifacts)
        else:
            result = await self._llm_review(task, artifacts, queue=queue)
        queue.put_nowait(TeamEvent(
            kind="task_review_done", team_id=self.team_id, plan=plan, task=task,
            role="reviewer", review=result, failure_category=result.category,
            message="；".join(result.findings or result.required_fixes),
        ))
        self._audit("team_task_review", team_id=self.team_id, task_id=task.id, verdict=result.verdict, findings=result.findings)
        return result

    async def _llm_review(
        self,
        task: TeamTask,
        artifacts: list[Artifact],
        *,
        queue: asyncio.Queue[TeamEvent | None] | None = None,
    ) -> ReviewResult:
        evidence = "\n".join(
            f"- [{artifact.kind}] {artifact.uri}: {artifact.summary[:TEAM_ARTIFACT_LIMIT]}"
            for artifact in artifacts
        ) or "（没有产物）"
        prompt = (
            f"任务：{task.title}\n说明：{task.description}\n"
            f"验收标准：\n- " + "\n- ".join(task.acceptance_criteria) +
            f"\n执行产物和证据：\n{evidence}\n"
            "请严格输出 JSON；verdict 为 fail 时尽量提供最小 repair_scope，"
            "每项格式为 {\"pattern\": \"项目内路径\", \"access\": \"write\"}。"
        )
        context = [Message(role="system", content=TEAM_REVIEWER_PROMPT), Message(role="user", content=prompt)]
        retry_limit = max(
            0,
            getattr(self.settings, "team_review_output_retries", TEAM_REVIEW_OUTPUT_RETRIES),
        )
        last_error = ReviewOutputError("review_output_invalid", "Reviewer 输出无效")
        for attempt in range(retry_limit + 1):
            raw = await self._llm_text(context)
            parsed = parse_review_output(task.id, raw)
            if isinstance(parsed, ReviewResult):
                return parsed
            last_error = parsed
            if queue is not None:
                queue.put_nowait(TeamEvent(
                    kind="review_output_invalid", team_id=self.team_id, task=task,
                    role="reviewer", attempt=attempt + 1,
                    failure_category=parsed.category, retryable=attempt < retry_limit,
                    message=parsed.message,
                ))
            if attempt >= retry_limit:
                break
            if queue is not None:
                queue.put_nowait(TeamEvent(
                    kind="review_output_retry", team_id=self.team_id, task=task,
                    role="reviewer", attempt=attempt + 1, retryable=True,
                    message=f"Reviewer 输出无法解析，正在重新请求结构化结果（{attempt + 1}/{retry_limit}）",
                ))
            context.extend([
                Message(role="assistant", content=raw),
                Message(
                    role="user",
                    content=(
                        f"上一次 Reviewer 输出无效（{parsed.category}）。"
                        "请不要重新执行任务或调用工具，只输出一个合法 JSON 对象。"
                        "如果无法安全确定修改文件范围，请使用 verdict=needs_input，不要猜测路径。"
                    ),
                ),
            ])
        return ReviewResult(
            task.id,
            "needs_input",
            ["Reviewer 未返回可解析的结构化结果"],
            ["请选择重新审查，或补充允许 Repairer 修改的文件范围"],
            [last_error.category],
            [],
            last_error.category,
        )

    def _worker_system_prompt(self, plan: TeamPlan, task: TeamTask, profile: AgentProfile) -> str:
        parts = [DEFAULT_SYSTEM_PROMPT, "", f"# 你的角色：{profile.name}", profile.system_prompt, "", f"# Team 总目标\n{plan.goal}", f"# 当前任务\n{task.title}\n{task.description}", "# 验收标准\n- " + "\n- ".join(task.acceptance_criteria)]
        parts.append(f"# 资源策略\n模式：{task.resource_scope_mode}")
        if task.resource_claims:
            parts.append("# 资源范围\n" + "\n".join(f"- {claim.access}: {claim.pattern}" for claim in task.resource_claims))
        if task.resource_scope_mode == "read_discovery":
            parts.append("# 探索规则\n允许在项目根目录内使用只读发现工具；不要写文件、执行命令或读取敏感文件。")
        dependencies: list[str] = []
        for dep_id in task.deps:
            dep = plan.task_by_id(dep_id)
            if dep is not None:
                dependencies.append(f"[{dep.id}] {dep.result[:TEAM_RESULT_LIMIT]}")
        if dependencies:
            parts.append("# 已通过审查的依赖结果\n" + "\n".join(dependencies))
        return "\n".join(parts)

    @staticmethod
    def _worker_user_prompt(task: TeamTask, *, recovery_summary: str = "", instruction: str = "") -> str:
        prompt = f"请执行任务 {task.id}（{task.title}）：\n{task.description}\n完成后简要汇报结果和验证证据，只处理这个任务。"
        extra = [part for part in (recovery_summary, instruction) if part]
        return f"{prompt}\n\n「{ '；'.join(extra) }」" if extra else prompt

    async def _llm_text(self, messages: list[Message]) -> str:
        parts: list[str] = []
        async for event in self.llm.stream_chat(messages, tools=None):
            if event.kind == "content" and event.text:
                parts.append(event.text)
        return "".join(parts)

    def _audit(self, action: str, **fields) -> None:
        if self.audit is not None:
            self.audit.record(action, **fields)
