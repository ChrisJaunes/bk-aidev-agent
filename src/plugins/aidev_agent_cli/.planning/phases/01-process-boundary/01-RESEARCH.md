# Research

Python3.11 multiprocessing spawn requires importable top-level entry and pickleable bootstrap arguments. Use only import-target string and JSON bytes; never pickle live SDK objects. Pipe send/recv can block, so send_bytes small bounded frames and parent poll with loop sleeps. Worker serially handles reply while parent services async hook. Cancel while build is stuck is observed by parent, not dependent on worker polling. terminate then timed join then kill then timed join; failure to reap raises and cannot release slot. JSON envelopes avoid unpickling arbitrary payload on the parent event channel. Tests must force SIGTERM-ignore to prove kill escalation.

No Agent tool available in this session catalog; research/planning/checking performed inline rather than creating conflict-prone agents.
