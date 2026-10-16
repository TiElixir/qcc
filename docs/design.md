# qcc design

## Scope

qcc compiles a deliberately bounded, pure-C subset into reversible circuits.
The first release optimizes for inspectable semantics and useful diagnostics,
not source-language coverage. Clang is used as the C frontend, while the
project owns the parser for the small textual LLVM IR subset it accepts.

The intended pipeline is:

```text
C source
  -> Clang textual LLVM IR
  -> validated qcc IR
  -> reversible lowering and ancilla scheduling
  -> X/CX/CCX circuit
  -> simulation, resource report, or OpenQASM 3
```

Each arrow is a validation boundary. Unsupported syntax, types, opcodes, and
control flow should produce an explicit diagnostic rather than being silently
approximated.

## Frontend boundary

qcc invokes Clang with optimization settings chosen for stable, non-vectorized
textual IR. qcc does not attempt to duplicate a C parser or rely on Clang's
internal Python bindings. The LLVM parser accepts harmless compiler-version
variation in attributes, metadata, and SSA names, but validates operation
codes, integer widths, and operand types strictly.

The source contract is intentionally small: fixed-width integer expressions in
single-entry, straight-line functions for v0.1. Later milestones may add
predication and bounded loops only when the resulting control-flow graph can
be represented and diagnosed unambiguously.

## Values and widths

A qcc integer has an explicit bit width. Arithmetic is modulo `2**width`, and
casts change width according to the documented `zext` and `trunc` rules. The
compiler never depends on C's implementation-defined signed overflow or on
undefined behavior. Inputs used for differential testing must therefore be
valid and defined under the C program being compared.

The v0.1 lowering target is an immutable-friendly circuit/netlist made from
X, controlled-X (CX), and doubly controlled-X (CCX). Wires are named by
logical value and layout metadata rather than by accidental parser order.

## Reversibility and cleanup

Computations are built out of clean ancillas. A temporary is computed, copied
to its destination when needed, and then uncomputed using the inverse schedule.
This is Bennett compute-copy-uncompute. The invariant is that every temporary
returns to its initial value at the end of a compiled function; only declared
outputs may differ from their initial state.

This baseline may use more gates or ancillas than an optimized implementation,
but it supplies a simple correctness reference. Later optimizer work may
reduce gate count or reuse clean ancillas only if it preserves the same
observable mapping and cleanup invariant.

## Outputs and validation

The circuit layer provides three consumers:

1. A classical simulator for exhaustive small-domain and differential checks.
2. Resource reporting for width, wire, ancilla, and gate counts.
3. An OpenQASM 3 emitter with named registers and internal consistency checks.

Qiskit is an optional integration for host-side experiments. The core runtime
remains standard-library-only so circuit construction and simulation do not
require a quantum SDK.

## Diagnostics and compatibility

Errors should identify the source function and the unsupported IR construct
when that information is available. Clang feature probing is preferred to
version-string assumptions, including probing support for
`-fno-discard-value-names`. Numeric SSA identifiers and compiler-generated
operation equivalents must remain valid inputs when their semantics are in the
supported subset.

The complete milestone scope and acceptance commands are maintained in
[plan.md](plan.md). The exact source subset is listed in
[supported_c.md](supported_c.md).
