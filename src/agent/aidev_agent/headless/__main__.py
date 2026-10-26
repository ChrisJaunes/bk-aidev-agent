# -*- coding: utf-8 -*-
"""``python -m aidev_agent.headless`` 入口。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
