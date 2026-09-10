"""Run the local Routivus Web Console server with ``python -m routivus.server``."""

from __future__ import annotations

import uvicorn

from routivus.server.config import ServerConfig


def main() -> None:
    config = ServerConfig.from_env()
    uvicorn.run(
        "routivus.server:create_app",
        factory=True,
        host=config.host,
        port=config.port,
    )


if __name__ == "__main__":
    main()
