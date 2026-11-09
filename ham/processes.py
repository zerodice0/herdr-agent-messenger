"""PID-local identity reads; macOS enumeration never requests KERN_PROC_ALL."""
from __future__ import annotations

import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path
import sys
import time
from dataclasses import dataclass


class ProcessLookupFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent: int
    name: str
    started: tuple[int, ...]


class _ProcBSDInfo(ctypes.Structure):
    # Darwin libproc.h / sys/proc_info.h: PROC_PIDTBSDINFO, no pointer fields.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid",
        "svuid", "svgid", "rfu_1")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32), ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("nice", ctypes.c_int32),
        ("start_tvsec", ctypes.c_uint64), ("start_tvusec", ctypes.c_uint64)]


@lru_cache(maxsize=1)
def _libproc():
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.proc_listallpids.restype = ctypes.c_int
    return library


class ProcessSource:
    def __init__(self, *, platform: str | None = None, proc_root: str | Path = "/proc"):
        self.platform = sys.platform if platform is None else platform
        self.proc_root = Path(proc_root)

    @property
    def lookup(self) -> str:
        return "libproc" if self.platform == "darwin" else "procfs"

    def read(self, pid: int) -> ProcessInfo:
        if pid <= 0:
            raise ProcessLookupFailure("invalid_output")
        try:
            if self.platform == "darwin":
                info = _ProcBSDInfo()
                ctypes.set_errno(0)
                count = _libproc().proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
                if count != ctypes.sizeof(info):
                    code = ctypes.get_errno()
                    if code == errno.ESRCH:
                        raise ProcessLookupFailure("exited")
                    raise ProcessLookupFailure("unavailable" if count <= 0 else "invalid_output")
                name = bytes(info.name or info.comm).decode("utf-8", errors="replace")
                result = ProcessInfo(info.pid, info.ppid, name,
                                     (info.start_tvsec, info.start_tvusec))
            elif self.platform.startswith("linux"):
                directory = self.proc_root / str(pid)
                raw = (directory / "stat").read_text(errors="replace")
                head, separator, tail = raw.rpartition(") ")
                fields = tail.split()
                actual = int(head.split(" (", 1)[0])
                if not separator or len(fields) < 20:
                    raise ValueError
                result = ProcessInfo(actual, int(fields[1]), head.split(" (", 1)[1],
                                     (int(fields[19]),))
            else:
                raise ProcessLookupFailure("unsupported")
            if result.pid != pid or result.parent < 0 or not result.name or any(v < 0 for v in result.started):
                raise ValueError
            return result
        except FileNotFoundError:
            raise ProcessLookupFailure("exited") from None
        except (OSError, AttributeError):
            raise ProcessLookupFailure("unavailable") from None
        except (ValueError, IndexError):
            raise ProcessLookupFailure("invalid_output") from None

    def pids(self) -> list[int]:
        if self.platform != "darwin":
            raise ProcessLookupFailure("unsupported")
        try:
            library = _libproc()
            capacity = library.proc_listallpids(None, 0)
            if capacity <= 0:
                raise ProcessLookupFailure("unavailable")
            for _ in range(3):
                capacity += 128
                values = (ctypes.c_int * capacity)()
                count = library.proc_listallpids(values, ctypes.sizeof(values))
                if count <= 0:
                    raise ProcessLookupFailure("unavailable")
                if count < capacity:
                    return sorted({pid for pid in values[:count] if pid > 0})
                capacity = count
            raise ProcessLookupFailure("unstable")
        except (OSError, AttributeError):
            raise ProcessLookupFailure("unavailable") from None


def process_chain(*, source: ProcessSource | None = None, timeout: float = 5,
                  stop_at: int | None = None) -> list[ProcessInfo]:
    """Recheck caller ancestry through an explicit shell, or PID 1 if not reached."""
    if stop_at is not None and (type(stop_at) is not int or stop_at <= 1):
        raise ProcessLookupFailure("invalid_output")
    source = ProcessSource() if source is None else source
    deadline = time.monotonic() + timeout
    chain = []
    seen = set()
    pid = os.getpid()

    def read(current):
        if time.monotonic() >= deadline:
            raise ProcessLookupFailure("timeout")
        info = source.read(current)
        if time.monotonic() >= deadline:
            raise ProcessLookupFailure("timeout")
        if (not isinstance(info, ProcessInfo)
                or type(info.pid) is not int or info.pid != current
                or type(info.parent) is not int or info.parent < 1
                or not isinstance(info.name, str) or not info.name
                or not isinstance(info.started, tuple) or not info.started
                or any(type(value) is not int or value < 0 for value in info.started)):
            raise ProcessLookupFailure("invalid_output")
        return info

    while pid > 1:
        if pid in seen:
            raise ProcessLookupFailure("invalid_output")
        seen.add(pid)
        info = read(pid)
        if chain and info.started > chain[-1].started:
            raise ProcessLookupFailure("invalid_output")
        chain.append(info)
        if pid == stop_at:
            if info.parent in seen:
                raise ProcessLookupFailure("invalid_output")
            break
        pid = info.parent
    # A PID alone is not evidence if the process exited, was reused or reparented
    # while walking. Check the entire observed chain before returning any match.
    for info in reversed(chain):
        if read(info.pid) != info:
            raise ProcessLookupFailure("changed")
    return chain
