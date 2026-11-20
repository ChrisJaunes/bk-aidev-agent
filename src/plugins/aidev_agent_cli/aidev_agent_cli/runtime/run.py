"""Per-turn ownership; no worker or sink is stored on a cross-turn Session."""

from dataclasses import dataclass

from .contracts import EventConsumer, RunHandle
from .models import HookHandler, TurnSnapshot
from .worker import ProcessWorker


@dataclass
class Run:
    handle: RunHandle
    snapshot: TurnSnapshot
    worker: ProcessWorker
    sink: EventConsumer | None
    hook_handler: HookHandler | None

    @property
    def hooks(self):
        return self.snapshot.hooks
