# -*- coding: utf-8 -*-
"""parent ↔ worker IPC 的纯常量边界（只依赖标准库，无本包 import）。

这两个常量同时被父进程（``worker`` / ``runs``）与 worker 侧使用，且都是「跨进程协议
参数」而不是某个实现模块的私事：把它们放在最轻的叶子里，父进程引用它们就不必导入
worker 的实现模块（``runtime.worker`` 仍是再导出面，见其 ``__all__``）。
"""

from __future__ import annotations

#: 单帧 JSON 上限。父进程与子进程都按它切片，超出即拒绝。
MAX_FRAME_BYTES = 256 * 1024

#: 合作取消的等待上限（秒）。超时后进入 terminate → kill → join 的强制终止路径。
CANCEL_GRACE_SECONDS = 2.0

#: 收尾时等待后台回调结束的上限（秒）。
CALLBACK_CLEANUP_SECONDS = 0.1


__all__ = ["CALLBACK_CLEANUP_SECONDS", "CANCEL_GRACE_SECONDS", "MAX_FRAME_BYTES"]
