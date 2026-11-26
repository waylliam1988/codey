"""Release the launched browser process and its owned temporary profile."""
from __future__ import annotations

import asyncio
import errno
import shutil
import subprocess
import time
from pathlib import Path


async def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is not None and process.poll() is None:
        try:
            await asyncio.to_thread(process.wait, timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=2)


async def remove_profile(profile: Path | None) -> None:
    if profile is None:
        return
    deadline = time.monotonic() + 3
    while profile.exists():
        try:
            await asyncio.to_thread(shutil.rmtree, profile)
        except FileNotFoundError:
            # Chromium may remove its lockfiles while the tree is being read.
            if time.monotonic() >= deadline and profile.exists():
                raise
        except OSError as exc:
            # Shutdown can finish before Chromium releases its cache handles.
            if (not isinstance(exc, PermissionError) and exc.errno != errno.ENOTEMPTY) or time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.1)
