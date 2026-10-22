"""Shared types for native wake client adapters (Claude Code, Codex)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class WakeTarget:
    client: str              # 'claude' | 'codex'
    session_ref: str         # claude sessionId (UUID) / codex thread id
    pid: int                 # native root process id
    journal_path: str        # absolute: claude transcript .jsonl / codex rollout .jsonl
    journal_offset: int      # st_size at discovery; observation starts here
    extra: Mapping[str, Any] = field(default_factory=dict)  # adapter-private (socket path, codex_home, binary)


class WakeUnavailable(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class WakeDeliveryFailed(Exception):
    def __init__(self, code: str, message: str, *, definitely_not_delivered: bool):
        super().__init__(message)
        self.code = code
        self.message = message
        self.definitely_not_delivered = definitely_not_delivered


@dataclass(frozen=True)
class Observation:
    status: str              # 'accepted' | 'pending' | 'gone'
    offset: int              # next scan offset (bytes)
    evidence: str | None = None   # non-empty when accepted
