# qcc documentation

This document gives the rules that qcc obeys. It gives the function of a
circuit, the C code that is accepted, and the limits. For commands and
procedures, refer to [USAGE.md](USAGE.md).

This document uses the writing rules of ASD-STE100 Simplified Technical
English. Section 12 gives the technical names and the technical verbs.

## Contents

1. [Scope](#1-scope)
2. [Circuit rules](#2-circuit-rules)
3. [Stages of the compiler](#3-stages-of-the-compiler)
4. [Accepted C](#4-accepted-c)
5. [C that is not accepted](#5-c-that-is-not-accepted)
6. [Optimization levels](#6-optimization-levels)
7. [Level 3 in detail](#7-level-3-in-detail)
8. [Circuit check](#8-circuit-check)
9. [Resource report](#9-resource-report)
10. [Error messages](#10-error-messages)
11. [Limits](#11-limits)
12. [Technical names and verbs](#12-technical-names-and-verbs)
13. [Source tree](#13-source-tree)
14. [References](#14-references)

## 1. Scope

qcc compiles one C function into one reversible circuit. The function must be
pure: its result must come only from its arguments, and it must not change
other data.

qcc does not compile a full C program. It does not make a circuit for one
specified quantum computer. The output contains only the gates `X`, `CX`, and
`CCX`.

## 2. Circuit rules

### 2.1 Registers

A circuit has three types of registers.

| Register | Contents |
| --- | --- |
| Input | One register for each function argument. The register has the name of the argument. |
| Output | One register with the name `output`. It has the width of the result type. |
| Ancilla | The work qubits. The register has the name `ancilla`. |

The width of a register is the number of bits of the C type. Bit 0 is the
least significant bit.

### 2.2 Function of the circuit

At the start, the output register and the ancilla register must be zero. The
input registers contain the arguments.

At the end, these three conditions are true:

- The output register contains the result of the function.
- Each input register contains the same value as at the start.
- Each ancilla qubit is zero.

These conditions make the circuit safe for a superposition of inputs. A work
qubit that is not zero stays entangled with the result. This causes incorrect
results in the quantum algorithm.

### 2.3 Output register that does not start at zero

Some algorithms do not start the output register at zero. For these
algorithms, the circuit must do an exclusive OR of the result into the output
register.

| Level | Result of 1 bit | Result of more than 1 bit |
| --- | --- | --- |
| `O0`, `O1`, `O2` | Exclusive OR | Exclusive OR |
| `O3` | Exclusive OR | Not specified |

At level `O3`, a result of more than 1 bit is calculated in the output
register. Thus the output register must start at zero.

### 2.4 Gates

| Gate | Function |
| --- | --- |
| `X` | Inverts one qubit |
| `CX` | Inverts the target qubit if the control qubit is 1 |
| `CCX` | Inverts the target qubit if the two control qubits are 1 |

Each gate is its own inverse. Thus the inverse of a circuit is the same gates
in the opposite sequence.

## 3. Stages of the compiler

```text
C source
  -> Clang                 LLVM IR text
  -> LLVM parser           checked functions
  -> legalization          functions with basic operations only
  -> lowering (O0 to O2)   circuit
     or synthesis (O3)     circuit
  -> OpenQASM 3, resource report, simulation, or check
```

1. **Clang** changes the C source into LLVM IR. qcc starts Clang with
   optimization and with loop unrolling. It stops vectorization.
2. **The LLVM parser** reads the LLVM IR. It accepts only the instructions in
   section 4. It does not ignore an instruction that it does not know. It
   changes each `switch` into a sequence of comparisons and branches.
3. **Legalization** puts each called function into its caller. It replaces
   intrinsic functions and variable shifts with basic operations.
4. **Lowering** or **synthesis** makes the circuit. Refer to section 6.
5. **The backend** writes OpenQASM 3 text or the resource report.

qcc examines each function independently. If one function is not accepted,
the other functions in the file can be compiled. A function that calls a
function that is not accepted is also not accepted.

## 4. Accepted C

### 4.1 Types

- Signed and unsigned integer types of all widths, for example `uint8_t`,
  `int16_t`, `uint64_t`, `bool`, and `_BitInt(N)`.
- A function has zero or more integer arguments and one integer result.

### 4.2 Operations

| Group | Accepted |
| --- | --- |
| Arithmetic | `+`, `-`, `*`. The result is modulo 2 to the power of the width. |
| Bit operations | `&`, `\|`, `^`, `~` |
| Shifts | `<<` and `>>` by a constant or by a variable |
| Rotations | By a constant, or by a variable if the width is a power of 2 |
| Comparisons | `==`, `!=`, `<`, `<=`, `>`, `>=`, signed and unsigned |
| Conversions | Casts between integer types |
| Division | Unsigned `/` and `%` by a constant that is not zero |
| Selection | `?:` |

A shift by the width or more has undefined behavior in C. The circuit result
for this shift is not specified.

### 4.3 Control flow

- `if`, `else`, and early `return`.
- `switch`, with or without `default`.
- Loops that have a constant number of cycles. Clang must remove the loop
  fully.

The control flow must have no cycle after Clang completes its work.

### 4.4 Function calls

A call to a function in the same file is accepted. qcc puts the body of the
called function into the caller. The keywords `static`, `inline`, and
`__attribute__((noinline))` are accepted.

### 4.5 Intrinsic functions

Clang changes some C code into LLVM intrinsic functions. qcc accepts these
intrinsic functions:

| Intrinsic function | Usual C source |
| --- | --- |
| `llvm.umin`, `llvm.umax`, `llvm.smin`, `llvm.smax` | `a < b ? a : b` |
| `llvm.abs` | `a < 0 ? -a : a` |
| `llvm.ctpop` | `__builtin_popcount` |
| `llvm.ctlz`, `llvm.cttz` | `__builtin_clz`, `__builtin_ctz` |
| `llvm.bswap` | `__builtin_bswap16`, `__builtin_bswap32`, `__builtin_bswap64` |
| `llvm.bitreverse` | `__builtin_bitreverse8` |
| `llvm.fshl`, `llvm.fshr` | A rotation |
| `llvm.uadd.sat`, `llvm.usub.sat` | Addition or subtraction with a limit |

## 5. C that is not accepted

qcc gives an error for each item in this table. It does not make an
approximate circuit.

| Item | Cause |
| --- | --- |
| Pointers and arrays | The circuit has no memory |
| Global variables | The result must come only from the arguments |
| Floating-point types | Only integer operations have a circuit |
| Calls to functions in other files | The body of the function is not known |
| Recursion | The circuit must have a constant size |
| Function pointers, inline assembly | The called code is not known |
| Loops without a constant limit | The circuit must have a constant size |
| Division by a variable | No circuit is available for this operation |
| Signed division | No circuit is available for this operation |
| Division by zero | The behavior is undefined in C |
| Code that has undefined behavior | The function has no result for some inputs |
| Functions with no result | A circuit must have an output register |

## 6. Optimization levels

| Level | Method |
| --- | --- |
| `O0` | The baseline. The compiler makes one register for each value. It calculates all values, copies the result, and then calculates in the opposite sequence. |
| `O1` | The same circuit as `O0` at this time. |
| `O2` | The `O0` method. The compiler also makes the values simpler, removes pairs of gates that cancel, and uses clean work qubits again. |
| `O3` | Synthesis. The compiler makes the circuit from the data flow of the full function. |

All levels obey the circuit rules in section 2. A higher level does not
always give a smaller circuit.

Level `O3` is available only for C source. `qcc.compile_c` also accepts a
dictionary that contains SSA instructions. For this input, qcc uses level
`O2`, and the resource report shows `requested_level` 3 and
`effective_level` 2.

The method of level `O0` is the Bennett method [1]. The second calculation,
in the opposite sequence, makes each work qubit zero again.

## 7. Level 3 in detail

### 7.1 Data-flow graph

The compiler changes the function into one graph of values. Two operations
that are the same become one node. Constants are calculated at this stage.

A branch becomes a selection. The circuit calculates the two sides of the
branch and then selects one value. This is correct because the accepted C has
no side effects.

### 7.2 Operations without gates

Some operations only move bits or give constant bits. These operations add no
work qubit. They add no gate, except an `X` gate for an inverted bit:

- Constants
- Shifts and rotations by a constant
- Casts
- AND, OR, and XOR with a constant
- The NOT operation
- Byte swap and bit reversal

A function that contains only these operations becomes a circuit of `CX`
gates and some `X` gates. The `CX` gates copy the result into the output
register.

### 7.3 Arithmetic

| Operation | Method |
| --- | --- |
| Addition | A ripple-carry adder that adds into its target [2]. It keeps each carry in a wire of the other operand, and then puts the wire back. An 8-bit addition uses 1 work qubit. |
| Subtraction | Addition of the inverted operand with a carry of 1 |
| Comparison | The carry of a subtraction. The sum is not calculated. |
| Multiplication | One controlled addition for each bit of one operand |
| Division by a constant | Restoring division. The quotient and the remainder come from one calculation. |

Each operation makes its work qubits zero again before the next operation
starts.

### 7.4 Result and cleanup

The compiler does these steps:

1. It calculates each intermediate value.
2. It calculates the result directly into the output register.
3. It does the gates of step 1 again in the opposite sequence.

Step 3 makes each work qubit zero. The gates of step 2 are done only one
time. Thus only the intermediate values have the cost of two calculations.

## 8. Circuit check

The check compares a circuit with the function that the LLVM parser made. It
makes sure that the conditions in section 2.2 are true.

- If the arguments have a total of 20 bits or less, the check uses all inputs.
- If the arguments have more bits, the check uses 4096 random inputs. The seed
  is constant, thus the result is the same each time.

The check calculates one bit of all inputs in one large integer. Thus a check
of 65,536 inputs is complete in less than one second for a small circuit.

The check has these limits:

- A check with random inputs is not a proof.
- The check does not examine Clang or the LLVM parser. Different tests compare
  the parsed functions with native C code.
- The check starts the output register at zero.

## 9. Resource report

| Field | Contents |
| --- | --- |
| `qubit_count` | The total number of qubits |
| `ancillas` | The number of work qubits |
| `gate_count` | The total number of gates |
| `x`, `cx`, `ccx` | The number of gates of each type |
| `mcx` | The number of gates with more than two controls, before they are divided into `CCX` gates |
| `depth` | The number of layers when gates on different qubits are done at the same time |
| `toffoli_estimate` | The number of `CCX` gates after all gates are divided |
| `t_count` | Seven times `toffoli_estimate` |
| `requested_level` | The level that you gave |
| `effective_level` | The level that qcc used |
| `before` | The data for the baseline circuit |
| `after` | The data for the circuit that qcc made |

The T estimate uses the usual circuit of seven T gates for one `CCX` gate
[5]. It is an estimate and not a measurement on hardware.

## 10. Error messages

An error message has this form:

```text
qcc: error: FILE:LINE:COLUMN: error [CODE]: TEXT
```

| Code | Cause |
| --- | --- |
| `unsupported-construct` | The C code is not accepted. Refer to section 5. |
| `unknown-function` | The file does not contain the function name |
| `clang-not-found` | The `clang` command is not available |
| `clang-compilation-failed` | Clang found an error in the C source |

The position is a position in the C source. For a global variable, it is the
position of the declaration. If qcc cannot find the position, the message
gives only the file name. Some messages from the lowering stage have no
position.

## 11. Limits

- Level `O1` gives the same circuit as level `O0`.
- Level `O3` calculates all branches of an `if` or a `switch`. A function
  with large branches gives a large circuit.
- Level `O3` keeps each intermediate value until the end. The number of
  qubits increases with the number of intermediate values.
- Level `O3` calculates each value with its full width. It does not find
  bits that are always zero.
- `__builtin_clz` and `__builtin_ctz` calculate with 32 bits and can give
  hundreds of qubits.
- The output contains only `X`, `CX`, and `CCX` gates. qcc does not change
  the circuit for a specified quantum computer.
- The `--samples` option of `compile`, `stats`, and `check` has no effect.
- The `json` output format is not stable.
- At levels `O0` to `O2`, a message for division by a variable has no source
  position.

## 12. Technical names and verbs

### 12.1 Technical names

| Name | Definition |
| --- | --- |
| Ancilla | A work qubit that starts at zero and must end at zero |
| Baseline | The circuit from level `O0` |
| Circuit | A sequence of gates on a constant number of qubits |
| Clang | The C compiler of the LLVM project |
| Data-flow graph | A graph where each node is one value of the function |
| Depth | The number of gate layers in a circuit |
| Intrinsic function | An operation of LLVM that has the form of a function call |
| LLVM IR | The intermediate representation that Clang writes |
| OpenQASM 3 | A text format for quantum circuits |
| Oracle | A circuit that calculates a classical function for a quantum algorithm |
| Pure function | A function without side effects |
| Qubit | The quantum unit of data |
| Register | A group of qubits that contains one value |
| Reversible circuit | A circuit that has an inverse |
| SSA | Static single assignment: each value is set only one time |
| T estimate | The estimated number of T gates |
| Toffoli gate | A different name for the `CCX` gate |

### 12.2 Technical verbs

| Verb | Definition |
| --- | --- |
| Compile | To change source code into a different form |
| Parse | To read text and make a data structure from it |
| Simulate | To calculate the result of a circuit with a classical computer |
| Clone | To make a local copy of a repository |
| Install | To put software on a computer |

## 13. Source tree

| Path | Contents |
| --- | --- |
| `src/qcc/frontend/clang.py` | Starts Clang |
| `src/qcc/frontend/llvm_parser.py` | The LLVM parser |
| `src/qcc/frontend/legalize.py` | Legalization |
| `src/qcc/ir/` | The data structures for functions and circuits |
| `src/qcc/lower/`, `src/qcc/lib/` | Lowering for levels `O0` to `O2` |
| `src/qcc/opt/` | The steps that level `O2` adds |
| `src/qcc/synth/` | Synthesis for level `O3` |
| `src/qcc/sim/` | The simulators |
| `src/qcc/verify.py` | The circuit check |
| `src/qcc/backend/` | OpenQASM 3 output and the resource report |
| `src/qcc/cli.py` | The command line |
| `scripts/` | The benchmark script and the random test script |
| `tests/` | The tests |
| `examples/` | The example C files and the Grover demonstration |

## 14. References

1. C. H. Bennett, "Logical reversibility of computation," *IBM Journal of
   Research and Development*, vol. 17, no. 6, 1973.
2. S. A. Cuccaro, T. G. Draper, S. A. Kutin, and D. P. Moulton, "A new
   quantum ripple-carry addition circuit," arXiv:quant-ph/0410184, 2004.
3. L. K. Grover, "A fast quantum mechanical algorithm for database search,"
   *Proceedings of the 28th ACM Symposium on Theory of Computing*, 1996.
4. A. Parent, M. Roetteler, and K. M. Svore, "Reversible circuit compilation
   with space constraints," arXiv:1510.00377, 2015.
5. M. A. Nielsen and I. L. Chuang, *Quantum Computation and Quantum
   Information*, Cambridge University Press, 2000.

Related documents in this repository:

- [docs/supported_c.md](docs/supported_c.md): the detailed contract for the
  accepted C.
- [docs/design.md](docs/design.md): the internal design.
- [docs/decisions.md](docs/decisions.md): the design decisions.
- [docs/benchmarks.md](docs/benchmarks.md): the measured data.
- [docs/plan.md](docs/plan.md): the milestones.
