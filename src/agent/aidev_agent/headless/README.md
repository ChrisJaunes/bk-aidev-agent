# Headless runtime

The SDK owns both stdio runtimes. The TUI no longer needs a separate Python
`worker` package. Both use NDJSON on stdin/stdout and send diagnostics to stderr.

## Entry points

```bash
# Embedded sessions: no platform calls; existing protocol and hooks unchanged
python -m aidev_agent.headless

# Platform-backed TUI: versioned frames and raw AG-UI events
python -u -m aidev_agent.headless --protocol tui
```

The protocol is selected before importing the SDK services. In TUI mode the
stdout guard, process configuration and optional evaluation egress guard are
installed before importing platform clients. Importing `aidev_agent.headless`
does not load either runtime or change the process environment.

Install TUI checkpoint support with `pip install "aidev-agent[headless]"`.
For tracing, install `"aidev-agent[headless,opentelemetry]"`. SQLite saver 3.1
requires `langgraph-checkpoint>=4.1.0` (the `DeltaChannelHistory` interface).
The headless extra also declares LangGraph 1.2+ for the newer prebuilt/runtime
interfaces. The SDK declares OpenAI 2.54+ for its native run-scoped clients.

## Migration boundary

The two resource managers deliberately remain separate:

- `resource_manager.HeadlessResourceManager` implements no-platform, injected
  configuration and an in-memory ledger for embedded sessions.
- `local_resources.LocalResourceManager` subclasses the SDK platform manager;
  it loads platform resources plus per-agent declarations and MCP definitions.

Replacing the first with the second would introduce network calls into embedded
mode. Combining their protocols would also break existing hosts. Neither change
is part of this migration.

| Module | Responsibility |
| --- | --- |
| `__main__` | Explicit protocol selection |
| `cli`, `protocol`, `session` | Existing embedded sessions and lifecycle |
| `events`, `hooks`, `interrupts`, `errors` | Existing AG-UI and host hook integration |
| `runtime` | TUI stdout guard, signals, tracing, incremental stdin and EOF lifecycle |
| `dispatch` | TUI method routing, authentication and concurrent run tracking |
| `frame_protocol` | Versioned frame constructors and serialized bounded writer |
| `agent_factory` | SDK assembly with platform persistence and local file runtime |
| `agent_runner` | Execution, event forwarding, cancellation and shared process state |
| `config` | `bkai_tui.json`, environment bootstrap and credentials |
| `local_resources` | Per-agent declarations, MCP resolution, platform resources and workspace index |
| `checkpointer` | Persistent SQLite checkpoints across SDK event loops |
| `network_guard` | Opt-in evaluation egress restrictions; disabled normally |
| `utils` | BKAI state paths and file/stderr logging |

## TUI wire contract

The envelope and method names are unchanged. Methods include `session.start`,
`session.cancel`, `session.create`, `session.get`, `session.list`, `auth.login`,
`auth.logout`, `agent.info`, `config.info`, `config.set`, `agent.llms`, and
`process.shutdown`. Response kinds are `ready`, `started`, `event`, `completed`,
`result`, and `error`. Correlate interleaved frames by `id` and `runId`, not order.

```bash
printf '{"id":"shutdown","method":"process.shutdown","params":{}}\n' \
  | python -u -m aidev_agent.headless --protocol tui
```

Expected: `ready`, `result`, exit 0. Stdin EOF cancels active runs and closes the
writer and checkpoint saver. SIGTERM uses the same shutdown path. The process
never listens on a port and is not a daemon.

TUI configuration remains at `~/.bkai/bkai_tui.json` and the workspace override
under `<AGENT_TUI_CWD>/.bkai/`; per-agent declarations and MCP merge semantics
are unchanged. `AGENT_TUI_CWD` remains the user workspace, not the Python module
directory. Authentication persistence still follows `TuiConfig`: `auth.login`
writes the configured auth state, and `auth.logout` clears it; protocol responses
never echo credentials.

## Run-scoped model retry

Rate-limit handling lives in `aidev_agent.packages.langchain_core.models.llm_retry`.
`ChatModel` uses SDK-owned synchronous and asynchronous OpenAI clients; no global
OpenAI classes are modified and there is no startup patch installation. Explicit
caller-supplied completion clients remain under caller control.

Headless binds `RetryState` only when `AGENT_TUI_LLM_RATE_LIMIT_RETRIES` is positive,
using `AGENT_TUI_LLM_RATE_LIMIT_WAIT_BUDGET` as its wait budget in seconds. Without
a bound state, existing OpenAI and SDK retry behavior is unchanged. A bound state
shares its budget across model calls and fallbacks, honors `Retry-After`, supports
cancellation and emits `llm_rate_limit_retry` status events. Exhaustion is terminal
for that state; a started successful stream is never replayed.

SDK callers can bind `RetryState` directly, without importing headless.

## Verification

From `src/agent`, with the headless and test dependencies installed:

```bash
python -m pytest tests/headless -q
```

`test_tui_*` contains the migrated worker regression suite. The other tests
continue to validate embedded sessions, no-platform guarantees and hooks.

During development, install this SDK checkout into the TUI interpreter or set
`PYTHONPATH` to this checkout's `src/agent`. A previously published SDK with the
same version number does not contain this migration; installing that release
is not sufficient until a release containing these changes is published.
