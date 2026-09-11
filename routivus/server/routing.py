"""把 SmartRouter 接入服务端会话轮次。

路由算法（`routivus.router`）与校准 / 自学习 / ML 精判 / 反馈采集此前只有 TUI
控制器在用，而 TUI 在本仓库没有入口（`[project.scripts]` 为空、无 Textual 层），
于是「按任务复杂度自动换档」在可运行路径上从未发生。

这里按 `SessionController._route_user_turn`（`routivus/tui/controller.py:272-307`）
同样的顺序接线：

    路由 → 采集反馈信号并落盘 → 切换 agent.llm → 推进防降级上下文

与 TUI 的两处自觉差异（都是行为等价或更稳妥）：

1. 不注入 `Hysteresis` —— TUI 也没有传（只靠 prev_tier / prev_ts 的 600s 防降级）。
2. **重活不在事件循环里做**：校准 / 自学习 / ML 精判（含 23.9 MB 的语义 ONNX
   会话）是同步 CPU + 磁盘负载，且原实现放在 `SessionRouter.__init__` 里。
   服务端会有多个会话，那种写法既会每会话重复加载，又会在首轮把整个 uvicorn
   事件循环占死（表现为"一对话就卡住"、连心跳与其它 HTTP 请求都不响应）。
   现在：重资产做成**进程级单例**（只加载一次），且由调用方用
   `asyncio.to_thread` 丢到工作线程执行，并带超时兜底。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from routivus.router import TIER_NAMES

logger = logging.getLogger("routivus.server.routing")

# 路由（含首次模型加载）的超时上限：超时只降级为「本轮不换档」，不阻断对话。
DEFAULT_ROUTE_TIMEOUT = 120.0

# 超过阈值就用 WARNING 打点：INFO 默认不会进桌面壳的日志，而这两个数字正是
# 排查"开了智能路由就卡"的关键证据。
_SLOW_LOAD_SECONDS = 2.0
_SLOW_ROUTE_SECONDS = 1.0


def route_timeout_seconds() -> float:
    """路由超时（秒）。可用 ``ROUTIVUS_ROUTER_TIMEOUT`` 覆盖，下限 5s。"""
    raw = os.environ.get("ROUTIVUS_ROUTER_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_ROUTE_TIMEOUT
    try:
        return max(5.0, float(raw))
    except ValueError:
        return DEFAULT_ROUTE_TIMEOUT


class _SharedAssets:
    """进程级共享的重资产：校准、自学习规则、ML 精判（含语义编码器）。

    只读使用（`route()` 不修改它们），所以跨会话共享是安全的；这样 24 MB 的
    ONNX 会话与 LightGBM 模型在整个服务进程里只加载一次。
    """

    __slots__ = ("calibration", "learned", "ml", "load_seconds")

    def __init__(self, calibration: Any, learned: Any, ml: Any, load_seconds: float) -> None:
        self.calibration = calibration
        self.learned = learned
        self.ml = ml
        self.load_seconds = load_seconds


_shared_lock = threading.Lock()
_shared_assets: _SharedAssets | None = None

# 预热只做一次；失败则复位，允许后续重试（首轮对话的惰性加载仍是兜底）。
_prewarm_lock = threading.Lock()
_prewarm_started = False


def shared_assets() -> _SharedAssets:
    """惰性加载重资产；锁内二次检查，避免并发的首轮重复加载。"""
    global _shared_assets
    if _shared_assets is not None:
        return _shared_assets
    with _shared_lock:
        if _shared_assets is None:
            from routivus.adaptive.calibrate import recalibrate
            from routivus.adaptive.learned_rules import re_learn
            from routivus.router.ml_router import MLRouter
            from routivus.router.semantic import load_semantic_encoder

            started = time.perf_counter()
            _shared_assets = _SharedAssets(
                calibration=recalibrate(),
                learned=re_learn(),
                ml=MLRouter(semantic=load_semantic_encoder()),
                load_seconds=0.0,
            )
            _shared_assets.load_seconds = time.perf_counter() - started
            log = logger.warning if _shared_assets.load_seconds >= _SLOW_LOAD_SECONDS else logger.info
            log("smart router: 重资产加载完成 %.2fs", _shared_assets.load_seconds)
    return _shared_assets


def reset_shared_assets() -> None:
    """丢弃共享重资产（进程级缓存失效时使用，例如产物被重新训练）。"""
    global _shared_assets, _prewarm_started
    with _shared_lock:
        _shared_assets = None
    with _prewarm_lock:
        _prewarm_started = False


def prewarm_shared_assets(*, blocking: bool = False) -> bool:
    """预热重资产（幂等）。

    默认在后台线程做，供**运行时**开关路径调用（不能阻塞事件循环）。
    ``blocking=True`` 则在当前线程同步完成，供**事件循环启动前**调用。

    为什么启动路径必须同步：SmartRouter 的首次重型 import（numpy / sklearn /
    lightgbm / onnxruntime…）若发生在后台线程里，会与事件循环首次创建
    AnyIO worker 线程（starlette 静态资源的 ``os.stat`` 走 ``to_thread``）
    抢同一个导入锁，两者一起死锁 —— 桌面端表现为握手 / ``/healthz`` 都通过，
    但加载首页的 ``GET /`` 永远不返回、窗口打不开。

    返回 True 表示本次真的触发了加载；False 表示已在加载或已加载完成。
    """
    global _prewarm_started
    with _prewarm_lock:
        if _prewarm_started or _shared_assets is not None:
            return False
        _prewarm_started = True
    if blocking:
        _prewarm_worker()
        return True
    threading.Thread(
        target=_prewarm_worker, name="routivus-router-prewarm", daemon=True
    ).start()
    return True


def smart_router_enabled(user_dir: "os.PathLike[str] | str | None" = None) -> bool:
    """读取用户级配置里的智能路由总开关；缺省或读取失败一律视为关闭。"""
    try:
        from pathlib import Path

        from routivus.config.manager import ConfigManager

        kwargs: dict[str, Any] = {"load_env": False}
        if user_dir is not None:
            base = Path(user_dir).expanduser()
            kwargs["user_dir"] = base
            kwargs["project_dir"] = base / ".no-project-context"
        return bool(ConfigManager(**kwargs).smart_router_config().get("enabled", False))
    except Exception:
        logger.debug("smart router: 读取开关失败，视为关闭", exc_info=True)
        return False


def prewarm_shared_assets_if_enabled(
    user_dir: "os.PathLike[str] | str | None" = None, *, blocking: bool = True
) -> bool:
    """开关开启时预热重资产；供事件循环启动前的主线程路径调用（默认同步）。"""
    if not smart_router_enabled(user_dir):
        return False
    return prewarm_shared_assets(blocking=blocking)


def _prewarm_worker() -> None:
    global _prewarm_started
    try:
        shared_assets()
    except Exception:
        # 预热失败不致命：首轮对话仍会惰性加载兜底；复位后允许下次重试。
        with _prewarm_lock:
            _prewarm_started = False
        logger.warning("smart router: 预热失败，首轮对话将重新尝试", exc_info=True)


class SessionRouter:
    """会话级 SmartRouter 运行态：只保存会话相关的量。

    重资产（校准 / 自学习 / ML / 语义）走 `shared_assets()`，不在此初始化 ——
    构造函数必须足够便宜，才能在事件循环线程上安全调用。
    """

    def __init__(self, *, session_key: str = "") -> None:
        from routivus.adaptive.feedback import FeedbackRecorder

        # 防降级上下文（600s 内最多降一档），与 inline 主循环一致
        self.prev_tier: str | None = None
        self.prev_ts: float | None = None
        self.feedback = FeedbackRecorder(session=session_key)
        # 最近一次路由结果，供会话快照回显
        self.last: Any = None

    def apply(self, text: str, *, settings: Any, manager: Any, agent: Any) -> tuple[Any, str]:
        """对一轮输入做路由，并按结果切换 `agent.llm`。

        纯同步实现，**调用方必须用 `asyncio.to_thread` 执行**：
        `shared_assets()` 首次调用要加载 20+ MB 模型，直接跑会阻塞事件循环。

        返回 `(RouteResult, error)`；error 非空表示模型切换失败（该轮仍用原模型）。
        """
        from routivus.adaptive.signals import capture_turn_signals
        from routivus.router import route as route_turn
        from routivus.service.commands import _attach_model

        assets = shared_assets()
        tiers_cfg = manager.smart_router_config().get("tiers") or {}
        now = time.time()
        started = time.perf_counter()
        result = route_turn(
            text,
            prev_tier=self.prev_tier,
            prev_ts=self.prev_ts,
            ts=now,
            fallback_provider=settings.provider,
            fallback_model=settings.model,
            tiers_config=tiers_cfg,
            manager=manager,
            calibration=assets.calibration,
            learned_rules=assets.learned,
            ml_router=assets.ml,
        )
        route_seconds = time.perf_counter() - started
        # 先采集 clarify / cmd_retry / short_high_tier 并立即落盘：采集不依赖
        # 本轮切换是否成功，也不阻断防降级上下文的推进（与 TUI 一致）。
        capture_turn_signals(
            self.feedback,
            text,
            getattr(result, "features", None) or {},
            self.prev_tier,
            result.tier,
            TIER_NAMES,
        )
        self.feedback.flush()

        error = ""
        if (result.provider, result.model) != (settings.provider, settings.model):
            error = _attach_model(settings, manager, agent, result.provider, result.model) or ""
        if not error:
            self.prev_tier, self.prev_ts = result.tier, now
        self.last = result
        log = logger.warning if route_seconds >= _SLOW_ROUTE_SECONDS else logger.info
        log(
            "smart router: tier=%s model=%s load=%.2fs route=%.2fs%s",
            result.tier,
            result.model,
            assets.load_seconds,
            route_seconds,
            f" error={error}" if error else "",
        )
        return result, error


def router_payload(result: Any, error: str = "") -> dict[str, Any]:
    """`RouteResult` → 下行事件载荷（前端展示「本轮用了哪一档」）。"""
    if result is None:
        payload: dict[str, Any] = {"enabled": True, "tier": "", "provider": "", "model": ""}
    else:
        payload = {
            "enabled": True,
            "tier": str(getattr(result, "tier", "")),
            "tier_idx": int(getattr(result, "tier_idx", 0)),
            "provider": str(getattr(result, "provider", "")),
            "model": str(getattr(result, "model", "")),
            "configured": bool(getattr(result, "configured", False)),
            "confidence": float(getattr(result, "confidence", 0.0)),
            "hard_rule": bool(getattr(result, "hard_rule", False)),
        }
    if error:
        payload["error"] = error
    return payload
