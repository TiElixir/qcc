# Supported C subset

This document is the source-level contract for qcc v0.1. It describes the
constructs that may be lowered after Clang emits textual LLVM IR. A program
that uses a construct outside this list must be rejected with a diagnostic;
qcc must not silently compile a weaker interpretation.

## Supported in v0.1

### Function shape

- A function with integer arguments and an integer return value.
- A single straight-line basic block ending in `return`.
- No observable side effects: the function may not read or write global state,
  perform I/O, or call another function.
- Fixed-width unsigned integer types, with `uint8_t` as the primary supported
  public type. Widths are explicit in the generated IR.

### Expressions

The v0.1 lowering contract covers the following operations when their integer
widths and operands are valid:

- Integer constants and function arguments.
- Addition and subtraction (`+`, `-`).
- Bitwise AND, OR, and XOR (`&`, `|`, `^`).
- Bitwise complement (`~`) when represented by the equivalent supported LLVM
  pattern.
- Zero extension and truncation between supported integer widths.
- Returning a supported integer value.

Operations are evaluated modulo the result width. The compiler's fixed-width
semantics apply only to defined source programs; C undefined behavior is not
made reversible or portable by qcc.

A representative v0.1-shaped function is:

```c
#include <stdint.h>

uint8_t mix(uint8_t x, uint8_t y) {
    return (uint8_t)((x ^ y) + 3u);
}
```

## Rejected in v0.1

The following are outside the first release, even when Clang can lower them
to LLVM IR:

- Equality or ordered comparisons, `if`, `switch`, conditional expressions,
  `select`, and `phi` nodes.
- Loops or any cyclic control-flow graph.
- Multiplication, division, remainder, variable shifts, and rotates.
- Pointers, arrays, structs, unions, volatile access, loads, stores, and
  dynamic allocation.
- Function calls, recursion, inline assembly, atomics, and observable I/O.
- Floating-point types, signed arithmetic whose behavior depends on overflow,
  and vector types.
- Pragmas or compiler extensions that change the fixed-width contract.

Comparisons, predication, and bounded acyclic control flow are planned for
v0.2. Arithmetic growth and host integration are later milestones; see
[plan.md](plan.md).

## Frontend expectations

qcc relies on Clang to produce textual LLVM IR and may select optimization
flags that canonicalize equivalent C expressions. The accepted IR is the
source of truth for the validator, so a construct is supported only when its
optimized representation is also in the accepted operation set. Clang is a
required tool for integration use and is installed in CI.
