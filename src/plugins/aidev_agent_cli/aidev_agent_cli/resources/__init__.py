# -*- coding: utf-8 -*-
"""Resources: source parsing, per-agent declarations, loaders and SDK resource manager.

No execution strategies or Session references live here. SDK runtime chooses the manager
from explicit accepted overrides; adapters choose only protocol transport. Package import
is lightweight and does not re-export SDK collaborators.
"""
