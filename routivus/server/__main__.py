"""Run the local Routivus Web Console server with ``python -m routivus.server``."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading

import uvicorn

from routivus.server.config import ServerConfig

# 桌面壳据此识别握手行；保持纯 ASCII，避免父进程解析时踩编码问题。
READY_PREFIX = "ROUTIVUS_DESKTOP_READY "

_FALSEY = ("", "0", "off", "false", "no")


def _desktop_requested(args: argparse.Namespace) -> bool:
    if args.desktop:
        return True
    return os.environ.get("ROUTIVUS_DESKTOP", "").strip().lower() not in _FALSEY


def _bind_socket(host: str, port: int) -> socket.socket:
    """先自己 bind，才能在 ``port=0`` 时拿到操作系统分配的真实端口。

    uvicorn 要到 ``Server.run`` 之后才知道端口，而桌面壳在拉起进程后需要立刻
    知道该访问哪里，所以这里提前绑定，再把 socket 交给 uvicorn 复用。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


def _watch_stdin_for_shutdown(server: uvicorn.Server) -> None:
    """桌面壳通过关闭子进程 stdin 触发优雅退出。

    Windows 上 Node 的 ``child.kill()`` 走的是 TerminateProcess，没有优雅停机；
    硬杀会跳过 lifespan，终端进程树只能靠 Job Object 兜底。这里用 stdin EOF
    做一次显式停机，让 lifespan 正常回收终端，失败再退化为强杀。
    """

    def _run() -> None:
        try:
            while True:
                line = sys.stdin.readline()
                if line == "":  # EOF：父进程关闭了 stdin
                    break
                if line.strip().lower() in ("shutdown", "quit", "exit"):
                    break
        except Exception:  # pragma: no cover - stdin 异常一律按停机处理
            pass
        server.should_exit = True

    threading.Thread(target=_run, name="routivus-stdin-watch", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m routivus.server")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help=(
            "桌面模式：就绪后向 stdout 打一行握手 JSON（端口等），"
            "并在 stdin 关闭时优雅退出。也可用 ROUTIVUS_DESKTOP=1 开启。"
        ),
    )
    args = parser.parse_args()

    desktop = _desktop_requested(args)
    config = ServerConfig.from_env()

    try:
        sock = _bind_socket(config.host, config.port)
    except OSError as exc:
        print(f"无法绑定 {config.host}:{config.port} —— {exc}", file=sys.stderr, flush=True)
        if desktop:
            print(
                READY_PREFIX + json.dumps({"error": "bind_failed", "detail": str(exc)}),
                flush=True,
            )
        raise SystemExit(2) from exc

    bound_port = int(sock.getsockname()[1])

    server = uvicorn.Server(
        uvicorn.Config(
            "routivus.server:create_app",
            factory=True,
            host=config.host,
            port=bound_port,
            log_level="info",
        )
    )

    if desktop:
        _watch_stdin_for_shutdown(server)
        # 握手在绑定成功之后、accept 之前打印；父进程拿到端口后轮询 /healthz 确认就绪。
        print(
            READY_PREFIX
            + json.dumps(
                {
                    "port": bound_port,
                    "host": config.host,
                    "static": bool(config.static_dir),
                }
            ),
            flush=True,
        )

    try:
        server.run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    main()
