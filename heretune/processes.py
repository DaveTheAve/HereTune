from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from typing import Any

_LOCK = threading.RLock()
_ACTIVE: set[subprocess.Popen[bytes]] = set()


def _register(proc: subprocess.Popen[bytes]) -> None:
    with _LOCK:
        _ACTIVE.add(proc)


def _unregister(proc: subprocess.Popen[bytes]) -> None:
    with _LOCK:
        _ACTIVE.discard(proc)


def active_process_count() -> int:
    with _LOCK:
        return sum(1 for proc in _ACTIVE if proc.poll() is None)


def _force_kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort hard kill, including descendants on Windows.

    FFmpeg normally has no children, but Windows pipe/network shutdown can leave a
    process stuck in kernel I/O long enough that ``terminate()`` alone is not a
    useful watchdog. ``taskkill /T /F`` is deliberately a last resort and is
    scoped to the exact PID HereTune created.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def terminate_process(proc: subprocess.Popen[bytes], grace: float = 0.75) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=max(0.05, grace))
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    _force_kill_tree(proc)
    try:
        proc.wait(timeout=0.75)
    except (OSError, subprocess.TimeoutExpired):
        pass


def terminate_all_processes() -> int:
    """Terminate every external process currently owned by HereTune.

    FFmpeg sampling jobs are the main reason a Windows console can look like
    Ctrl-C was ignored: Uvicorn received the interrupt, but worker threads were
    still blocked in child-process I/O. Keeping a small registry lets shutdown
    interrupt those jobs immediately.
    """
    with _LOCK:
        processes = [proc for proc in _ACTIVE if proc.poll() is None]
    for proc in processes:
        terminate_process(proc)
    return len(processes)



def start_process(command: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    """Start and register a long-running child process owned by HereTune."""
    proc = subprocess.Popen(list(command), **kwargs)
    _register(proc)
    return proc


def release_process(proc: subprocess.Popen[bytes]) -> None:
    """Remove a process from the owned-process registry after it exits/terminates."""
    _unregister(proc)


def run_capture(
    command: Sequence[str],
    *,
    timeout: float | None = None,
    stderr_to_stdout: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command while tracking it and always capture raw bytes.

    Raw-byte capture is deliberate. Windows Python otherwise decodes pipe
    threads using the active ANSI code page (often cp1252), which can crash on
    perfectly valid FFmpeg diagnostic bytes before the caller ever sees them.
    """
    stderr = subprocess.STDOUT if stderr_to_stdout else subprocess.PIPE
    proc = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=stderr,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    _register(proc)
    try:
        try:
            stdout, stderr_bytes = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Never perform an unbounded second communicate() here. A decoder
            # blocked on SMB/network I/O can survive terminate() briefly on
            # Windows and used to freeze the analysis worker indefinitely.
            terminate_process(proc)
            try:
                proc.communicate(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                _force_kill_tree(proc)
                try:
                    proc.communicate(timeout=0.75)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            raise exc
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=int(proc.returncode or 0),
            stdout=stdout or b"",
            stderr=None if stderr_to_stdout else (stderr_bytes or b""),
        )
    finally:
        _unregister(proc)
