"""Process entry point for the API server."""

from __future__ import annotations

import logging

import uvicorn

from crwallm.config import get_settings


def _install_uvloop() -> str:
    """Use uvloop where available. Not on Windows — run the worker in the
    Linux container for the 2-4x event loop speedup (docs/12_PERFORMANCE.md).
    """
    try:
        import uvloop
    except ImportError:
        return "asyncio"
    uvloop.install()
    return "uvloop"


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    loop = _install_uvloop()
    logging.getLogger(__name__).info("event loop: %s", loop)

    uvicorn.run(
        "crwallm.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.is_dev,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
