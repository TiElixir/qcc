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
  -> normalized and validated qcc IR
  -> reversible lowering and ancilla scheduling
  -> X/CX/CCX circuit
  -> simulation, resource report, or OpenQASM 3
```

Each arrow is a validation boundary. Unsupported syntax, types, opcodes, and
control flow should produce an explicit diagnostic rather than being silently
approximated. The v0.1 baseline is straight-line integer code. The v0.2
sections below specify the control-flow and predicate extension; they are a
semantic contract, not evidence that every v0.2 operation is already present
in a particular checkout.

## Frontend boundary

qcc invokes Clang with optimization settings chosen for stable, non-vectorized
textual IR. qcc does not attempt to duplicate a C parser or rely on Clang's
internal Python bindings. The LLVM parser accepts harmless compiler-version
variation in attributes, metadata, and SSA names, but validates operation
codes, integer widths, and operand types strictly.

The v0.1 source contract is fixed-width integer expressions in single-entry,
straight-line functions. For v0.2, the frontend may normalize supported
conditionals and constant-bound loops into a finite control-flow graph (CFG).
The validator then checks the normalized CFG and does not infer support from
source syntax that disappeared during Clang lowering.

## Values and widths

A qcc integer has an explicit bit width. Arithmetic is modulo `2**width`, and
casts change width according to the documented `zext`, `sext`, and `trunc`
rules. The compiler never depends on C's implementation-defined signed
overflow or on undefined behavior. Inputs used for differential testing must
therefore be valid and defined under the C program being compared.

The v0.1 lowering target is an immutable-friendly circuit/netlist made from
X, controlled-X (CX), and doubly controlled-X (CCX). Wires are named by
logical value and layout metadata rather than by accidental parser order.
The v0.2 lowering keeps that primitive output and represents conditions with
clean predicate wires instead of measurements or host-language branches.

## v0.2 CFG and SSA semantics

### CFG normalization

A normalized function has one entry block, named successor edges, and integer
value-producing instructions. A terminator creates the edges; ordinary
instructions do not. Reachability is computed from the entry block, and
unreachable blocks are rejected or removed before lowering rather than being
silently assigned a meaning.

The supported conditional shape is an acyclic collection of unconditional and
conditional branch edges, including nested if/else diamonds. A branch
condition is an `i1` predicate. The lowering treats the branch as a coherent
control condition: it does not measure the predicate and choose one host-side
path. Each reachable edge receives a path predicate, and the predicates for
a set of outgoing edges are mutually exclusive and exhaustive.

### Phi and select

A `phi` is evaluated at the entry of its containing block. Its incoming value
is selected by the concrete CFG edge that reached the block, not by inspecting
the value of a data wire or by re-evaluating a source-level condition. The
normalization pass gives every terminator edge a stable identity; incoming
`(value, edge)` pairs are not collapsed merely because two edges have the same
predecessor block label. Every reachable incoming edge must have exactly one
well-typed incoming value.

A `phi` result with incoming values `v_e` and one-hot edge predicates `p_e` is
lowered bitwise as:

```text
phi = XOR over edges e of (p_e AND v_e)
```

The destination starts clean, each edge predicate controls a reversible copy,
and the edge/path-predicate computation is later uncomputed. This makes the
merge deterministic while preserving all incoming values and the input
condition wires. A malformed phi, a missing incoming edge, or a type/width
mismatch is a hard diagnostic.

`select c, t, f` is an expression rather than a CFG edge. Its condition must
be `i1`, and its two arms must have the same integer type. The result is the
bitwise mux:

```text
select(c, t, f) = f XOR (c AND (t XOR f))
```

For a fresh, clean result bit, the baseline direct lowering emits `CX(f, out)`
followed by `CCX(c, t, out)` and `CCX(c, f, out)`. With `c = 0` the result is
`f`; with `c = 1`, the two controlled toggles change it to `t`. A multi-edge
phi uses the same construction with one control per incoming edge.

### Reversible predicates and conditional blocks

Every supported `icmp` produces a clean one-bit predicate without modifying
its operands. Equality can be formed from bitwise differences and a reversible
zero test; ordered predicates use a ripple comparison of the fixed-width bit
patterns. `eq`/`ne`, all four unsigned ordered predicates, and all four signed
ordered predicates are distinct semantics, not aliases chosen from host
language truthiness.

A signed predicate interprets an `n`-bit pattern as two's-complement:

```text
signed(x) = x                 when x < 2**(n - 1)
            x - 2**n          otherwise
```

For an if/else region, qcc computes the predicate, evaluates branch-local
reversible work under the appropriate path control, merges results with
`select`/`phi` muxes, copies any observable result, and reverses temporary
work. Nested regions use conjunctions of already-clean path predicates. The
predicate and all path workspaces must return to their initial values after
the final output copy. No branch is treated as an irreversible discard of the
unselected value.

## Constant shifts and wiring

A shift or rotate is a wiring operation only when its amount is a compile-time
constant. With little-endian bit indices `0..n-1`, the result mappings are:

```text
shl  y[i] = x[i-k]       when i >= k, otherwise 0
lshr y[i] = x[i+k]       when i+k < n, otherwise 0
ashr y[i] = x[i+k]       when i+k < n, otherwise x[n-1]
rotl y[i] = x[(i-k) mod n]
rotr y[i] = x[(i+k) mod n]
```

The fixed mapping is represented in the logical layout rather than by a
variable-shift network, carry circuit, or data-dependent controls. If a
backend requires a distinct clean destination register, forwarded bits are
copied with CX and fill bits use their initialized zero or sign value; those
copies are reported as wiring/copy cost. A rotate count is reduced modulo the
width. A non-constant amount, or a shift count outside `0 <= k < n`, is
rejected because it requires unsupported dynamic control or has undefined/
poison semantics at the C/LLVM boundary.

Constant wiring does not make signed C right shift portable: the source
contract accepts only the explicitly documented fixed-width semantics, while
`ashr` is the two's-complement sign-fill operation in normalized IR.

## Loop-unroll policy

The frontend may accept a source loop only when it can prove a finite,
compile-time iteration bound and fully unroll the loop before qcc lowers the
CFG. Zero-iteration and early-exit cases may remain as acyclic conditional
paths, but no runtime loop control is emitted. A partial unroll followed by a
runtime backedge is not a supported fallback.

After unrolling, the validator walks every reachable edge and rejects any
backedge, self-loop, or strongly connected component that contains more than
one block. A loop that survives the frontend unroll pass is therefore a hard,
source-aware cyclic-CFG diagnostic, even if a condition appears likely to
terminate. The compiler must never silently choose an iteration limit.

## Cost model for predicates and muxes

The baseline resource model counts primitive X, CX, and CCX gates. Let `P(n)`
mean the one-way primitive cost of computing an `n`-bit predicate. A clean
predicate that must be restored after use costs the forward computation and its
inverse, or approximately `2P(n)`. The comparison rows intentionally specify
linear work and clean workspace rather than an exact gate schedule; optimizer
or backend changes may alter constants without changing semantics.

| Operation | Observable result | Baseline primitive work | Clean workspace and notes |
| --- | --- | --- | --- |
| `icmp eq` / `icmp ne` on `n` bits | One predicate bit | `O(n)` CX/CCX; `ne` adds predicate inversion | A reversible zero-test/reduction workspace; `ne` is the inverse of equality |
| Unsigned ordered `icmp` | One predicate bit | `O(n)` CX/CCX with a ripple carry/borrow comparison | Linear comparator workspace; `ugt`/`uge`/`ult`/`ule` are operand/order variants |
| Signed ordered `icmp` | One predicate bit | `O(n)` CX/CCX with sign handling and the same linear comparator | Linear comparator workspace; sign bits decide differing-sign cases |
| Two-arm `select` with a clean condition | `n`-bit result | `n` CX + `2n` CCX for the direct clean-target mux | No temporary delta register; the target starts at zero |
| `m`-way `phi` with clean edge predicates | `n`-bit result | Up to `mn` controlled copies, normally `mn` CCX | Edge predicates are one-hot; statically known edges can use CX |
| Mux with a computed predicate that is restored | `n`-bit result | `2P(n) + n` CX + `2n` CCX | Includes predicate compute/uncompute; branch-local work is additional |

These figures are planning costs, not claims about completed implementation
or test output. Resource reports must count any actual copy, fill, predicate,
and cleanup gates emitted by the selected lowering schedule.

## Reversibility and cleanup

Computations are built out of clean ancillas. A temporary is computed, copied
to its destination when needed, and then uncomputed using the inverse schedule.
This is Bennett compute-copy-uncompute. The invariant is that every temporary,
predicate, path flag, and comparator workspace returns to its initial value at
the end of a compiled function; only declared outputs may differ from their
initial state.

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
when that information is available. For v0.2, diagnostics must also name the
block or edge involved in a malformed phi, identify a residual CFG cycle, and
state when a predicate, shift amount, or mux operand violates the contract.
Clang feature probing is preferred to version-string assumptions, including
probing support for `-fno-discard-value-names`. Numeric SSA identifiers and
compiler-generated operation equivalents must remain valid inputs when their
semantics are in the supported subset.

The complete milestone scope and acceptance commands are maintained in
[plan.md](plan.md). The authoritative source subset and diagnostic policy are
listed in [supported_c.md](supported_c.md).
