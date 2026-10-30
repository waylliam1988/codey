"""Synthesis intent; current facts are supplied by the kernel working view."""

from codey.research.tools import ResearchTools


def synthesis_context(tools: ResearchTools) -> str:
    return ("Synthesize the saved evidence using the current Research observations. "
            "Audit claims against original passages; preserve conditions and specific gaps. "
            "Do not copy the earlier report or acquisition receipt.")
