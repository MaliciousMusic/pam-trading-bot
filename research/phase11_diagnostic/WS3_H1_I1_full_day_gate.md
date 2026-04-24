# WS3 H1.I1 — Full-day determinism gate PASS

Follow-up docs commit for the full-day determinism verification
promised in commit `3e2c3b6` ("Full-day gate follow-up commit").

## Result

`python -m scripts.verify_determinism --day 2026-04-20` — **PASS**.

| field                  | value                                    |
|:-----------------------|:-----------------------------------------|
| Day                    | 2026-04-20                               |
| Synth rate             | 60 (default)                             |
| Seed                   | 42 (default)                             |
| Max ticks              | None (full day)                          |
| Run A sha256           | `1fc982a69f3a1df5…` (260,143,564 bytes)  |
| Run B sha256           | `1fc982a69f3a1df5…` (260,143,564 bytes)  |
| Verdict                | `decisions.jsonl` byte-identical across runs |
| Wall clock             | ~3 h 40 min (13:40:59 → 17:20 UTC+2)     |
| HEAD at gate start     | `3e2c3b6` (the H1.I1 instrumentation commit) |

Log: [_ws3_h1_I1_gate/full_day.log](research/phase11_diagnostic/_ws3_h1_I1_gate/full_day.log)

## Significance

Satisfies `WS3_H1_SCOPE.md` §3 ("Determinism gate must PASS on the
instrumentation commit before Phase 1 measurement ships"). H1 Phase 1
measurement is now unblocked.

## Parallel-workstream note

Between the gate start (HEAD = `3e2c3b6`) and this follow-up commit,
four parallel audits landed as docs-only commits per
`WS3_H1_SCOPE.md` §11:

- `c46670a` — WS5 harvest-path audit
- `9bd1d86` — WS4 regime-hysteresis audit
- `cb512fc` — WS2 executor parity audit
- `05e3b05` — WS3b warmup/persistence audit

None touched code, so the gate result at `3e2c3b6` remains the
authoritative determinism-gate pass for the instrumentation surface
H1 Phase 1 will measure against.

## Observed during run

Kernel calibrator clamp saturation at `hi=4.00` throughout — first
refit triggers clamp and stays saturated for the remainder of the
day. Matches the MEASUREMENT_INFRA.md §2 "400× weaker pressure"
sprint-tape signature and is orthogonal to the determinism question
(both runs clamp identically, decisions.jsonl byte-identical). See
`WS3b_warmup_persistence_audit.md` §2.2 for the structural read.
