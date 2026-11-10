# Supported C subset

This document is the authoritative source-level contract for qcc v0.5. It
covers the constructs that may be lowered after Clang emits normalized textual
LLVM IR. v0.5 widens the v0.4 subset with calls to functions defined in the
same file, the integer intrinsics Clang emits for ordinary C, and shifts by a
runtime amount. It does not make arrays, pointers, or other memory constructs
supported. A construct is supported only when its generated IR satisfies the
value, CFG, arithmetic, cleanup, and optimizer rules below.

## Supported in v0.5

### Function and value model

- A pure function with fixed-width scalar integer arguments and one fixed-width
  integer return value. `uint8_t` remains the primary public example; other
  explicit widths are accepted when the IR and resource limits support them.
- Fixed-width signed integer values for defined comparisons. Signed predicates
  interpret their operands as two's-complement bit patterns. General signed
  arithmetic that can overflow is not made defined by qcc.
- No observable side effects: the function may not read or write global state,
  perform I/O, access memory, or call a function that is not defined in the
  same file.
- Defined source behavior only. C undefined behavior, poison, and `undef`
  values are rejected rather than assigned reversible semantics.

### Integer expressions

These operations are covered when widths and operands are valid:

- Integer constants and function arguments.
- Addition and subtraction (`+`, `-`) with the documented fixed-width modulo
  behavior for accepted IR.
- Same-width fixed-width multiplication (`mul`). The result is reduced modulo
  `2**width`, and the reversible lowering preserves the operands while it
  computes the result into a clean output/work register.
- Bitwise AND, OR, XOR, and complement (`&`, `|`, `^`, `~`) including the
  canonical LLVM representation of complement.
- Zero extension, sign extension, and truncation (`zext`, `sext`, `trunc`) with
  the declared source and destination widths.
- Equality, inequality, and ordered comparisons. All LLVM integer `icmp`
  predicates are supported: `eq`, `ne`, `ugt`, `uge`, `ult`, `ule`, `sgt`,
  `sge`, `slt`, and `sle`.
- Conditional expressions and LLVM `select` when the condition is `i1` and
  both arms have the same integer type.
- Shifts. A compile-time count must satisfy `0 <= count < width` and is pure
  wiring. A runtime count becomes a barrel shifter with one mux stage per
  count bit. C leaves a shift by the operand width or more undefined, so only
  the count bits that can select a defined shift are examined; the circuit's
  value for an out-of-range count is unspecified. A signed right shift is
  accepted only when the frontend supplies the documented two's-complement
  `ashr` semantics.
- Rotates, as a recognized fixed-width idiom or the `llvm.fshl`/`llvm.fshr`
  funnel shifts. The count is reduced modulo the operand width. A runtime
  count requires a power-of-two width.
- Unsigned division and remainder by a compile-time nonzero constant. The
  normalized operations are `udiv` and `urem`, the divisor is represented at
  the operation width, and the restoring schedule computes `floor(x / d)` or
  `x % d` for `1 <= d < 2**width`.
- Returning a supported integer value.

### Function calls

- A direct call to a function defined in the same translation unit. Every
  call is inlined, so the compiled circuit is always one self-contained
  function; a callee with branches or early returns is spliced into the
  caller's CFG. `static`, `inline`, and `noinline` helpers are all accepted.
- A file is validated function by function. A function that uses an
  unsupported construct is rejected with its own diagnostic, as is every
  function that calls it; the remaining functions in the file still compile.

### Integer intrinsics

Clang rewrites common C idioms and builtins into LLVM intrinsics. qcc expands
these into the primitive operations above:

| Intrinsic | Typical C source |
| --- | --- |
| `llvm.umin`, `llvm.umax`, `llvm.smin`, `llvm.smax` | `a < b ? a : b` and friends |
| `llvm.abs` | `a < 0 ? -a : a` |
| `llvm.ctpop` | `__builtin_popcount` |
| `llvm.ctlz`, `llvm.cttz` | `__builtin_clz`, `__builtin_ctz` |
| `llvm.bswap`, `llvm.bitreverse` | `__builtin_bswap16/32/64`, bit reversal |
| `llvm.fshl`, `llvm.fshr` | rotate idioms |
| `llvm.uadd.sat`, `llvm.usub.sat` | saturating add/subtract idioms |

Value-free intrinsics (`llvm.lifetime.*`, `llvm.dbg.*`, `llvm.assume`) are
ignored. Any other intrinsic is rejected by name.

### Control flow and SSA

- A single entry block and a finite, reachable CFG made from unconditional and
  conditional branches and integer returns. Nested `if`/`else` regions are
  supported after normalization.
- `switch`. The parser rewrites it, before CFG validation, into a chain of
  `icmp eq` tests and two-way branches: one test per distinct case target
  (the OR of that target's case values), the last branch falling through to
  the default label. Clang emits a `switch` for a C `switch` statement and
  also for any run of `==` tests on one value, so an `if (a == 0) ... else if
  (a == 1) ... else if (a == 5) ...` chain, or the same written with `?:`,
  depends on this rule. When the cases cover every value of the selector,
  Clang points the default label at an `unreachable` block and the last case
  target takes its place as the fall-through. An `unreachable` that a branch
  can still reach, such as a `__builtin_unreachable()` that Clang cannot
  remove, is rejected: this function can reach code that has undefined
  behavior, and only functions that are defined for every input are
  supported.
- `phi` nodes at the beginning of a merge block. Each reachable predecessor
  edge has exactly one same-width incoming value, and the incoming value is
  selected by the edge that actually reached the block.
- The frontend may fully unroll a source loop with a compile-time iteration
  bound. The resulting CFG may retain conditional exits, but it must contain
  no backedge or cycle before lowering.
- A source conditional expression may lower to `select` instead of a branch;
  both forms use the same reversible predicate and mux semantics.

## Optimizer semantics

Optimization is a semantics-preserving schedule choice after source validation.
It does not change the C-to-LLVM acceptance boundary or give a rejected
construct a weaker interpretation. For every defined input, all supported
levels must preserve the return value, input registers, output register
mapping, and clean declared ancillas.

The release contract defines these levels:

- **O0** keeps the deterministic v0.3 Bennett compute-copy-uncompute baseline
  and does not apply optional SSA, gate, eager-uncomputation, or reuse passes.
- **O1** is permitted to constant-fold defined fixed-width SSA expressions,
  propagate copies and identities, eliminate dead values, and apply local
  reversible-gate cancellations. The current implementation applies none of
  these at O1 and emits the O0 circuit.
- **O2** may additionally uncompute a temporary eagerly at its last
  path-aware use, then reuse its clean workspace only when the lifetime and
  cleanup proof obligations in [design.md](design.md) are discharged.
- **O3** synthesizes the whole function from its dataflow graph instead of
  lowering instruction by instruction: wiring-only operations emit no gates,
  branches are if-converted to selects, arithmetic is computed in place, and
  the returned value is produced directly in the output register. See
  [design.md](design.md#v05-dataflow-synthesis).

Every level computes the result into an output register that starts at zero,
preserves the input registers, and returns work qubits to zero. O0, O1, and
O2 additionally XOR the result into the output register, so the circuit is
correct for any starting value of that register. O3 does the same for a
one-bit result, because its output qubit is only ever a gate target and never
a control; this is the bit-flip oracle form that Grover-style phase kickback
needs. For a wider result O3 builds the value in place in the output
register, so that register must start at zero.

The level selects a strategy, not a performance promise. O2 may produce the
same circuit as O0. Levels above O3 have no meaning and are rejected rather
than silently aliased to O3. O3 needs a typed function parsed from C; the
structural SSA dictionary accepted by `compile_c` for compatibility is
compiled at O2, and the resource report records 3 as the requested level and
2 as the effective one.

### Conservative optimizer fallback

The source validator remains strict: an unsupported opcode, pointer or array
operation, undefined divisor, residual cycle, malformed edge set, or other
source-level violation is a hard diagnostic at every optimizer level. Optional
optimization has a narrower fallback rule. If a pass cannot prove constant
validity, edge/lifetime safety, gate commutation, clean release, layout
non-overlap, or semantic equivalence, qcc discards that candidate and retains
the last verified schedule. O2 therefore falls back to a verified O1 or O0
schedule rather than emitting an approximate circuit or reusing an uncertain
wire.

A resource report records both the requested and effective level and a
machine-readable fallback reason when applicable. It also records before/after
snapshots from the same post-decomposition primitive layer; at O3 the `before`
snapshot is the O0 circuit for the same function. A fallback is not a resource
reduction claim; the report must make clear which schedule actually ran.

## Verification

The contract above is checkable. `qcc verify` and `qcc.verify.verify` compare
a compiled circuit with a reference evaluation of the parsed function. For
each input they check the output register, that every input register is
unchanged, and that every work qubit returned to zero. All inputs are tried
when the arguments total at most 20 bits; beyond that a seeded random sample
is used and the result says so. A sampled pass is evidence, not proof.

The reference evaluates the same parsed LLVM function the circuit was built
from, so it checks lowering and synthesis, not Clang or the LLVM parser. The
test suite covers that remaining gap separately by comparing parsed functions
and circuits against natively compiled C.

## Unsupported in v0.5

The following constructs are outside the authoritative contract and must be
rejected:

- Runtime, data-dependent, or partially unrolled loops; any CFG cycle,
  backedge, self-loop, or residual strongly connected component.
- Indirect branches, computed `goto`, exception edges, and other terminators
  that are not normalized to the supported branch CFG. A `switch` is
  normalized; an `unreachable` that is still reachable is not.
- Constant shift counts outside the operand width, runtime rotates of a
  width that is not a power of two, and source signed-right-shift forms whose
  result depends on implementation-defined C behavior.
- Runtime/general division and remainder, including `x / y` or `x % y` when
  the divisor is not a compile-time constant. qcc does not lower a
  data-dependent divisor.
- Signed division and signed remainder (`sdiv`, `srem`, or signed C `/` and
  `%`) even when the divisor is a constant. Signed quotient/remainder sign
  restoration and the C `INT_MIN / -1` overflow case are outside this
  contract.
- Zero-divisor division or remainder, even if written as a constant. C makes
  this operation undefined, so qcc rejects it rather than choosing quotient or
  remainder bits.
- Floating-point arithmetic, vectors, atomics, and compiler extensions that
  change the fixed-width contract.
- Pointers, arrays, structs, unions, `volatile` access, loads, stores, address
  calculations, dynamic allocation, and global state.
- Recursion (direct or mutual), calls to functions not defined in the same
  file, function pointers and other indirect calls, inline assembly, I/O, and
  other observable side effects.
- Signed arithmetic whose C result can depend on overflow. Signed comparisons
  are supported as specified above; that exception does not make signed
  addition, subtraction, multiplication, division, or remainder generally
  portable. In particular, signed division/remainder remain unsupported as a
  family.
- Multiple return values or return types, void-producing public functions,
  malformed SSA, or a phi with a missing or ambiguously duplicated edge value.
  Operands with mismatched or inferred widths are also rejected.

Clang may lower a source construct into a different LLVM shape. The normalized
LLVM IR is the validator's source of truth: qcc accepts a source program only
when that shape is in the supported list, and it never skips an unrecognized
instruction because the original C looked harmless. Measured benchmark rows
for every optimizer level are recorded in `docs/benchmarks.md`. Pointer and
array support remains outside v0.5.

## Diagnostic policy

Unsupported behavior is a hard error, never a warning followed by a weaker
circuit. Diagnostics follow these rules:

1. Report the source file, line, and column when Clang or the IR parser provides
   them. Otherwise include the function, basic block, instruction, or CFG edge
   that established the error.
2. Name the rejected source or LLVM construct and state the violated rule. For
   example, a residual backedge must say that full constant-bound unrolling is
   required; an external call must name the callee and say that only functions
   defined in the same file can be inlined; a pointer parameter is reported at
   the first instruction that dereferences it.
3. Validate at each boundary. Clang failures, malformed textual LLVM, invalid
   types, unsupported opcodes, malformed phi edge sets, and residual CFG cycles
   must not be silently normalized into a different program.
4. Reject undefined, poison, and `undef` behavior. qcc's modulo arithmetic and
   two's-complement comparison rules apply only after the source program has
   supplied defined values and operations.
5. Keep diagnostics deterministic and source-aware enough for callers to fix
   the program. A missing location is not a reason to accept an unsupported
   construct.
6. Treat inability to prove an optional optimization as a fallback to the last
   verified schedule, not as permission to weaken source validation or emit an
   unverified circuit.

A diagnostic may include the function name and the normalized block/edge path,
but it must not claim that an operation was lowered or tested when validation
stopped before lowering.
