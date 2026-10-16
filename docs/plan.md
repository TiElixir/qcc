# qcc implementation plan

## Goal

Build `qcc`, a Python compiler for a deliberately bounded pure-C subset. It
invokes Clang, parses textual LLVM IR, lowers fixed-width integer functions to
reversible X/CX/CCX circuits, emits OpenQASM 3, and reports resources. The
baseline compiler uses out-of-place computation followed by Bennett
compute-copy-uncompute so every temporary returns to its initial value. The
v0.4 release contract adds semantics-preserving optimizer levels and measured
before/after reporting without widening the source subset.

## Repository and toolchain

- Python 3.10+ with a `src/` layout and Hatchling packaging.
- Clang is the only required compiler frontend; qcc does not parse C source.
- The LLVM parser is a small project-owned parser for the supported textual IR
  subset. It accepts compiler-version variations in attributes and SSA names.
- Runtime code uses only the Python standard library. Pytest, Hypothesis, Ruff,
  and mypy are development dependencies. Qiskit is optional.
- The public package is `qcc`; the console command is also `qcc`.

## Milestones

### v0.1 — straight-line uint8_t

**Status:** Complete and tagged `v0.1.0`.

1. Bootstrap packaging, CI, errors, and documentation.
2. Invoke Clang with optimized, non-vectorized textual LLVM output.
3. Parse typed single-block SSA for integer operations, casts, and returns.
4. Implement immutable-friendly X/CX/CCX netlists and a classical simulator.
5. Implement Cuccaro modular addition, subtraction, and Bennett scheduling.
6. Lower `add`, `sub`, `and`, `or`, `xor`, bitwise-not patterns, `zext`, and
   `trunc`.
7. Expose `compile_c`, `Circuit`, layout, simulation, reports, and a minimal
   OpenQASM 3 emitter.
8. Differential-test native C and the reversible simulator exhaustively for
   the required small domains.

Acceptance commands:

```text
ruff check .
ruff format --check .
mypy --strict src
pytest -q
```

### v0.2 — comparisons, predication, and bounded loops

**Status:** Complete and tagged `v0.2.0`.

1. Add all integer `icmp` predicates, `select`, `phi`, and acyclic CFG support.
2. Lower reversible predicates, muxes, and nested if/else diamonds while
   preserving clean inputs and workspaces.
3. Add constant shifts/rotates as wiring and constant-bound loop unrolling.
4. Reject cycles that remain after the frontend unroll pass and reject every
   unsupported construct with a source-aware diagnostic.

### v0.3 — arithmetic growth and quantum host integration

**Status:** Complete and tagged `v0.3.0`.

1. Add fixed-width multiplication and unsigned constant-divisor restoring
   division/remainder, rejecting zero, runtime, and signed divisors.
2. Add controlled arithmetic, MCX intermediate gates, and deterministic clean-
   ancilla decomposition to primitive gates before QASM emission.
3. Complete OpenQASM 3 named-register output, logical-to-register mapping, and
   internal validation.
4. Add the required Grover oracle integration path, an optional Qiskit host
   demonstration, and statevector/regression tests.
5. Record logical counts, post-decomposition X/CX/CCX counts, clean-ancilla
   usage, depth, and the documented 7-T-per-CCX estimate for each measured
   acceptance example.

### v0.4 — optimization contract and measured resource reporting

**Status:** Contract specified; implementation and reduction evidence are not
verified in this checkout. The documentation defines the release behavior but
does not claim a performance result.

1. Implement optimizer levels O0, O1, and O2. O0 retains the v0.3 Bennett
   baseline; O1 adds safe SSA/peephole simplification; O2 adds proven
   lifetime-based eager uncomputation and clean-ancilla reuse.
2. Require every optional pass to preserve fixed-width outputs, input/output
   layout, and clean workspaces. If a proof obligation is unknown or fails,
   retain the last verified lower-level schedule and report the fallback.
3. Capture actual before/after resource snapshots at the same post-decomposition
   primitive layer, including qubits, peak ancillas, gate counts, depth, and
   the declared T estimate. A higher level may equal its fallback; no reduction
   is release evidence until measured.
4. Run the complete quality gate and the v0.4 acceptance tests. The benchmark
   artifact `docs/benchmarks.md` is owned by the benchmark work and is not
   created or edited by this documentation change.

### v0.5 limitations and stretch

These are deliberately deferred topics, not v0.4 support:

- **Arrays and pointers:** require a bounded reversible memory model, explicit
  alias analysis, and load/store cleanup rules before they can be accepted.
- **Pebbling:** global space/time pebbling may improve on local lifetime reuse,
  but needs a separate schedule and cost model.
- **ESOP:** exclusive-sum-of-products synthesis is a possible Boolean-network
  strategy for later lowering, not a current optimizer fallback.

A v0.5 release may explore these areas only with new semantics, proof
obligations, diagnostics, and measurements. The v0.4 scalar contract remains
the compatibility boundary until then.

## Commit and release rhythm

Use small Conventional Commits. Land the bootstrap and each major subsystem
independently, run the complete quality gate before each milestone tag, and
create `v0.1.0`, `v0.2.0`, `v0.3.0`, and `v0.4.0` only after their acceptance
criteria pass. The v0.4 tag additionally requires the optimizer verification
results and benchmark-owned before/after evidence; documentation alone does not
constitute a reduction claim.

## Initial risks and resolution strategy

- LLVM textual syntax varies by version: parse attributes and metadata
  permissively, but validate opcodes and types strictly.
- `-fno-discard-value-names` is unavailable on old Clang: support numeric SSA
  identifiers and probe the flag rather than relying on a version string.
- Optimized C may replace source operators with equivalent LLVM operations:
  recognize semantic patterns such as NOT as XOR with an all-ones constant.
- A loop that survives full unrolling is never silently compiled: emit an
  explicit cyclic-CFG diagnostic.
- Native C with undefined behavior is excluded from differential tests; qcc's
  documented fixed-width behavior is tested only against defined native cases.
- An optimization pass may fail to prove a lifetime, commutation, or cleanup
  fact: keep the last verified schedule and report the fallback instead of
  weakening semantics.
