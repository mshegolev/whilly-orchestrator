"""Mount the design-system static assets onto a FastAPI app.

Helper kept under ``whilly/api/`` so the assets live alongside
``whilly/api/static/`` and ``whilly/api/templates/``. Called from
:mod:`whilly.adapters.transport.server` right after the app is built.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

STATIC_DIR: Path = Path(__file__).resolve().parent / "static"


def mount_static_assets(app: FastAPI) -> None:
    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR),
        name="static",
    )


__all__ = ["STATIC_DIR", "mount_static_assets"]
