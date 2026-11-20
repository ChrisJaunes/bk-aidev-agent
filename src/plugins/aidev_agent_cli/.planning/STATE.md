# State

## Current Position
All five runtime refactor phases plus quick task `261109-wqo` (import boundaries) implemented and
verified. Final implementation verified and ready for local commit; no push or shared-system changes.

## Final Independent Verification
Command: `PYTHONPATH=$PWD .venv/bin/python -m pytest -q -o addopts="" --maxfail=0`
Actual result: **748 passed, 0 failed, 0 skipped, 8 warnings in 788.37s**
(baseline before `261109-wqo` was 734 passed; +14 new audit/contract tests).
`ruff check .`: All checks passed.
`python -m compileall -q aidev_agent_cli tests`: passed.
`git diff --check`: passed.
Function-level imports (AST): **11, UNJUSTIFIED 0** (was 65).
Warnings originate from pytest configuration and SDK deprecations; no failed tests or skips used to
waive refactor requirements.

## Delivered
Concrete cross-turn Session and per-run snapshots, explicit active/FIFO queues, bounded completed
results, shielded waiters, RuntimeService admission/closing gates and shared shutdown; spawned
production SDK build/execute, serialized IPC events/hooks/tools, dedicated process group termination
and descendant reaping; durable local history and staged continuation/memo commits after confirmed
worker cleanup; identity/settings revisions and startup workspace root; SDK isolation, explicit
resource overrides and all three adapters/tests/docs migrated.

Import boundaries (`261109-wqo`): pure value objects moved to `runtime.models` (zero SDK) and the
SDK executor to `runtime/sdk/executor.py` (module-top SDK deps), so `auth → sdk → auth` no longer
exists as a latent cycle; `runtime/boundary.py` holds IPC constants; `runtime/terminal.py` is the
single intentional parent→SDK edge (only `runner` imports it); `runtime/sdk/__init__.py` is a thin
zero-SDK export surface whose `build` is the one declared import-target boundary; worker startup
order is centralised in `infrastructure.startup.install_worker_startup`; OTel/bkapi optional
dependencies each have exactly one declared load point.

## Independent Review Closure
1. Terminal sink blocking: strict bounded batch delivery, transport error distinct from execution result.
2. Cancellation-resistant parent callback cleanup: bounded cleanup with tracked late callbacks.
3. Resume close crash clearing pending/memo: stage/commit after confirmed worker exit; failed cleanup discards staged state.
4. `261109-wqo`: value-object field parity and executor class-body parity re-checked by AST against
   the pre-refactor source; startup-order assertions were strengthened, not relaxed; `__main__`
   import proven side-effect free by subprocess; README tree/dependency table updated.
Added regression tests also verify controlled descendant cleanup, configuration identity/workspace
snapshots, import boundaries (AST + fresh-import), and worker bootstrap ordering.

## Explicit Limits
Force termination cannot undo already committed external tool/platform side effects. History and
LangGraph checkpoints are separate durable facts, not a distributed atomic transaction. Importable
tool factories are required across spawn; live parent SDK/client/SQLite objects are not supported.
Linux process-group and subreaper facilities implement hard termination. `runtime.terminal` and
therefore `runtime.service`/`runs`/`runner` transitively import the SDK — that is the single
declared parent→SDK edge, not an oversight (see `01-SUMMARY.md`, Deviations #1).
