from __future__ import annotations

import os
import signal
import sys
from contextlib import suppress

from codey.runtime.core.process_contracts import ChildProcess, ProcessTreeOwner


def terminate_process_tree(
    proc: ChildProcess,
    job: ProcessTreeOwner | None = None,
) -> None:
    """Terminate a process tree started in its own process group.

    Contract: ``proc`` comes from ``start_process()`` (POSIX
    ``start_new_session=True``, Windows Job Object) so ``proc.pid`` is the
    group leader. Arbitrary external ``Popen`` objects need their own
    direct-child termination path and must not share this helper.
    """
    _terminate_process_tree(proc, job)


def terminate_direct_child(proc: ChildProcess) -> None:
    """Terminate one direct child without group signalling."""
    with suppress(Exception):
        proc.terminate()
    with suppress(Exception):
        proc.wait(timeout=1)
    with suppress(Exception):
        if proc.poll() is None:
            proc.kill()
    with suppress(Exception):
        proc.wait(timeout=1)


def _process_group_exists(pgid: int) -> bool:
    """Check a process group without touching its (possibly reaped) leader."""
    try:
        _kill_process_group(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _terminate_process_tree(
    proc: ChildProcess,
    job: ProcessTreeOwner | None = None,
) -> None:
    if os.name == "nt":
        if job is not None:
            with suppress(Exception):
                job.terminate()
        with suppress(Exception):
            proc.kill()
        try:
            proc.wait(timeout=1)
        except Exception:
            with suppress(Exception):
                proc.kill()
            with suppress(Exception):
                proc.wait(timeout=1)
    else:
        # Processes from start_process() run in a new session, so the known
        # group id is proc.pid: no getpgid() on a possibly reaped parent.
        pgid = proc.pid
        if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
            # Not a real group leader (closed proc or non-process double):
            # fall back to direct-child termination instead of signalling
            # a meaningless group id.
            terminate_direct_child(proc)
            return
        try:
            _kill_process_group(pgid, signal.SIGTERM)
        except ProcessLookupError:
            with suppress(Exception):
                proc.terminate()
        except OSError:
            with suppress(Exception):
                proc.terminate()
        with suppress(Exception):
            proc.wait(timeout=1)
        # proc.wait() only reaps the direct child; grandchildren holding
        # pipes are judged by group existence, never by the parent's wait.
        if _process_group_exists(pgid):
            with suppress(Exception):
                _kill_process_group(pgid, 9)
            with suppress(Exception):
                proc.wait(timeout=1)


if sys.platform != "win32":
    def _kill_process_group(pgid: int, sig: int) -> None:
        os.killpg(pgid, sig)
else:
    def _kill_process_group(pgid: int, sig: int) -> None:
        raise ProcessLookupError("POSIX process groups are unavailable on Windows")
