"""Bounded, process-local ACP conversation cache. No prompt bodies are retained.

A session is leased by removing it from the cache, and returned only after a
successful complete generation. Exact history fingerprints prevent appending
to an edited, compacted, forked or otherwise divergent conversation.
"""
from dataclasses import dataclass
import hashlib
import json
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def fingerprints(messages):
    """Ignore generated assistant call IDs, but retain result-to-call bindings."""
    calls, counter, out = {}, 0, []
    for message in messages:
        parts = []
        for part in message["parts"]:
            p = dict(part)
            kind = p.get("type")
            if kind == "tool_call":
                counter += 1
                tid = p.pop("id", "")
                if tid:
                    calls[tid] = counter
                args = p.get("arguments")
                if isinstance(args, str):
                    try:
                        p["arguments"] = json.loads(args)
                    except ValueError:
                        pass
            elif kind == "tool_result":
                tid = p.pop("id", "")
                p["call_ref"] = calls.get(tid, "unresolved:" + tid)
            if kind == "text":
                if parts and parts[-1].get("type") == "text":
                    parts[-1]["text"] += p.get("text", "")
                    continue
            parts.append(p)
        # Wire protocols split text chunks differently, and tool parsers trim
        # surrounding whitespace. Never normalize text inside tool arguments.
        if message["role"] == "assistant":
            parts = [dict(p, text=p["text"].strip()) if p.get("type") == "text" else p for p in parts]
            parts = [p for p in parts if p.get("type") != "text" or p["text"]]
        out.append(digest({"role": message["role"], "parts": parts}))
    return tuple(out)


@dataclass
class SessionRecord:
    scope: str
    account: str
    process: object
    epoch: int
    session: dict
    history: tuple
    touched: float


class SessionCache:
    def __init__(self, maximum=64, ttl=1800):
        self.maximum = max(1, maximum)
        self.ttl = max(1, ttl)
        self.entries = {}
        self.hits = 0
        self.misses = 0

    def valid(self, record):
        return (time.monotonic() - record.touched < self.ttl and record.process.alive
                and record.process.epoch == record.epoch)

    def candidates(self, scope, history):
        return [r for r in self.entries.values() if r.scope == scope and self.valid(r)
                and len(history) > len(r.history) and history[:len(r.history)] == r.history]

    def take(self, scope, history, process):
        records = [r for r in self.candidates(scope, history) if r.process is process]
        if not records:
            self.misses += 1
            return None
        record = max(records, key=lambda r: len(r.history))
        self.entries.pop((id(record.process), record.session["sessionId"]), None)
        self.hits += 1
        return record

    def put(self, record):
        self.entries[(id(record.process), record.session["sessionId"])] = record

    def evict(self):
        removed = []
        for key, record in list(self.entries.items()):
            if not self.valid(record):
                removed.append(self.entries.pop(key))
        while len(self.entries) > self.maximum:
            key = min(self.entries, key=lambda k: self.entries[k].touched)
            removed.append(self.entries.pop(key))
        return removed

    def discard_process(self, process):
        for key, record in list(self.entries.items()):
            if record.process is process:
                self.entries.pop(key)

    def has_process(self, process):
        return any(r.process is process and self.valid(r) for r in self.entries.values())
