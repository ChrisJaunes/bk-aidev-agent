# Phase 1: Spawn transport implementation

Implemented importable factory + JSON snapshot, bounded JSON event/request/reply transport, worker-local build/execute, cooperative cancel and terminate/kill/join, single-use worker, error evidence precedence, exit-code validation and side-effects warning. Added RunHandle shared-future shielding.

Actual outputs:
- Runtime baseline: 105 passed, 7 warnings in 12.30s.
- Final dedicated tests: 17 passed, 1 warning in 2.43s.
- Full suite: 708 passed, 159 warnings in 213.14s (0:03:33).
- Ruff scoped checks: All checks passed!
- Worker import: no SDK imported.
- git diff --check: clean.

Commands:

default runtime baseline: .venv/bin/python -m pytest tests/runtime -q --tb=short
process boundary: .venv/bin/python -m pytest tests/runtime/test_process_worker.py -q --tb=short
full suite: PYTHONPATH="$PWD" .venv/bin/python -m pytest tests -q --tb=short --maxfail=0

Initial full suite without PYTHONPATH stopped with 2 test subprocess import errors and 270 passes; supplying the repository test import path resolved both without source edits.

Scope caveat: Production execution is NOT migrated; only the independently verified worker primitive and shared-result protection are implemented. Phase 2–5 remain. No external-resource blocker established; the milestone is incomplete and must not be archived as complete.
