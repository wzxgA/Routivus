"""Pure command-completion parsing and token replacement for Routivus.

This is the P0 slice of ``enhancement-v2/07``: it moves the parsing and
text-replacement logic out of the Textual ``Composer`` widget so it can be
unit-tested without launching a UI.  User-visible behaviour is intentionally
unchanged in this phase.

Later phases (P1-P3) layer subcommand, option and dynamic-value completion on
top of the data model introduced here.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from routivus.cli.commands import (
    SLASH_COMMANDS,
    SlashCommandSpec,
    SlashSubcommandSpec,
    filter_slash_commands,
)

CompletionKind = Literal["command", "subcommand", "argument", "option", "value"]

CompletionValueKind = Literal["text", "enum", "provider", "model"]


@dataclass(frozen=True)
class Token:
    """One whitespace-separated token with absolute offsets in the input."""

    text: str
    start: int
    end: int


@dataclass(frozen=True)
class CompletionCandidate:
    """A candidate: what text to insert, where, and how to present it.

    A candidate only describes text insertion.  It never carries tool calls,
    file writes, command execution or any other side effect.
    """

    label: str
    insert_text: str
    detail: str = ""
    kind: CompletionKind = "value"
    sort_key: tuple[int, str] = (0, "")


@dataclass(frozen=True)
class CompletionContext:
    """Parsed view of a composer line, independent of any UI widget."""

    raw: str
    cursor_position: int
    is_command: bool = False
    command: str = ""
    command_token_start: int = 0
    command_token_end: int = 0
    tokens: tuple[Token, ...] = ()
    current_token: str = ""
    current_token_start: int = 0
    current_token_end: int = 0


def tokenize(raw: str) -> list[Token]:
    """Split on whitespace, keeping quoted segments together.

    Defensive by design: an unterminated quote never raises; the remaining
    text is returned as one trailing token.
    """

    tokens: list[Token] = []
    index = 0
    length = len(raw)
    while index < length:
        while index < length and raw[index].isspace():
            index += 1
        if index >= length:
            break
        start = index
        quote: str | None = None
        while index < length:
            char = raw[index]
            if quote is not None:
                index += 1
                if char == quote:
                    quote = None
                continue
            if char in ("'", '"'):
                quote = char
                index += 1
                continue
            if char.isspace():
                break
            index += 1
        tokens.append(Token(raw[start:index], start, index))
    return tokens


def _clamp(value: int, length: int) -> int:
    return max(0, min(length, value))


def _current_token(tokens: tuple[Token, ...], cursor: int) -> Token | None:
    """Return the token under the cursor.

    The cursor sitting exactly on a token's end boundary belongs to that
    token so that completing right after a word still targets it.
    """

    chosen: Token | None = None
    for token in tokens:
        if token.start <= cursor <= token.end:
            chosen = token
    return chosen


def parse_completion_line(raw: str, cursor_position: int | None = None) -> CompletionContext:
    """Parse a composer line into a non-throwing CompletionContext.

    A line is a command context when its first token starts with ``/``.
    Partial or malformed input never raises; it simply yields an incomplete
    context that callers can ignore.
    """

    cursor = _clamp(len(raw) if cursor_position is None else cursor_position, len(raw))
    tokens = tuple(tokenize(raw))
    is_command = False
    command = ""
    command_start = 0
    command_end = 0
    if tokens and tokens[0].text.startswith("/"):
        is_command = True
        command = tokens[0].text.lower()
        command_start = tokens[0].start
        command_end = tokens[0].end

    active = _current_token(tokens, cursor)
    return CompletionContext(
        raw=raw,
        cursor_position=cursor,
        is_command=is_command,
        command=command,
        command_token_start=command_start,
        command_token_end=command_end,
        tokens=tokens,
        current_token=active.text if active else "",
        current_token_start=active.start if active else cursor,
        current_token_end=active.end if active else cursor,
    )


def replace_span(raw: str, start: int, end: int, replacement: str) -> tuple[str, int]:
    """Return ``(new_text, new_cursor)`` with ``raw[start:end]`` replaced.

    The returned cursor points to the end of the inserted text.
    """

    new = raw[:start] + replacement + raw[end:]
    return new, start + len(replacement)


def complete_command_token(raw: str, command: str) -> tuple[str, int]:
    """Replace the leading slash command token with ``command``.

    Reproduces the exact behaviour the App/Composer used to implement
    inline: leading whitespace and any trailing text after the command are
    preserved, and the final cursor moves to the end of the resulting line.
    Non-command input is returned unchanged.
    """

    ctx = parse_completion_line(raw)
    if not ctx.is_command:
        return raw, len(raw)
    new_text, _ = replace_span(
        raw, ctx.command_token_start, ctx.command_token_end, command
    )
    return new_text, len(new_text)


def sort_candidates(
    candidates: list[CompletionCandidate], *, exact_match: str | None = None
) -> list[CompletionCandidate]:
    """Stable sort by sort_key then label; exact matches float to the front.

    ``exact_match`` is the lowercase text that, when it equals a candidate's
    insert label, ranks that candidate first (mirrors the exact-alias-first
    ordering of top-level command filtering).
    """

    if exact_match is not None:
        exact_match = exact_match.lower()
    return sorted(
        candidates,
        key=lambda cand: (
            0
            if exact_match is not None and cand.insert_text.lower() == exact_match
            else 1,
            cand.sort_key[0],
            cand.sort_key[1] or cand.label,
            cand.label,
        ),
    )


# ---------------------------------------------------------------------------
# P1: layered command / subcommand / static-option completion
# ---------------------------------------------------------------------------


def _command_spec_or_none(command: str) -> SlashCommandSpec | None:
    """Return the resolved command spec (name or alias) or None."""
    normalized = command.lower()
    for spec in SLASH_COMMANDS:
        if spec.name.lower() == normalized or any(
            alias.lower() == normalized for alias in spec.aliases
        ):
            return spec
    return None


def _subcommand_or_none(spec: SlashCommandSpec, token: str) -> SlashSubcommandSpec | None:
    normalized = token.lower()
    for sub in spec.subcommands:
        if sub.name.lower() == normalized:
            return sub
    return None


def completion_candidates(raw: str, cursor_position: int | None = None) -> list[CompletionCandidate]:
    """Return layered completion candidates for a command line.

    Top-level commands, subcommands and statically-declared options are
    offered at the correct token, keeping existing top-level behaviour
    compatible. Dynamic values (providers/models/MCP/skills/...) are a later
    phase and intentionally not resolved here.
    """

    if not isinstance(raw, str) or not raw.lstrip().startswith("/"):
        return []

    ctx = parse_completion_line(raw, cursor_position)
    if not ctx.is_command or not ctx.tokens:
        return []

    tokens = ctx.tokens
    command_token = ctx.command
    index_of_current = None
    for index, token in enumerate(tokens):
        if token.start <= ctx.cursor_position <= token.end:
            index_of_current = index
            break
    # Cursor sitting on the boundary after the command belongs to the next token.
    if index_of_current is None and ctx.cursor_position >= tokens[0].end:
        index_of_current = len(tokens)  # treated as a fresh argument slot

    spec = _command_spec_or_none(command_token)
    is_command_exact = spec is not None

    # --- Layer 1: still typing the command token (or nothing after it) ---
    if not is_command_exact or index_of_current in (0, None):
        prefix = tokens[0].text if tokens else raw.strip()
        if is_command_exact:
            # Typing an exact command as its own full token; show subcommands.
            pass
        else:
            specs = filter_slash_commands(prefix)
            return [
                CompletionCandidate(
                    spec.name,
                    spec.name,
                    detail=spec.description,
                    kind="command",
                )
                for spec in specs
            ]

    # --- Layer 2: resolve the current token against subcommands / options ---
    if index_of_current is None:
        return []

    if index_of_current == 0:
        # Exact command typed with no arguments yet -> offer subcommands/options.
        if spec is None:
            return []
        if spec.subcommands:
            return [
                CompletionCandidate(sub.name, sub.name, detail=sub.description, kind="subcommand")
                for sub in spec.subcommands
            ]
        return [
            CompletionCandidate(option, option, detail="选项参数", kind="option")
            for option in spec.options
        ]

    if index_of_current == 1:
        # Second token: a subcommand is expected ('' when the slot is blank).
        token = tokens[1].text if len(tokens) > 1 else ""
        if spec is not None and spec.subcommands:
            subs = [sub for sub in spec.subcommands if sub.name.lower().startswith(token.lower())]
            if not subs:
                subs = list(spec.subcommands)
            return [
                CompletionCandidate(sub.name, sub.name, detail=sub.description, kind="subcommand")
                for sub in subs
            ]
        if spec is not None and spec.options:
            options = [o for o in spec.options if o.lower().startswith(token.lower())] or list(spec.options)
            return [
                CompletionCandidate(o, o + " ", detail="选项参数", kind="option") for o in options
            ]
        return []

    # --- index_of_current >= 2: arguments / option values ---
    # Detect whether we are typing the *value* of a preceding option (the
    # previous token itself is a declared option name). In that case the
    # static layer must not repeat the option list — dynamic rules own value
    # completion. Options already typed are also excluded so you never see the
    # same option twice.
    prev_declares_value = False
    if index_of_current >= 1 and index_of_current - 1 < len(tokens):
        prev_text = tokens[index_of_current - 1].text
        declared_options: set[str] = set()
        if spec is not None:
            declared_options.update(spec.options or ())
            if spec.subcommands and len(tokens) >= 2:
                sub_spec = _subcommand_or_none(spec, tokens[1].text)
                if sub_spec is not None:
                    declared_options.update(sub_spec.options or ())
        prev_declares_value = prev_text in declared_options

    # Collect already-written option names so we don't offer them twice. They
    # are the non-current tokens whose text is in declared_options.
    consumed_options: set[str] = set()
    if spec is not None:
        for idx, token in enumerate(tokens):
            if idx == index_of_current:
                continue
            if token.text in declared_options:
                consumed_options.add(token.text)

    if spec is not None and spec.subcommands and len(tokens) >= 2:
        sub = _subcommand_or_none(spec, tokens[1].text)
        if sub is not None and sub.options and not prev_declares_value:
            current = ctx.current_token if index_of_current < len(tokens) else ""
            options = [o for o in sub.options if o not in consumed_options]
            if current and current.startswith("-"):
                matches = [o for o in options if o.lower().startswith(current.lower())]
                options = matches or options
            return [
                CompletionCandidate(o, o + " ", detail="选项参数", kind="option") for o in options
            ]
        if sub is None and tokens[1].text and spec.options and not prev_declares_value:
            options = [o for o in spec.options if o not in consumed_options]
            return [
                CompletionCandidate(o, o + " ", detail="选项参数", kind="option")
                for o in options
            ]
    if spec is not None and spec.options and not prev_declares_value:
        options = [o for o in spec.options if o not in consumed_options]
        return [
            CompletionCandidate(o, o + " ", detail="选项参数", kind="option")
            for o in options
        ]
    return []


def apply_completion(
    raw: str, cursor_position: int | None, candidate: CompletionCandidate
) -> tuple[str, int]:
    """Apply a candidate to replace the current token; return (value, cursor).

    Trailing text after the current token is preserved. The cursor is placed
    at the end of the inserted text (mirrors complete_command_token).
    """

    ctx = parse_completion_line(raw, cursor_position)
    if not ctx.is_command:
        return raw, len(raw)
    start = ctx.current_token_start
    end = ctx.current_token_end
    new_text, _ = replace_span(raw, start, end, candidate.insert_text)
    # Keep the cursor on the inserted token rather than forcing it to the end
    # of the whole line, so any later tokens are preserved (P3 §4.4).
    return new_text, start + len(candidate.insert_text)


# ---------------------------------------------------------------------------
# P2: dynamic local value completion
#
# Layered static candidates (commands/subcommands/options) are independent of
# runtime data.  P2 adds local, side-effect-free dynamic values (providers,
# models, MCP servers, skills, memory ids, team tasks).  The engine only knows
# about a *kind* plus a provider name; the registry resolves names to data,
# so the engine stays U.I.- and agent-independent and never triggers network
# I/O or tool execution.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletionArgumentSpec:
    """Describes one positional argument slot that takes a dynamic value.

    ``kind`` drives presentation; ``value_provider`` names a registered,
    user-agnostic provider (never a user-constructed callable).
    """

    name: str
    kind: CompletionValueKind = "text"
    value_provider: str = ""


@dataclass(frozen=True)
class DynamicArgumentRules:
    """Declare which dynamic value kinds each subcommand/option consumes.

    ``positionals`` maps a likely subcommand token to the value spec for its
    first argument; ``option_values`` maps ``--option`` to a value spec.
    """

    positionals: dict[str, CompletionArgumentSpec] = field(default_factory=dict)
    option_values: dict[str, CompletionArgumentSpec] = field(default_factory=dict)


__DYNAMIC_RULES: dict[str, DynamicArgumentRules] = {
    "/model": DynamicArgumentRules(
        positionals={
            "list": CompletionArgumentSpec("provider", "provider", "provider"),
            "provider": CompletionArgumentSpec("provider", "provider", "provider"),
            "model": CompletionArgumentSpec("model", "model", "model"),
            "": CompletionArgumentSpec("provider", "provider", "provider"),
        }
    ),
    "/mcp": DynamicArgumentRules(
        positionals={
            "status": CompletionArgumentSpec("server", "enum"),
            "resources": CompletionArgumentSpec("server", "enum"),
            "restart": CompletionArgumentSpec("server", "enum", "mcp"),
            "logs": CompletionArgumentSpec("server", "enum", "mcp"),
            "enable": CompletionArgumentSpec("server", "enum", "mcp"),
            "disable": CompletionArgumentSpec("server", "enum", "mcp"),
        }
    ),
    "/skill": DynamicArgumentRules(
        positionals={
            "load": CompletionArgumentSpec("skill", "enum", "skill"),
            "enable": CompletionArgumentSpec("skill", "enum", "skill"),
            "disable": CompletionArgumentSpec("skill", "enum", "skill"),
        }
    ),
    "/memory": DynamicArgumentRules(
        positionals={"delete": CompletionArgumentSpec("id", "text", "memory_id")}
    ),
    "/web": DynamicArgumentRules(
        positionals={"search": CompletionArgumentSpec("query", "text")}
    ),
}


def dynamic_argument_rules(command: str) -> DynamicArgumentRules:
    return __DYNAMIC_RULES.get(command.lower(), DynamicArgumentRules())


ProviderGetCandidates = Callable[[CompletionContext], Sequence[CompletionCandidate]]


class CompletionProviderRegistry:
    """Resolve provider names to candidate generators.

    Providers are registered by plain string names.  Resolving never raises:
    an unknown provider or a failing provider yields an empty sequence so the
    user can always keep typing manually.
    """

    def __init__(self) -> None:
        self._providers: dict[str, ProviderGetCandidates] = {}

    def register(self, name: str, fn: ProviderGetCandidates) -> None:
        self._providers[name] = fn

    def candidate(self, ctx: CompletionContext, provider_name: str) -> list[CompletionCandidate]:
        fn = self._providers.get(provider_name)
        if fn is None:
            return []
        try:
            result = fn(ctx)
        except Exception:
            return []
        if result is None:
            return []
        return list(result)


def dynamic_candidates(
    raw: str, cursor_position: int | None, registry: CompletionProviderRegistry, *, limit: int = 20
) -> list[CompletionCandidate]:
    """Return capped dynamic candidates for the current argument slot.

    Only arguments whose declared provider resolves to non-empty values are
    offered.  Results are capped to ``limit`` so a huge local store never
    floods the suggestion list. Returns [] for non-command lines.
    """

    if not isinstance(raw, str) or not raw.lstrip().startswith("/"):
        return []
    ctx = parse_completion_line(raw, cursor_position)
    if not ctx.is_command or not ctx.tokens:
        return []

    rules = dynamic_argument_rules(ctx.command)
    spec = _dynamic_slot_spec(ctx, rules)
    if spec is None or not spec.value_provider:
        return []
    candidates = registry.candidate(ctx, spec.value_provider)
    return candidates[:limit]


def _dynamic_slot_spec(
    ctx: CompletionContext, rules: DynamicArgumentRules
) -> CompletionArgumentSpec | None:
    tokens = ctx.tokens
    if len(tokens) < 2:
        return None

    # Determine the index of the token under the cursor. Boundary at the very
    # end counts as a fresh argument slot after the command.
    current_index = None
    for index, token in enumerate(tokens):
        if token.start <= ctx.cursor_position <= token.end:
            current_index = index
            break
    if current_index is None and ctx.cursor_position >= tokens[0].end:
        current_index = len(tokens)
    if current_index is None:
        current_index = 0

    # Option value being typed: an option token appears at or before the
    # cursor and declares a dynamic value (e.g. `/provider add p --model <名>`)
    for idx, token in enumerate(tokens):
        if idx >= current_index or token.text.startswith("--") is False:
            continue
        spec = rules.option_values.get(token.text)
        if spec is not None:
            return spec

    # Argument slot: the second token may name a declared subcommand whose
    # first value is dynamic. `/mcp restart lo` -> value at index 2.
    # Otherwise the first argument itself is dynamic. `/model dee` -> index 1.
    first_arg = tokens[1].text.lower()
    pos_spec = rules.positionals.get(first_arg)
    if pos_spec is not None:
        return pos_spec if current_index >= 2 else None
    default_spec = rules.positionals.get("")
    if default_spec is not None and current_index >= 1:
        return default_spec
    return None


