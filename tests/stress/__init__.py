"""Codey fault-injection + restart-recovery acceptance system.

Only one question is asked here: after ANY fault and a restart, do the
durable facts converge to the same result? Process return codes are never
asserted; canonical durable state is.

Fault matrix (boundary x crash point -> restart expectation):

```text
boundary    crash point         restart  expected
provider    before send         yes      no effect row
provider    after send          yes      one pending intent (unknown outcome)
provider    before settle       yes      settle-or-recover, exactly once
tool        before execute      yes      safe replay only
tool        after execute       yes      no unsafe duplicate
delivery    before mark settled yes      delivery recovery (replayable batch)
shell       before Popen        yes      rejected, zero spawns
shell       after Popen claim   yes      ticket consumed, no second execution
ghost       after append        yes      projection rebuilds from log
sse         disconnect          yes      replay from Last-Event-ID
browser     worker crash        yes      generation isolation (stale discarded)
self-repair after start         yes      journal recovery, one logical repair
```

Rules for every scenario in this package:

- Deterministic seeds only; the same seed replays the same failure.
- Fakes only (no browsers, providers, shells, or networks).
- The FaultController never touches business state: it only injects
  timeout / kill / delay / duplicate / disconnect / crash / race.
- A restart is dropping every in-memory object and reopening the same
  state directory. Memory-only surfaces (event bus, approvals, worker
  threads) are documented as ephemeral and must rebuild EMPTY, never
  half-full.
"""
