"""训练 ML 模型命令的执行层：/train 的参数解析、确认提示与子进程流式执行。

设计口径（对齐 05 文档与现有 /provider 红线）：
- 训练仍是显式手动触发：不带 --yes 只返回确认提示，绝不静默跑。
- 训练跑在独立子进程 :file:`tools/train_router.py`，本层只「spawn + 收日志」，
  不 import 训练逻辑，规避 torch 等重依赖进主进程。
- inline 走同步版 :func:`run_training_sync`；TUI 走异步版 :func:`run_training_async`，
  两者共用参数解析与 argv 构造，日志都逐行流式上报。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from xg.tui.i18n import UiLanguage, normalize_language, translate

_TRAIN_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "train_router.py"
_TRAIN_DEPS = ("lightgbm", "sklearn", "joblib", "numpy")


@dataclass
class TrainPlan:
    dataset: str | None = None
    output: str | None = None
    overwrite: bool = False
    feedback_only: bool = False
    semantic: bool = True  # 默认带语义列：自动探测语义编码器；--no-semantic 关闭


def _default_output() -> Path:
    """默认产物路径：与 feedback.log 同目录的 router.lgb（对齐 train_router）。"""
    from xg.adaptive.store import data_dir  # noqa: PLC0415

    return data_dir() / "router.lgb"


def _default_semantic_onnx() -> Path | None:
    """自动探测默认语义编码器产物：数据目录 → 随包 assets 兜底，找到即返回路径。"""
    from xg.adaptive.store import data_dir  # noqa: PLC0415

    primary = data_dir() / "router_semantics.onnx"
    if primary.exists():
        return primary
    bundled = Path(__file__).resolve().parents[1] / "assets" / "router_semantics.onnx"
    return bundled if bundled.exists() else None


def check_train_deps(language: UiLanguage = "zh") -> str | None:
    """缺训练依赖时返回安装提示；全部可用返回 None。

    注意：语义编码器（bge）非必需——缺失时 train_router 自动回退 TF-IDF 特征。
    """
    missing = [m for m in _TRAIN_DEPS if _import_safe(m) is False]
    if missing:
        return translate(language, "ui.train.missing_deps", deps=", ".join(missing))
    return None


def _import_safe(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def parse_train_command(raw: str, language: UiLanguage = "zh") -> tuple[TrainPlan, str | None]:
    """解析 `/train [...]` 参数，返回 (plan, error)。错误时 plan 不保证完整。"""
    plan = TrainPlan()
    tokens = raw.split(maxsplit=1)[1].split() if len(raw.split(maxsplit=1)) > 1 else []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("--yes", "-y"):
            plan.overwrite = True
        elif t == "--feedback-only":
            plan.feedback_only = True
        elif t == "--no-semantic":
            plan.semantic = False
        elif t in ("--output", "--out", "-o"):
            i += 1
            if i >= len(tokens):
                return plan, translate(language, "ui.train.output_required")
            plan.output = tokens[i]
        elif t.startswith("-"):
            return plan, translate(language, "ui.train.unknown_option", option=t)
        else:
            if plan.dataset is not None:
                return plan, translate(language, "ui.train.extra_arg", arg=t)
            plan.dataset = t
        i += 1
    if plan.dataset and plan.feedback_only:
        return plan, translate(language, "ui.train.conflict")
    if not plan.dataset and not plan.feedback_only:
        plan.feedback_only = True  # 缺省：仅用 feedback.log
    if plan.output is not None:
        plan.output = plan.output.strip('"\'')
    return plan, None


def confirmation_message(plan: TrainPlan, language: UiLanguage = "zh") -> str:
    """未带 --yes 时返回的确认提示：告知将如何训练、需加 --yes 才执行。"""
    language = normalize_language(language)
    src = translate(language, "ui.train.feedback") if plan.feedback_only else translate(language, "ui.train.dataset", name=plan.dataset)
    out = plan.output or str(_default_output())
    sem_note = translate(language, "ui.train.semantic" if plan.semantic else "ui.train.no_semantic")
    return translate(language, "ui.train.confirm", source=src, output=out, semantic=sem_note)


def build_argv(plan: TrainPlan) -> list[str]:
    """构造 tools/train_router.py 的 argv（output 缺省时不传，交给脚本默认）。

    语义列默认启用：plan.semantic 为真时自动探测默认语义编码器产物并传入
    --semantic-onnx；未探测到或 --no-semantic 则不传，训练回退纯 TF-IDF 特征。
    """
    argv = [sys.executable, str(_TRAIN_SCRIPT)]
    if plan.output:
        argv += ["--out", plan.output]
    if plan.feedback_only:
        argv += ["--feedback-only"]
    elif plan.dataset:
        argv += [plan.dataset]
    if plan.semantic:
        onnx = _default_semantic_onnx()
        if onnx is not None:
            argv += ["--semantic-onnx", str(onnx)]
    return argv


def _build_env() -> dict[str, str]:
    """让子进程以 UTF-8 输出（Windows 默认 GBK 会与读取端 utf-8 解码冲突导致乱码）。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_training_sync(
    plan: TrainPlan, on_line: callable | None = None
) -> tuple[bool, list[str]]:
    """同步执行训练（inline 用）。逐行流式读子进程输出交给 on_line，返回 (ok, lines)。"""
    argv = build_argv(plan)
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_build_env(),
    )
    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if on_line is not None:
                on_line(line)
    finally:
        rc = proc.wait()
    return rc == 0, lines


async def run_training_async(
    plan: TrainPlan, on_line: callable | None = None
) -> tuple[bool, list[str]]:
    """异步执行训练（TUI / CommandService 用）。逐行流式上报，返回 (ok, lines)。"""
    argv = build_argv(plan)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_build_env(),
    )
    lines: list[str] = []
    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        lines.append(line)
        if on_line is not None:
            on_line(line)
    await proc.wait()
    return (proc.returncode or 0) == 0, lines
