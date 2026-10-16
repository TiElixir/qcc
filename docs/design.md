# qcc design

## Scope

qcc compiles a deliberately bounded, pure-C subset into reversible circuits.
The first releases optimize for inspectable semantics and useful diagnostics,
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
approximated. The v0.1 baseline is straight-line integer code and v0.2 adds
predicates, reversible control flow, and constant wiring. The v0.3 sections below
specify arithmetic growth, MCX lowering, named QASM registers, and resource
 conventions. The v0.3 arithmetic, QASM, and resource implementations are
integrated and covered by the release test suite. The v0.4 section specifies
semantics-preserving optimization and release reporting; it is a contract, not
a claim that a resource reduction has already been measured.

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

## v0.3 arithmetic

The v0.3 arithmetic operations use explicit fixed-width bit-vector semantics.
For an `n`-bit value, an unsigned result is reduced modulo `2**n`; operands
remain available to the caller unless an operation is explicitly documented
as an in-place target update. A pure function returning an arithmetic result
uses a clean output register, and all work registers are restored before the
function finishes.

### Multiplication

`mul` is fixed-width modular multiplication. For same-width operands `a` and
`b`, the observable result is:

```text
mul_n(a, b) = (a * b) mod 2**n
```

The baseline reversible schedule preserves both inputs, forms controlled
partial products with the multiplier bits, and accumulates them with modular
adders. Partial products that fall outside the result width are discarded as
part of the modulo operation; they are not silently saturated. The reference
implementation may use a clean `n`-bit accumulator plus carry/work bits, with
the accumulator copied to the output and all partial-product work uncomputed.
This is an `O(n**2)` baseline in controlled-adder work. Exact X/CX/CCX counts
depend on the chosen adder schedule and must be reported from the emitted
circuit when available.

The public source example is unsigned fixed-width multiplication. A signed
source operation is accepted only if its normalized IR semantics are in the
documented defined subset; signed overflow is never given a reversible meaning
by qcc.

### Constant restoring division and remainder

The supported division family has an unsigned, compile-time nonzero divisor.
For `n`-bit `x` and a constant `d` with `1 <= d < 2**n`, the results are:

```text
udiv_n(x, d) = floor(x / d)
urem_n(x, d) = x mod d
```

The divisor is a classical constant, not a runtime quantum register. A
restoring schedule scans the dividend from the most significant bit to the
least significant bit. Each iteration shifts the partial remainder, compares
it with `d`, conditionally subtracts `d`, and records one quotient bit. The
compare/subtract decision is then uncomputed as part of the cleanup schedule.
The quotient and remainder can share the same restoring work; a public
single-result operation copies either selected result to its clean output.

The reversible mapping for a result register is conceptually:

```text
|x>|0_result>|0_work> -> |x>|udiv_n(x, d)>|0_work>
|x>|0_result>|0_work> -> |x>|urem_n(x, d)>|0_work>
```

The baseline uses `n` restoring iterations, each with linear-width comparison
and subtraction work, so its planning cost is `O(n**2)` primitive work. It
does not claim a fixed gate count: carry direction, comparator choice, output
copy, and uncompute order are part of the selected lowering schedule. A
runtime divisor, a zero divisor, and signed division/remainder are outside
this contract. Division by zero is rejected before lowering because the C
source operation is undefined; qcc does not invent quotient or remainder bits.

## v0.4 optimization contract

v0.4 optimizes only a validated normalized SSA function and its reversible
lowering. It does not widen the supported C subset, assign meaning to
undefined behavior, or permit a pass to replace an unsupported operation with an
approximation. Every optimization must preserve the function's fixed-width
mapping, named input/output layout, and clean-workspace invariant for every
defined input.

This section specifies what an eventual v0.4 implementation and release report
must guarantee. It is not a performance result: an optimizer level may emit the
same circuit as its fallback, and no reduction is claimed without measured
before/after resource snapshots.

### Optimization boundary and SSA passes

The optimizer starts after Clang output has been parsed, normalized, and
validated against the supported subset. It may use typed SSA values, constant
values, CFG edge identity, use counts, path predicates, and proven gate
dependencies. It must not depend on source spelling, unstable SSA names, a
particular Clang version, or undefined/poison behavior.

The v0.4 SSA pass set is:

- **Constant folding:** evaluate pure integer operations, casts, predicates,
  and constant `select`/`phi` choices when widths, modulo behavior, and
  definedness are known. A zero divisor, invalid shift, or other undefined
  case remains a diagnostic; folding never creates a reversible meaning for it.
- **Copy and identity propagation:** forward equivalent SSA values and remove
  fixed-width identities such as a copy of a value or an operation with a
  proven neutral operand, while preserving edge-specific `phi` semantics.
- **Dead-value elimination:** remove an SSA definition only when it has no
  observable result, no live consumer, and no role in a required compute or
  inverse-cleanup schedule. An apparently dead value that owns workspace or
  feeds an output copy is live for this rule.
- **Predicate and mux simplification:** simplify a condition only when the
  condition is known or the transformation is algebraically reversible. The
  pass must preserve all reachable CFG edges and the incoming-edge meaning of
  `phi` values.

SSA changes are applied before optional circuit peepholes. The optimizer keeps a
mapping from each surviving value to its source definition and cleanup schedule
so that eliminating a name cannot accidentally eliminate the work needed to
restore an ancilla.

### Gate-level peepholes

After lowering, qcc may apply local rewrites to logical or primitive reversible
gates. The allowed v0.4 peephole class includes:

- canceling adjacent identical self-inverse gates (`X`, `CX`, `CCX`, or a
  logical `MCX`) and an explicitly paired compute/inverse schedule;
- removing proven identity copies and zero-length wiring operations;
- commuting gates only when their qubit dependencies are disjoint or a
  separately proven commutation rule applies, then exposing a valid
  cancellation; and
- applying the same rules after MCX decomposition, with the primitive resource
  report taken only after the final `X`/`CX`/`CCX` sequence is fixed.

A peephole may not cross an output copy, a measurement boundary if a future
backend adds one, a live control dependency, or a cleanup obligation merely to
make two gates adjacent. Relative-phase substitutions, dirty-ancilla tricks,
and hardware-specific identities are outside this v0.4 contract unless their
phase and workspace behavior are separately specified and verified.

### Lifetime-based eager uncomputation

The baseline retains temporary work until the enclosing compute-copy-uncompute
schedule finishes. O2 may release a temporary earlier. Lifetime analysis marks
the definition, every use on every reachable CFG edge, the output-copy point,
and the inverse schedule that restores the temporary. When the last use has
completed, qcc may immediately apply the inverse of the exact compute schedule
and return its workspace to the clean pool.

Eager uncomputation is path-aware. A value used in one branch is not released
before the other reachable branch and its merge/`phi` handling have completed;
a path predicate remains live until all controlled work and its own cleanup are
done. The inverse may run early only when all of its inputs remain available,
no live gate depends on the temporary, and the inverse cannot alter an input,
output, or another live value. If those facts cannot be proven, the baseline
late cleanup is retained.

### Ancilla reuse proof obligations

Reusing a clean ancilla is a verified allocation decision, not a best-effort
packing heuristic. Every reuse must discharge all of these obligations:

1. **Clean acquisition:** the wire is zero on every incoming path at the point
   of allocation, including paths through conditional cleanup.
2. **Exclusive liveness:** no live SSA value, control, target, path predicate,
   pending inverse, input, or output aliases the wire during its new lifetime.
3. **Complete release:** the previous owner has been uncomputed by its inverse
   schedule on every reachable path, with no residual phase or classical
   garbage.
4. **Stable layout:** the physical wire remains in the declared circuit domain;
   reuse does not overlap gate operands or change the logical-to-OpenQASM
   register mapping.
5. **Equivalent behavior:** the optimized circuit has the same observable
   output mapping as the baseline for every defined input and leaves all
   declared workspaces clean. The proof/evidence must cover branch, `phi`,
   early-return, and boundary cases relevant to the schedule.

A failed or undecidable obligation is a reason to allocate a fresh clean
ancilla or keep the prior schedule. It is never permission to reuse a dirty
wire or to silently drop cleanup. Peak live ancilla usage and total allocated
qubits must both be recomputed after reuse.

### Optimizer levels

The public v0.4 contract defines three levels. A level selects an allowed pass
set; it is not a promise that a higher level will reduce any resource.

| Level | Required behavior | Cleanup and fallback |
| --- | --- | --- |
| `O0` | Validated normalization and the deterministic v0.3 Bennett baseline; no optional SSA, peephole, eager-uncompute, or reuse pass | Fresh clean workspace and the baseline cleanup schedule are retained |
| `O1` | `O0` plus the specified safe SSA simplifications and local gate peepholes | No lifetime-based reuse is required; an unprovable local rewrite is skipped |
| `O2` | `O1` plus lifetime-based eager uncomputation and clean-ancilla reuse when every proof obligation is discharged | Each failed proof falls back to the last verified `O1`/`O0` schedule and is reported |

The allowed pass set is monotonic, but resource counts need not improve at every
level. O2 is allowed to equal O1 or O0. Levels above O2 have no v0.4 semantics
and must not be silently treated as an alias for O2 by a release
implementation.

### Before/after resource reporting

An optimized compilation must capture an actual **before** snapshot after the
deterministic baseline has been lowered and all logical MCX operations have been
expanded to the primitive `X`/`CX`/`CCX` layer. It then captures an **after**
snapshot from the selected optimized or fallback circuit at that same layer.
The snapshots use the same input/output layout, width, decomposition convention,
and counting rules. Comparing a logical pre-decomposition count with a
primitive post-decomposition count is invalid.

Each snapshot reports at least:

- `qubit_count`: total physical qubits in the selected layout;
- `ancillas`: peak simultaneously live clean work qubits, with all released
  wires accounted for;
- `gate_count` and `gate_counts` for `x`, `cx`, and `ccx` (and `mcx` only in a
  separately labeled logical snapshot);
- `depth`, computed with the same dependency/layering rule; and
- `t_count`, using the declared Toffoli decomposition convention.

The enclosing report records `requested_level`, `effective_level`, and a
machine-readable fallback reason when a requested pass was not applied. It may
also provide derived deltas, but deltas must be calculated from the two emitted
snapshots rather than from an asymptotic estimate. Existing flat resource keys
remain the selected (`after`) view for compatibility. At `O0`, the before and
after snapshots are intentionally the same baseline. A release note may claim a
reduction only when the corresponding before/after values were actually
measured and verified.

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

## MCX decomposition policy

MCX is an intermediate logical gate, not an OpenQASM 3 primitive in the qcc
backend. Every multi-controlled X must be expanded to `X`, `CX`, and `CCX`
before QASM emission or before a primitive resource report is treated as
final. The baseline policy is deterministic and uses clean ancillas:

- Zero controls are `X`; one control is `CX`; two controls are `CCX`.
- For `k >= 3` controls, allocate `k - 2` clean ancillas. Compute the
  conjunction of the controls into a Toffoli ladder, apply one `CCX` to the
  final conjunction and target, and reverse the ladder.
- The clean-ancilla ladder therefore uses `2k - 3` `CCX` gates for `k >= 2`
  controls (with the `k = 2` case being the direct one-`CCX` base case) and
  returns every decomposition ancilla to zero.
- Ancilla-free, relative-phase, dirty-ancilla, and hardware-specific MCX
  decompositions are optimizer or backend policies outside this baseline.

The decomposition must preserve control and target wires, must not measure or
discard a control, and must be applied before the OpenQASM 3 emitter sees the
circuit. A resource report may expose both the logical `mcx` count and the
post-decomposition primitive count, but it must label which layer it reports.

## Reversibility and cleanup

Computations are built out of clean ancillas. A temporary is computed, copied
to its destination when needed, and then uncomputed using the inverse schedule.
This is Bennett compute-copy-uncompute. The invariant is that every temporary,
predicate, path flag, and comparator workspace returns to its initial value at
the end of a compiled function; only declared outputs may differ from their
initial state.

This baseline may use more gates or ancillas than a later optimized
implementation, but it supplies the O0 correctness reference. v0.4 may reduce
gate count or reuse clean ancillas only under the optimization proof
obligations above; the baseline remains available for fallback and for the
`before` resource snapshot.

## OpenQASM 3 register mapping

The OpenQASM 3 backend maps the logical circuit layout to named quantum
registers in declared layout order. Each register is emitted as
`qubit[n] name;`; register names must be valid OpenQASM identifiers and sizes
must be positive. Inputs, outputs, and clean ancillas remain distinct named
registers when the layout exposes them. The backend emits no measurements or
classical registers.

Logical bit `i` of a named `n`-bit register is emitted as `name[i]`. This is a
little-endian convention: bit zero is the least significant bit and is the
first element of the register's logical bit sequence. For a non-contiguous
physical layout, the layout's ordered bit list defines the mapping; physical
indices are not used as OpenQASM array indices by accident. Every gate operand
must resolve through that map and must be in range.

The emitted primitive subset is:

```qasm
OPENQASM 3.0;
include "stdgates.inc";
qubit[n] input;
qubit[m] output;
qubit[k] ancilla;
// x, cx, and ccx instructions follow
```

The emitter validates the header, include, declarations, gate arity, named
register references, and index ranges. MCX is an error at this boundary if it
has not already been decomposed. OpenQASM text is therefore a serialization
of a validated primitive circuit, not a place where unsupported operations are
silently approximated.

## Resource and T-count estimate conventions

Resource reports and planning estimates use separate, explicitly labelled
layers:

1. **Logical layer:** counts source-level arithmetic blocks and any `mcx`
   operations before decomposition.
2. **Primitive layer:** counts emitted `X`, `CX`, and `CCX` gates after MCX
   expansion. It includes output copies, predicate/quotient/remainder work,
   and all compute/uncompute gates. It excludes no cleanup merely because the
   cleanup is an inverse schedule.
3. **T estimate:** estimates the Clifford+T cost from the primitive layer.

The standard baseline convention is an exact Toffoli decomposition with
`T-count(CCX) = 7`, `T-count(X) = 0`, and `T-count(CX) = 0`; both `T` and
`T-dagger` count as one T gate. Under this convention, an MCX with `k >= 2`
controls contributes `7 * (2k - 3)` estimated T gates, while zero and one
control contribute zero T gates. A backend that uses a different exact or
relative-phase decomposition must report its own convention instead of
silently reusing this estimate.

For each resource snapshot, `qubit_count` includes inputs, outputs, and
allocated physical work qubits; `ancillas` is the peak number of simultaneously
live clean work qubits in that snapshot; `gate_count` is the number of
operations in the layer being reported; and `depth` is the sequential layer
count where gates sharing a qubit cannot be placed in the same layer. A T
estimate is not a measured hardware runtime or fidelity prediction; it is a
portable accounting estimate for the selected decomposition convention.

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
