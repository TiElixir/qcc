# qcc usage

This document tells you how to install qcc and how to use it. For the rules
that qcc obeys, refer to [DOCUMENTATION.md](DOCUMENTATION.md).

This document uses the writing rules of ASD-STE100 Simplified Technical
English.

## Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [Commands](#3-commands)
4. [Procedures](#4-procedures)
5. [Python interface](#5-python-interface)
6. [Rules for the C source](#6-rules-for-the-c-source)
7. [Use of a circuit as an oracle](#7-use-of-a-circuit-as-an-oracle)
8. [Scripts](#8-scripts)
9. [Troubleshooting](#9-troubleshooting)

## 1. Requirements

| Item | Requirement |
| --- | --- |
| Python | Version 3.10 or later |
| Clang | The `clang` command must be on the `PATH` |
| Qiskit | Optional. Version 1.0 or later |

qcc was tested with Clang 22.1 and Python 3.14. The runtime uses only the
Python standard library.

## 2. Installation

1. Get the source code:

   ```console
   $ git clone https://github.com/TiElixir/Qmpiler.git
   $ cd Qmpiler
   ```

2. Install the package and the tools for the tests:

   ```console
   $ python -m pip install -e ".[dev]"
   ```

3. Make sure that the installation is correct:

   ```console
   $ qcc check examples/add.c --function add
   ok: examples/add.c (add)
   ```

If Clang is not installed on Debian or Ubuntu, use this command:

```console
$ sudo apt-get install --no-install-recommends -y clang
```

To use the Qiskit adapter, install Qiskit:

```console
$ python -m pip install -e ".[qiskit]"
```

## 3. Commands

A command has this form:

```text
qcc COMMAND SOURCE [OPTIONS]
```

`SOURCE` is the path of a C source file.

| Command | Function |
| --- | --- |
| `qcc check` | Compiles the function and makes sure that the output is valid |
| `qcc compile` | Compiles the function and writes the circuit |
| `qcc stats` | Compiles the function and writes the resource report |
| `qcc verify` | Compiles the function and compares the circuit with the function |

### 3.1 Options of `check`, `compile`, and `stats`

| Option | Default | Function |
| --- | --- | --- |
| `--function NAME` | `main` | The function that qcc compiles |
| `--opt LEVEL` | `O0` | The optimization level: `O0`, `O1`, `O2`, or `O3` |
| `--emit FORMAT` | Refer to the note | The output format: `qasm3`, `stats`, or `json` |
| `-o PATH`, `--output PATH` | Standard output | The file that gets the output |
| `--dump-ir` | Off | Writes the LLVM IR from Clang to standard error |
| `--samples N` | None | The compiler does not use this option at this time |

Note: The default format is `qasm3` for `compile`. It is `stats` for `stats`
and `check`.

Note: The `json` format is a copy of the internal circuit data. Its structure
can change in a later version.

If the file contains only one function, you can omit `--function`.

### 3.2 Options of `verify`

| Option | Default | Function |
| --- | --- | --- |
| `--function NAME` | `main` | The function that qcc compiles |
| `--opt LEVEL` | `O3` | The optimization level of the circuit |
| `--exhaustive-bits BITS` | `20` | The largest number of input bits for a check of all inputs |
| `--samples N` | `4096` | The number of random inputs when the number of input bits is too large |
| `--seed SEED` | `0` | The seed for the random inputs |

### 3.3 Exit status

| Status | Cause |
| --- | --- |
| `0` | The command was successful |
| `1` | qcc found an error in the source, or the check found an incorrect circuit |
| `2` | The command line was incorrect |
| `130` | You stopped the command |

## 4. Procedures

Do these procedures in the repository directory.

### 4.1 Compile a function into OpenQASM 3

1. Write the C function in a file. Obey the rules in section 6.
2. Compile the function:

   ```console
   $ qcc compile examples/add.c --function add --opt O3 -o add.qasm
   ```

3. Open the file `add.qasm`. The first lines are:

   ```qasm
   OPENQASM 3.0;
   include "stdgates.inc";

   qubit[8] a;
   qubit[8] b;
   qubit[8] output;
   qubit[1] ancilla;
   ```

Each function argument has one register with the same name. The register
`output` gets the result. The register `ancilla` contains the work qubits.

### 4.2 Compare a circuit with its function

1. Start the check:

   ```console
   $ qcc verify examples/add.c --function add
   ok: add: verified on all 65536 inputs [O3: 25 qubits, 49 gates, T estimate 98]
   ```

2. Read the first word of the output. The word `ok` shows a correct circuit.
3. Read the text after the function name. It shows the number of inputs in
   the check.

The check makes sure of three things for each input:

- The output register contains the value of the function.
- Each input register contains the same value as before.
- Each work qubit is zero.

If the arguments have a total of 20 bits or less, the check uses all inputs.
If they have more bits, the check uses random inputs. The output then
contains the word `sampled`:

```console
$ qcc verify tests/fixtures/v02_arithmetic.c --function clamp8
ok: clamp8: verified on 4096 sampled inputs of 2**24 [O3: 43 qubits, 420 gates, T estimate 616]
```

Note: A check with sampled inputs is not a proof. It shows only that the
circuit is correct for those inputs.

To do the check for a different level, use `--opt`:

```console
$ qcc verify examples/benchmarks.c --function multiply --opt O2
ok: multiply: verified on all 65536 inputs [O2: 97 qubits, 2478 gates, T estimate 28560]
```

### 4.3 Get the resource report

1. Start the command:

   ```console
   $ qcc stats examples/add.c --function add --opt O3
   ```

2. Read the JSON output. The table shows the primary fields.

| Field | Contents |
| --- | --- |
| `qubit_count` | The total number of qubits |
| `ancillas` | The number of work qubits |
| `gate_count` | The total number of gates |
| `x`, `cx`, `ccx` | The number of gates of each type |
| `depth` | The circuit depth |
| `t_count` | The T estimate: seven for each `CCX` gate |
| `requested_level` | The level that you gave |
| `effective_level` | The level that qcc used |
| `before` | The same data for the baseline circuit |
| `after` | The same data for the circuit that qcc made |

### 4.4 Show the LLVM IR

Use this procedure to find the cause of an error message.

1. Add `--dump-ir` to the command:

   ```console
   $ qcc check examples/add.c --function add --dump-ir
   ```

2. Read the LLVM IR on standard error.

### 4.5 Select the optimization level

| Level | Use |
| --- | --- |
| `O0` | To compare resource data between versions of qcc |
| `O2` | To get a smaller circuit that has the same structure as `O0` |
| `O3` | To get the smallest circuit |

Level `O1` gives the same circuit as `O0` at this time.

## 5. Python interface

### 5.1 Compile and simulate

```python
import qcc

source = """
#include <stdint.h>
uint8_t add(uint8_t a, uint8_t b) { return a + b; }
"""

circuit = qcc.compile_c(source=source, function="add", opt=3)

# qcc.simulate gives the value of the output register.
assert qcc.simulate(circuit, {"a": 5, "b": 7}) == 12

# Circuit.simulate gives all registers.
state = circuit.simulate({"a": 200, "b": 100})
assert state == {"a": 200, "b": 100, "output": 44, "ancillas": {24: 0}}
```

`qcc.compile_c` accepts C source text, the path of a C file, or a function
from `qcc.parse_c`. The default level is `opt=1`.

### 5.2 Read the circuit

```python
import qcc

circuit = qcc.compile_c("examples/add.c", function="add", opt=3)

circuit.num_qubits  # 25
circuit.gate_count  # 49
circuit.depth  # 35
circuit.layout.inputs["a"]  # (0, 1, 2, 3, 4, 5, 6, 7)
circuit.layout.output  # (16, 17, 18, 19, 20, 21, 22, 23)
circuit.layout.ancillas  # (24,)

for gate in circuit.gates:
    print(gate.op, gate.controls, gate.target)

print(circuit.to_qasm3())
```

Bit 0 of a register is the least significant bit.

### 5.3 Read the resource report

```python
import qcc

circuit = qcc.compile_c("examples/add.c", function="add", opt=3)
report = circuit.report()

report.qubit_count  # 25
report.ancillas  # 1
report.ccx  # 14
report.t_count  # 98
report.effective_level  # 3
report.before["t_count"]  # 448, from the baseline circuit
report.as_dict()  # all fields
```

### 5.4 Compare a circuit with its function

```python
import qcc
from qcc.verify import assert_verified, verify

function = qcc.parse_c("examples/add.c", "add")
circuit = qcc.compile_c(function, function="add", opt=3)

result = verify(circuit, function)
assert result.ok
assert result.exhaustive  # the check used all inputs
assert result.vectors == 65536  # the number of inputs

assert_verified(circuit, function)  # gives VerificationError for a bad circuit
```

If the circuit is incorrect, `result.counterexample` contains the first bad
input. The field `kind` is `output`, `input`, or `ancilla`.

### 5.5 Use the circuit in Qiskit

Qiskit must be installed for this procedure.

```python
import qcc

circuit = qcc.compile_c("examples/grover_oracle.c", function="grover_oracle", opt=3)
print(circuit.to_qiskit().draw("text"))
```

```text
q_0: ──■───────────────────■──
       │                   │
q_1: ──■────■─────────■────■──
       │    │         │    │
q_2: ──┼────┼────■────┼────┼──
       │    │  ┌─┴─┐  │    │
q_3: ──┼────┼──┤ X ├──┼────┼──
     ┌─┴─┐┌─┴─┐└─┬─┘┌─┴─┐┌─┴─┐
q_4: ┤ X ├┤ X ├──■──┤ X ├┤ X ├
     └───┘└───┘     └───┘└───┘
```

The qubits `q_0` to `q_2` are the input `x`. The qubit `q_3` is the output.
The qubit `q_4` is the work qubit.

### 5.6 Errors

```python
import qcc
from qcc.errors import CompilerError

try:
    qcc.compile_c(source="float f(float a) { return a; }", function="f")
except CompilerError as error:
    print(error.diagnostic.code)  # unsupported-construct
    print(error.location)  # <stdin>:1:1
    print(error.message)
```

Most compiler errors are a `CompilerError`. `UnsupportedConstructError` is the
error for C code that qcc does not accept.

## 6. Rules for the C source

Obey these rules when you write a function for qcc.

1. Use only integer types for the arguments and the result.
2. Give the function one result. Do not use `void`.
3. Do not use pointers, arrays, or global variables.
4. Do not call a function from a different file or from a library.
5. Do not use recursion.
6. Give each loop a constant number of cycles.
7. Divide only by a constant that is not zero. Use unsigned types for division.
8. Do not write code that has undefined behavior.

### 6.1 Integer widths

The standard types `uint8_t`, `uint16_t`, `uint32_t`, `uint64_t`, and `bool`
are accepted. The signed types are also accepted.

Use `_BitInt(N)` to get a register with `N` qubits:

```c
typedef unsigned _BitInt(3) grover_input_t;
typedef unsigned _BitInt(1) grover_output_t;

grover_output_t grover_oracle(grover_input_t x) {
    return x == 6;
}
```

Note: A small width decreases the number of qubits. It also lets the check
use all inputs.

### 6.2 Functions in the same file

You can divide the code into more than one function. qcc puts the body of
each called function into the caller.

```c
static uint8_t square(uint8_t x) { return (uint8_t)(x * x); }

uint8_t sum_of_squares(uint8_t a, uint8_t b) {
    return (uint8_t)(square(a) + square(b));
}
```

### 6.3 Conditions

`if`, `else`, `switch`, `?:`, and an early `return` are accepted.

```c
uint8_t select_value(uint8_t a, uint8_t b) {
    switch (a) {
    case 3: return (uint8_t)(b + 1);
    case 200: return (uint8_t)(b * 3);
    default: return b;
    }
}
```

Note: The circuit calculates all branches. Thus a function with large
branches gives a large circuit.

### 6.4 Loops

The number of cycles must be a constant. Clang must be able to remove the
loop fully.

```c
uint8_t popcount(uint8_t a) {
    uint8_t count = 0;
    for (int i = 0; i < 8; i++) count += (a >> i) & 1;
    return count;
}
```

### 6.5 Built-in functions

These Clang built-in functions are accepted: `__builtin_popcount`,
`__builtin_clz`, `__builtin_ctz`, `__builtin_bswap16`, `__builtin_bswap32`,
`__builtin_bswap64`, and `__builtin_bitreverse8`.

Note: `__builtin_clz` and `__builtin_ctz` calculate with 32 bits. They can
give a circuit with hundreds of qubits.

## 7. Use of a circuit as an oracle

Each circuit obeys this rule: if the output register and the work qubits
start at zero, the output register gets the result.

Some algorithms start the output qubit in a different condition. Grover
search is an example. For these algorithms, the circuit must invert the
output qubit when the result is 1. The table shows when a circuit does this.

| Level | Result of 1 bit | Result of more than 1 bit |
| --- | --- | --- |
| `O0`, `O1`, `O2` | Yes | Yes |
| `O3` | Yes | No. The output register must start at zero |

To see an example, start the Grover demonstration:

```console
$ python examples/grover_demo.py --no-qiskit
```

The demonstration compiles `examples/grover_oracle.c` and finds the solution
`x = 6`. If you omit `--no-qiskit` and Qiskit is installed, the demonstration
uses the Qiskit statevector simulator.

## 8. Scripts

### 8.1 Measure the benchmarks

To write the measured data to `docs/benchmarks.md`:

```console
$ python scripts/benchmarks.py
```

To print the data for one function and not change the file:

```console
$ python scripts/benchmarks.py --smoke
```

### 8.2 Do random tests

The script `scripts/fuzz.py` makes random functions. It compiles each
function and compares the circuit with the function for each input.

```console
$ python scripts/fuzz.py --count 2000 --seed 1
2000 random functions verified exhaustively at O3 (seeds 1..2000)
```

| Option | Default | Function |
| --- | --- | --- |
| `--count` | `500` | The number of functions |
| `--seed` | `0` | The first seed |
| `--opt` | `3` | The optimization level |
| `--max-steps` | `12` | The largest number of operations in one function |
| `--max-input-bits` | `10` | The largest total number of input bits |
| `--branch-chance` | `0.15` | The probability that a step starts an `if` |

If a function has a failure, the script prints the function and the bad
input. The exit status is then `1`.

### 8.3 Do the quality checks

```console
$ ruff check .
$ ruff format --check .
$ mypy --strict src
$ pytest -q
```

## 9. Troubleshooting

| Message | Cause | Correction |
| --- | --- | --- |
| `clang executable 'clang' was not found` | Clang is not on the `PATH` | Install Clang |
| `clang failed to compile` | The C source has a syntax error | Read the Clang message that follows. Correct the source |
| `function 'NAME' is not defined in FILE; pass one of: ...` | The function name is incorrect | Use one of the names in the message |
| `source file not found` | The path is incorrect | Correct the path |
| `--opt expects O0, O1, O2, or O3` | The level is not valid | Use a level from `O0` to `O3` |
| `call to external function @NAME` | The function calls code from a different file | Put the called function in the same file |
| `recursive call @A -> @A` | The function calls itself | Replace the recursion with a loop that has a constant limit |
| `memory access through pointer parameter` | An argument is a pointer | Use integer arguments |
| `global variable @NAME` | The function uses a global variable | Change the global variable into an argument |
| `floating-point ... not supported` | The function uses `float` or `double` | Use integer types |
| `a surviving LLVM loop is data-dependent` | The number of cycles is not a constant | Give the loop a constant limit |
| `requires a nonzero compile-time constant divisor` | The divisor is a variable | Divide by a constant |
| `signed division and remainder are not supported` | The function divides a signed value | Use unsigned types |
| `division or remainder by zero is undefined` | The divisor is zero | Use a divisor that is not zero |
| `can reach code that has undefined behavior` | The function has no result for some inputs | Give a result for each input |
| `FAILED: NAME: wrong output for (...)` | The circuit is incorrect | Record the input. Send a problem report |

An error message has this form:

```text
qcc: error: FILE:LINE:COLUMN: error [CODE]: TEXT
```

`LINE` and `COLUMN` give the position in the C source. Some messages have no
position.
