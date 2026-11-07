# Kernel migration behavior vectors, version 1

The language-neutral vectors in `tests/fixtures/kernel_go_contract_v1.json`
record existing behavior. They are not generated during tests. The differential
harness feeds these same inputs to the independent Go reference and compares
its results with Python; the golden tests separately pin committed expectations.
Adding internal Python fields must not change this wire contract.

## Coordinates and identity

- Text result and passage offsets are half-open Unicode codepoint indexes.
  UTF-8 byte indexes and UTF-16 code units are different. No Unicode or newline
  normalization is permitted before slicing or hashing. The vectors include
  Chinese, an astral emoji, and a combining character.
- Context digest v1 is SHA-256 of UTF-8 JSON with sorted object keys, compact
  separators, `ensure_ascii=False`, and `allow_nan=False`. A string digest hashes
  its JSON string encoding, including quotes and escapes, not its raw text.
- Passage refs hash their existing URL/hash/offset/version framing. Pinning
  source/evidence versions changes identity. Existing identities are not
  regenerated using a new canonicalization algorithm.
- Workspace fingerprints use `sha256:` followed by 64 hexadecimal characters.
  A boolean is not an integer workspace revision. Missing identity is different
  from an explicitly malformed identity.

## Provider requests and replies

The six legacy method/args/kwargs representations must decode to explicit
requests, preserving tool declarations, call/result ids, user boundary input,
optional values and bound protocol windows. `send` and `send_turn` take a prompt;
result methods take receipts, and `_bound` methods additionally take the exact
window. The native reply retains finish, reasoning, raw data and tool arguments.

Later typed codecs must preserve this legacy decoder and its frozen artifacts.
Unsupported versions must be rejected. The known shape itself is not a grant of
permission to send: durable admission remains mandatory.

## Recovery and evidence

`tests/durable/test_prepared_provider_request.py` covers real process death at
initial/followup freeze, previous settlement, admission and response persistence,
plus lost commit acknowledgements. A request frozen but not admitted can execute
once; an admitted request with unknown outcome cannot be resent. Saving a reply
must not place the network operation inside a CAS retry loop. Recovery consumes
the frozen declarations rather than today's registry.

`tests/test_provider_not_sent_settlement.py` covers NOT_SENT versus MAYBE_SENT.
`tests/test_completion_incomplete_latest_observation_blocks.py` covers latest
incomplete observations preventing revival of an older successful verification.
The vectors capture the associated missing/invalid identity/result gap codes.

## Migration policy

This contract freezes existing behavior, not every accidental permissive input.
Stricter decoding and deterministic production fixes require explicit regression
tests and a documented behavior decision. Go integer bounds, numeric formatting,
missing/null presence, cancellation and map/slice ownership must be explicit in
the final codecs; Python-only checks cannot prove cross-language equivalence.


## Executable migration reference

Run `python -m tools.kernel_go_differential` with Go on PATH or `GO_EXECUTABLE`
pointing to the compiler. It builds `tools/go_contract`, runs Go vet/test,
and executes 615 deterministic differential cases (508 accepted, 107 rejected).
The local validation used Go 1.27.3 on Windows; CI has a separate Linux job.
Missing Go is an error, never a skipped equivalence check.

The reference covers six provider requests, reply codecs, malformed inputs,
Unicode spans, passage framing, canonical digests (including seeded finite
IEEE-754 cases), verification gaps and latest-row shadowing. The reducer slice
covers states without effects/batches and their cancellation/input guards.
It does not yet implement Go durable storage, effect/batch recovery, actual
networking, or full research execution. The existing Python crash/CAS tests
remain required evidence for those behaviors until an equivalent Go runtime
exists. Passing a bounded differential sample does not prove equivalence for
all possible JSON numbers or every runtime interleaving.

Provider counters are nonnegative int64. Existing legacy workspace revisions
are not universally bounded to int64; the reference preserves such integers
with arbitrary precision rather than silently adding a new wire restriction.
