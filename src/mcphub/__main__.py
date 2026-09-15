"""Entry point: `python -m mcphub`."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .app import create_app
from .config import Settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging.getLogger(__name__).info(
        "mcphub starting — public URL %s, data in %s", settings.public_url, settings.data_dir
    )
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        # Behind a reverse proxy the forwarded scheme and host are what make
        # the OAuth issuer match what the client actually fetched.
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
