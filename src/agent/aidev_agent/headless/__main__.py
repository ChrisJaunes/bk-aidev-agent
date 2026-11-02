# -*- coding: utf-8 -*-
"""``python -m aidev_agent.headless`` protocol selection before SDK imports."""

from __future__ import annotations

import argparse
import asyncio


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the aidev-agent headless stdio runtime")
    parser.add_argument(
        "--protocol", choices=("headless", "tui"), default="headless",
        help="headless: no-platform sessions (default); tui: platform-backed versioned frames",
    )
    args = parser.parse_args(argv)
    if args.protocol == "tui":
        from .runtime import main as run_tui

        return asyncio.run(run_tui())

    from .cli import main as run_headless

    return run_headless([])


if __name__ == "__main__":
    raise SystemExit(main())
