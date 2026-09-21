"""Kept minimal repros: one file per pinned contract, no raw JSON dumps.

A soak failure artifact (tens of thousands of recorded steps) is debugging
material, not a regression test: it lives in ``tests/stress/artifacts/``
(ignored) until shrunk, and only lands here once reduced to a fast,
deterministic test that names the contract it pins.
"""
