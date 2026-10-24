# Supported C subset

This document is the authoritative source-level contract for qcc v0.3. It
covers the constructs that may be lowered after Clang emits normalized textual
LLVM IR. The v0.3 contract is deliberately narrower than C: a construct is
supported only when its generated IR satisfies the value, CFG, arithmetic, and
cleanup rules below.

## Supported in v0.3

### Function and value model

- A pure function with fixed-width scalar integer arguments and one fixed-width
  integer return value. `uint8_t` remains the primary public example; other
  explicit widths are accepted when the IR and resource limits support them.
- Fixed-width signed integer values for defined comparisons. Signed predicates
  interpret their operands as two's-complement bit patterns. General signed
  arithmetic that can overflow is not made defined by qcc.
- No observable side effects: the function may not read or write global state,
  perform I/O, access memory, or call another function.
- Defined source behavior only. C undefined behavior, poison, and `undef`
  values are rejected rather than assigned reversible semantics.

### Integer expressions

The v0.3 lowering contract covers these operations when widths and operands are
valid:

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
- Constant-count shifts. Unsigned left/right shifts are supported when the
  count is a compile-time value in `0 <= count < width`. A signed right shift
  is accepted only when the frontend supplies the documented two's-complement
  `ashr` semantics.
- Constant-count rotates represented by a recognized fixed-width rotate idiom
  or frontend operation. The count is reduced modulo the operand width; a
  variable rotate is not supported.
- Unsigned division and remainder by a compile-time nonzero constant. The
  normalized operations are `udiv` and `urem`, the divisor is represented at
  the operation width, and the restoring schedule computes `floor(x / d)` or
  `x % d` for `1 <= d < 2**width`.
- Returning a supported integer value.

### Control flow and SSA

- A single entry block and a finite, reachable CFG made from unconditional and
  conditional branches and integer returns. Nested `if`/`else` regions are
  supported after normalization.
- `phi` nodes at the beginning of a merge block. Each reachable predecessor
  edge has exactly one same-width incoming value, and the incoming value is
  selected by the edge that actually reached the block.
- The frontend may fully unroll a source loop with a compile-time iteration
  bound. The resulting CFG may retain conditional exits, but it must contain
  no backedge or cycle before lowering.
- A source conditional expression may lower to `select` instead of a branch;
  both forms use the same reversible predicate and mux semantics.

## Unsupported in v0.3

The following constructs are outside the authoritative v0.3 contract and must
be rejected:

- Runtime, data-dependent, or partially unrolled loops; any CFG cycle,
  backedge, self-loop, or residual strongly connected component.
- `switch`, indirect branches, computed `goto`, exception edges, and other
  terminators that are not normalized to the supported branch CFG.
- Variable shifts and rotates, shift counts outside the operand width, and
  source signed-right-shift forms whose result depends on implementation-
  defined C behavior.
- Runtime/general division and remainder, including `x / y` or `x % y` when
  the divisor is not a compile-time constant. qcc does not lower a
  data-dependent divisor.
- Signed division and signed remainder (`sdiv`, `srem`, or signed C `/` and
  `%`) even when the divisor is a constant. Signed quotient/remainder sign
  restoration and the C `INT_MIN / -1` overflow case are outside this
  v0.3 contract.
- Zero-divisor division or remainder, even if written as a constant. C makes
  this operation undefined, so qcc rejects it rather than choosing quotient or
  remainder bits.
- Floating-point arithmetic, vectors, atomics, and compiler extensions that
  change the fixed-width contract.
- Pointers, arrays, structs, unions, `volatile` access, loads, stores, address
  calculations, dynamic allocation, and global state.
- Function calls, recursion, function pointers, inline assembly, I/O, and
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
instruction because the original C looked harmless. The v0.3 implementation
and acceptance suite are integrated in this checkout; resource optimization
and broader pointer/array support remain later work.

## Diagnostic policy

Unsupported behavior is a hard error, never a warning followed by a weaker
circuit. Diagnostics follow these rules:

1. Report the source file, line, and column when Clang or the IR parser provides
   them. Otherwise include the function, basic block, instruction, or CFG edge
   that established the error.
2. Name the rejected source or LLVM construct and state the violated rule. For
   example, a residual backedge must say that full constant-bound unrolling is
   required; a variable shift must say that only constant wiring is supported.
3. Validate at each boundary. Clang failures, malformed textual LLVM, invalid
   types, unsupported opcodes, malformed phi edge sets, and residual CFG cycles
   must not be silently normalized into a different program.
4. Reject undefined, poison, and `undef` behavior. qcc's modulo arithmetic and
   two's-complement comparison rules apply only after the source program has
   supplied defined values and operations.
5. Keep diagnostics deterministic and source-aware enough for callers to fix
   the program. A missing location is not a reason to accept an unsupported
   construct.

A diagnostic may include the function name and the normalized block/edge path,
but it must not claim that an operation was lowered or tested when validation
stopped before lowering.
