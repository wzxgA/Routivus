"""Configuration for the local Routivus Web Console server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _split_paths(raw: str) -> tuple[Path, ...]:
    return tuple(Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip())


@dataclass(frozen=True)
class ServerConfig:
    """Resolved server settings.

    The default workspace is the directory from which the server is started.
    A deployment can explicitly provide ``ROUTIVUS_WORKSPACE_ROOTS`` using the
    platform path separator (``;`` on Windows, ``:`` on POSIX).
    """

    projects_file: Path
    workspace_roots: tuple[Path, ...]
    host: str = "127.0.0.1"
    port: int = 18_765
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "testserver")
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ServerConfig":
        values = env if env is not None else os.environ
        user_dir = Path(values.get("ROUTIVUS_USER_DIR", "~/.routivus")).expanduser()
        projects_file = Path(
            values.get("ROUTIVUS_PROJECTS_FILE", str(user_dir / "projects.json"))
        ).expanduser()

        roots = _split_paths(values.get("ROUTIVUS_WORKSPACE_ROOTS", ""))
        if not roots:
            roots = (Path.cwd(),)

        hosts = tuple(
            item.strip()
            for item in values.get("ROUTIVUS_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
            if item.strip()
        )
        origins = tuple(
            item.strip()
            for item in values.get("ROUTIVUS_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
        host = values.get("ROUTIVUS_SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(values.get("ROUTIVUS_SERVER_PORT", "18765"))
        except ValueError:
            port = 18_765
        if not 1 <= port <= 65_535:
            port = 18_765
        return cls(
            projects_file=projects_file,
            workspace_roots=roots,
            host=host,
            port=port,
            allowed_hosts=hosts,
            allowed_origins=origins,
        )
