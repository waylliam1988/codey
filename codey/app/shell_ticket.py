from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codey.policies.command_line import ShellSpec
from codey.policies.shell_analysis import ShellAnalysis


@dataclass(frozen=True)
class ShellExecutionTicket:
    """Single-use, immutable shell spawn authorization.

    Minted under the claim lock by ``claim_shell_ticket``: generation, stop
    flag, resolved cwd, and approval consumption happen together, so an
    Allow cannot outrun a concurrent Stop. The execution layer only accepts
    this ticket and re-validates it under the same lock immediately before
    Popen (spawn gate).
    """

    command: str
    cwd: Path
    generation: int
    timeout: int
    output_limit: int
    shell: ShellSpec
    analysis: ShellAnalysis
    execution_id: str = ""
    session_id: str = ""
    run_id: str = ""
