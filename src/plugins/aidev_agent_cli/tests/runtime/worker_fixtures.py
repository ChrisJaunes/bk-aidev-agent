"""Importable worker factories: all behavior lives behind the real spawn boundary."""

import os
import signal
import threading
import time


def build(snapshot, context):
    if snapshot.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    context.emit({"type": "PID", "pid": os.getpid()})
    if snapshot.get("hang_build"):
        while True:
            time.sleep(0.01)
    return Script(snapshot)


class Script:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def execute(self, context):
        if self.snapshot.get("child_process"):
            import subprocess
            import sys

            child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"])
            context.emit({"type": "CHILD", "pid": child.pid})
            while True:
                time.sleep(0.01)
        if self.snapshot.get("threaded"):
            values = []

            def call(index):
                context.emit({"type": "THREAD", "index": index})
                values.append(context.request("before_execute", {"value": index}))

            threads = [threading.Thread(target=call, args=(index,)) for index in range(4)]
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads):
                context.cancelled()
                time.sleep(0.001)
            for thread in threads:
                thread.join()
            return sorted(values)
        if self.snapshot.get("hang_execute"):
            while True:
                time.sleep(0.01)
        if self.snapshot.get("cooperate"):
            while not context.cancelled():
                time.sleep(0.01)
        if self.snapshot.get("crash"):
            os._exit(17)
        if self.snapshot.get("error_event"):
            context.emit({"type": "RUN_ERROR", "message": "failed"})
        if self.snapshot.get("hook"):
            return context.request("before_execute", {"value": 3})
        return {"pid": os.getpid(), "value": self.snapshot.get("value")}

    def close(self):
        if self.snapshot.get("crash_close"):
            os._exit(19)
