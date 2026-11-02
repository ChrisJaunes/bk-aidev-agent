# -*- coding: utf-8 -*-
"""Headless stdio runtime with embedded and platform-backed TUI protocols.

``python -m aidev_agent.headless`` runs the no-platform session protocol.
``python -m aidev_agent.headless --protocol tui`` runs the TUI frame protocol.
Imports stay lightweight so TUI configuration precedes SDK initialization.
"""

from __future__ import annotations

from typing import TextIO

from .errors import HeadlessError

__all__ = ["HeadlessError", "main", "serve"]


def main(argv: list[str] | None = None) -> int:
    from .__main__ import main as entry_main

    return entry_main(argv)


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    from .cli import serve as serve_embedded

    return serve_embedded(stdin, stdout)
