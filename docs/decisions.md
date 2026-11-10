# Architecture decisions

## D-004 — integrate the five v0.1 subsystem implementations directly

- **Decision:** Keep the independently implemented frontend, netlist, lowering,
  and backend APIs as small adapters around one canonical `qcc.ir.netlist`
  representation, resolving name collisions in the public package rather than
  duplicating circuit implementations.
- **Alternatives:** Rewrite every subsystem into one monolithic implementation;
  retain the lowering branch's fallback-only circuit model.
- **Reason:** The canonical netlist already validates domains, layouts, gate
  operands, inversion, and resources. Adapters preserve those checks and keep
  the pipeline testable without making the frontend depend on backend details.

## D-005 — Clang textual LLVM is the v0.1 compiler boundary

- **Decision:** `qcc.compile_c` invokes the Clang wrapper, parses its textual
  LLVM result, and then selects and lowers the requested function.
- **Alternatives:** Parse C directly; call Clang through an LLVM Python binding;
  make callers provide LLVM IR.
- **Reason:** This satisfies the source-level API without introducing a C parser
  or a runtime LLVM dependency, while keeping the supported LLVM subset small
  and explicitly diagnosable.

This record captures the decisions that define the bootstrap and v0.1
boundary. The implementation plan in [plan.md](plan.md) is the source for the
milestones referenced below.

## D-001: Use a `src/` layout and Hatchling

**Status:** accepted

The distribution is named `qcc`, uses `src/qcc`, and is built with Hatchling.
The `src/` layout prevents an unchecked working-tree import from masking
packaging errors. Hatchling keeps the build backend small and declarative.
Python 3.10 is the minimum supported runtime, with CI covering 3.10 through
3.12.

## D-002: Keep the core runtime standard-library-only

**Status:** accepted

The compiler, circuit model, simulator, and emitter have no required runtime
dependencies beyond Python's standard library. Pytest, Hypothesis, Ruff, and
mypy are development extras. Qiskit is an explicitly optional extra for host
integration and examples, not a dependency of compilation.

## D-003: Let Clang front C and own the accepted LLVM parser

**Status:** accepted

qcc invokes Clang rather than maintaining a C parser or depending on private
Clang bindings. It owns a small textual LLVM IR parser so the accepted subset,
version-tolerant syntax, and diagnostics remain under project control. Parser
permissiveness applies to harmless attributes and naming variation only;
unsupported opcodes and types remain errors. This follows the repository plan's
frontend boundary and LLVM-version risk strategy.

## D-004: Start with a narrow reversible baseline

**Status:** accepted

v0.1 supports straight-line fixed-width integer functions and lowers to
X/CX/CCX gates. Bennett compute-copy-uncompute is the baseline cleanup
strategy. It makes temporary cleanup and correctness easy to inspect before
later optimizer work trades resources for fewer gates. Comparisons and
predication wait for v0.2 rather than expanding the first release implicitly.

## D-005: Make fixed-width behavior explicit

**Status:** accepted

Integer widths are part of the IR and arithmetic is modulo `2**width`. qcc
rejects or documents constructs whose C behavior depends on undefined or
implementation-defined behavior. Differential tests compare only defined
native-C inputs. This prevents a reversible circuit from appearing correct
because a host compiler happened to choose one undefined result.

## D-006: Make quality checks executable in CI

**Status:** accepted

Every supported Python version installs Clang and runs the project gate:
`ruff check .`, `ruff format --check .`, `mypy --strict src`, and `pytest -q`.
The configuration lives in `pyproject.toml`, and CI installs the same `.[dev]`
extra used by contributors. This keeps formatting, lint, types, and behavior
checks aligned.

## D-007: Keep bootstrap ownership boundaries explicit

**Status:** accepted

This bootstrap change adds package metadata and a `py.typed` marker but does
not create the public `src/qcc/__init__.py`. The lowering/integration owner
will add that public package surface along with the implementation modules.
The frontend, circuit, lowering, CLI, and tests are intentionally separate
work items so their commits can be reviewed and merged without a placeholder
API becoming a hidden contract.

## D-008 — define phi values by incoming CFG edge

**Status:** accepted

A `phi` selects its incoming value from the concrete predecessor edge taken at
block entry. Edge identity is preserved during CFG normalization, and missing,
extra, or mismatched incoming edges are diagnostics rather than inferred
values.

## D-009 — reject cycles left after constant-bound unrolling

**Status:** accepted

A loop is accepted only after the frontend proves a constant bound and fully
unrolls it. Any backedge or cycle remaining in the normalized reachable CFG is
rejected; qcc never substitutes a runtime loop or an implicit iteration cap.

## D-010 — interpret signed comparisons as two's-complement predicates

**Status:** accepted

Signed `icmp` predicates interpret each fixed-width operand as an `n`-bit
two's-complement value. This signed comparison rule does not make
signed-overflow arithmetic defined or expand support for general signed
arithmetic.

## D-011 — reject zero divisors

**Status:** accepted

Constant `udiv` and `urem` require a compile-time nonzero divisor. A zero
divisor is rejected before lowering because C defines division by zero as
undefined; qcc does not assign an arbitrary quotient, remainder, or reversible
mapping to it.

## D-012 — decompose MCX with a clean-ancilla Toffoli ladder

**Status:** accepted

MCX is an intermediate logical operation only. Before primitive resource
reporting or OpenQASM 3 emission, qcc uses the deterministic clean-ancilla
ladder: direct `X`/`CX`/`CCX` for zero, one, and two controls, and `k - 2`
clean ancillas plus `2k - 3` `CCX` gates for `k >= 3`. The ladder is reversed
to restore every ancilla. Ancilla-free, dirty-ancilla, and relative-phase
decompositions are not the v0.3 baseline.

## D-013 — keep Qiskit optional

**Status:** accepted

The compiler core, classical simulator, resource reports, OpenQASM 3 emitter,
and validator remain standard-library-only. Qiskit is an optional extra for
host-side integration and demonstrations; installing it is never required for
the qcc quickstart or for the compiler's correctness checks.

## D-014 — define v0.4 optimization levels as semantics-preserving schedules

**Status:** accepted for the v0.4 release contract

The public optimizer levels are O0, O1, and O2. O0 is the deterministic v0.3
Bennett baseline. O1 adds defined SSA simplification and local reversible-gate
peepholes. O2 adds path-aware lifetime-based eager uncomputation and clean
ancilla reuse when its proof obligations are discharged. A level is an allowed
pass set, not a promise of fewer gates, qubits, depth, or T gates; O2 may produce
the O1 or O0 circuit. Levels above O2 have no v0.4 semantics.

The qcc optimization contract applies after normalized SSA validation. It is
separate from any Clang optimization used to obtain stable textual LLVM, and it
does not widen the supported source subset or assign meaning to undefined
behavior.

## D-015 — require proof obligations for eager cleanup and ancilla reuse

**Status:** accepted for the v0.4 release contract

A temporary may be eagerly uncomputed only after its last use on every reachable
path and by inverting the exact verified compute schedule. An ancilla may be
reused only when it is clean at acquisition, exclusive of all live values and
pending inverses, restored on every path, disjoint from inputs/outputs and gate
operands, and covered by observable-equivalence and clean-workspace evidence.
Unknown aliasing, edge identity, path cleanup, phase, or layout facts retain the
baseline schedule and fresh workspace.

## D-016 — use conservative optimizer fallback and same-layer before/after reports

**Status:** accepted for the v0.4 release contract

If an optional pass cannot prove its preconditions or its candidate fails
verification, qcc keeps the last verified O1/O0 schedule and reports the
requested level, effective level, and fallback reason. Source-level unsupported
constructs remain hard errors. A v0.4 resource report captures actual before and
after snapshots after the same MCX-to-X/CX/CCX decomposition, including qubits,
peak clean ancillas, gate counts, depth, and the declared T estimate. Resource
reductions are release claims only when those paired measurements exist.

## D-017 — defer arrays, pointers, pebbling, and ESOP to v0.5 exploration

**Status:** accepted as a scope boundary

v0.4 remains a scalar, acyclic, pure-C contract. Bounded arrays and pointers
require an explicit reversible memory and aliasing model; global pebbling is a
future space/time scheduling problem beyond local lifetime reuse; and ESOP
synthesis is a future Boolean-network strategy. These are v0.5 limitations or
stretch topics, not implicit v0.4 support.

## D-018 — legalize calls, intrinsics, and variable shifts in the frontend

**Status:** accepted for v0.5

Current Clang emits `llvm.umin`, `llvm.fshl`, `llvm.abs`, and similar
intrinsic calls for C that the v0.4 parser expected as `icmp`/`select`, so
rejecting every `call` rejected programs inside the documented subset. qcc now
parses calls and removes them before lowering: intrinsics expand to primitive
operations, and calls to functions defined in the same file are inlined. No
backend learns a new opcode, so both lowering paths and the verifier get the
feature at once. Inlining is complete, not heuristic, because the subset has
no recursion; a call-graph cycle is a diagnostic. External calls stay
rejected because their bodies, and therefore their side effects, are unknown.

## D-019 — reject per function, not per file

**Status:** accepted for v0.5

A module is validated function by function. A function that fails is stored
with its diagnostic and raises it when requested; a function that calls it
inherits that diagnostic. The alternative, rejecting the file, made a single
`printf` helper block every unrelated function beside it. Strict whole-module
parsing remains available as `parse_module(..., strict=True)`.

## D-020 — make level 3 a separate synthesizer, not more passes on level 2

**Status:** accepted for v0.5

The level 0–2 lowering allocates a register per SSA value and a predicate per
block. Its costs are structural: no peephole recovers the n-1 scratch wires
of an out-of-place adder or the path-condition logic of a branch. Level 3
therefore builds circuits from a dataflow graph with a different value
representation (bit references) and in-place arithmetic. The older levels
are kept unchanged as the stable baseline that reports compare against, and
because they still accept the structural SSA dictionary input.

If-conversion is the default treatment of branches at level 3. It evaluates
both arms, which is free of semantic risk here (no traps, no side effects)
and avoids synthesizing path predicates. It can cost more than predicated
execution when one arm is very expensive and rarely taken; the benchmarks do
not show that case, and it is noted as a known trade-off rather than solved.

## D-021 — verify exhaustively, and say when the check was sampled

**Status:** accepted for v0.5

Correctness claims are backed by sweeping every input when the arguments
total at most 20 bits. Bit-sliced simulation makes this cheap enough to run
on every test and every fuzzed function. Above the limit the verifier
samples with a fixed seed and its result is marked non-exhaustive, so a
sampled pass is never presented as a proof. The verifier checks three
properties, not one: output value, input preservation, and clean work wires.
The last two are what make a circuit usable as a quantum oracle and are the
ones a value-only test misses.

## D-022 — keep the default optimization levels unchanged in v0.5

**Status:** accepted for v0.5; expected to be revisited

`compile_c` still defaults to `opt=1` and the CLI to `--opt O0`, so existing
callers and recorded resource figures do not change underneath them. `qcc
verify` defaults to O3 because it has no prior behavior to preserve. Making
O3 the general default is a deliberate, separate change.
