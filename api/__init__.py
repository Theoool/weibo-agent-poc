"""只读 API 包。对外只暴露 `create_app`。"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
