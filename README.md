# qcc

[![CI](https://github.com/TiElixir/Qmpiler/actions/workflows/ci.yml/badge.svg)](https://github.com/TiElixir/Qmpiler/actions/workflows/ci.yml)
[![Status: Beta](https://img.shields.io/badge/status-beta-f59e0b.svg)](pyproject.toml)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**qcc** is a compiler. It changes a pure C function into a reversible quantum
circuit.

A quantum algorithm, for example Grover search, calculates a classical function
on a superposition of inputs. For this, the function must be a reversible
circuit. If you write this type of circuit manually, the work is slow and
errors are easy to make. With qcc, you write the function in C.

qcc uses Clang to change the C source into LLVM IR. Then it makes a circuit
that contains only `X`, `CX`, and `CCX` gates. The circuit writes the result
into an output register. It does not change the inputs, and it sets each work
qubit to zero again.

## Example

This C function is a search condition:

```c
#include <stdint.h>
#include <stdbool.h>

bool is_solution(uint8_t x) {
    return (uint8_t)(x * x + 3) % 17 == 5;
}
```

The `verify` command compiles the function. Then it compares the circuit with
the function for each possible input:

```console
$ qcc verify examples/benchmarks.c --function is_solution
ok: is_solution: verified on all 256 inputs [O3: 43 qubits, 840 gates, T estimate 2401]
```

A small function gives a small circuit. This function has a 3-bit input:

```c
typedef unsigned _BitInt(3) grover_input_t;
typedef unsigned _BitInt(1) grover_output_t;

grover_output_t grover_oracle(grover_input_t x) {
    return x == 6;
}
```

```console
$ qcc compile examples/grover_oracle.c --function grover_oracle --opt O3
OPENQASM 3.0;
include "stdgates.inc";

qubit[3] x;
qubit[1] output;
qubit[1] ancilla;

ccx x[0], x[1], ancilla[0];
cx x[1], ancilla[0];
ccx ancilla[0], x[2], output[0];
cx x[1], ancilla[0];
ccx x[0], x[1], ancilla[0];
```

## Measured results

The table compares the baseline level (`O0`) with the highest level (`O3`).
The file `examples/benchmarks.c` contains the five functions.

| Function | Qubits (`O0` → `O3`) | T estimate (`O0` → `O3`) | Decrease |
| --- | ---: | ---: | ---: |
| `add` | 41 → 25 | 448 → 98 | 4.6x |
| `multiply` | 97 → 33 | 35,840 → 1,281 | 28.0x |
| `compare` | 49 → 25 | 504 → 112 | 4.5x |
| `popcount` | 303 → 50 | 3,136 → 686 | 4.6x |
| `is_solution` | 209 → 43 | 66,038 → 2,401 | 27.5x |

The T estimate is a usual cost measure for a fault-tolerant quantum computer.
qcc counts seven T gates for each `CCX` gate. The script
`scripts/benchmarks.py` measures all these values. Refer to
[docs/benchmarks.md](docs/benchmarks.md) for the full data.

## Functions

- **Usual C code.** qcc accepts integer arithmetic, comparisons, `if` and
  `else`, `switch`, shifts, loops with a constant limit, and calls to functions
  in the same file.
- **Four optimization levels.** Level `O3` makes the circuit from the data
  flow of the full function. The other levels change one instruction at a time.
- **A circuit check.** `qcc verify` compares the circuit with the C function
  for each input. If the number of inputs is too large, it uses a random
  sample.
- **A resource report.** `qcc stats` gives the number of qubits, gates, and
  T gates, and the circuit depth.
- **OpenQASM 3 output** with one named register for each function argument.
- **Clear error messages.** An error message gives the file, the line, and
  the cause.
- **No Python dependencies.** Only Python and Clang are necessary.

## Installation

1. Make sure that Python 3.10 or a later version is installed.
2. Make sure that the `clang` command is available.
3. Get the source code:

   ```console
   $ git clone https://github.com/TiElixir/Qmpiler.git
   $ cd Qmpiler
   ```

4. Install the package:

   ```console
   $ python -m pip install -e ".[dev]"
   ```

Qiskit is optional. It is necessary only for the Qiskit adapter. To install
it, use `python -m pip install -e ".[qiskit]"`.

## Quick start

Do these steps in the repository directory.

1. Make sure that qcc can compile the function:

   ```console
   $ qcc check examples/add.c --function add
   ok: examples/add.c (add)
   ```

2. Compare the circuit with the C function:

   ```console
   $ qcc verify examples/add.c --function add
   ok: add: verified on all 65536 inputs [O3: 25 qubits, 49 gates, T estimate 98]
   ```

3. Write the circuit to an OpenQASM 3 file:

   ```console
   $ qcc compile examples/add.c --function add --opt O3 -o add.qasm
   ```

4. Show the resource report:

   ```console
   $ qcc stats examples/add.c --function add --opt O3
   ```

You can also use qcc from Python:

```python
import qcc

circuit = qcc.compile_c("examples/add.c", function="add", opt=3)
print(circuit.report().t_count)  # 98
print(qcc.simulate(circuit, {"a": 5, "b": 7}))  # 12
print(circuit.to_qasm3())
```

Refer to [USAGE.md](USAGE.md) for all commands, options, and procedures.

## Optimization levels

| Level | Effect |
| --- | --- |
| `O0` | The baseline. Each value gets its own register. |
| `O1` | The same circuit as `O0` at this time. |
| `O2` | `O0`, then removal of unnecessary gates and work qubits. |
| `O3` | A new circuit from the data flow of the full function. |

The commands `compile`, `stats`, and `check` use `O0` if you do not give a
level. The command `verify` uses `O3`. Use `--opt O3` to get the smallest
circuit.

## Accepted C

qcc accepts a function that has integer arguments and one integer result. The
function must not have side effects.

- Accepted: `+`, `-`, `*`, bit operations, casts, all comparisons, `?:`, `if`
  and `else`, `switch`, shifts, rotations, loops with a constant limit, and
  division by a constant. Calls to functions in the same file are also
  accepted.
- Not accepted: pointers, arrays, global variables, floating-point values,
  input and output, recursion, and calls to external functions.
- Also not accepted: loops without a constant limit, division by a variable,
  and signed division.

If one function in a file is not accepted, qcc can compile the other
functions in that file. Refer to [DOCUMENTATION.md](DOCUMENTATION.md) for the
full rules.

## Documentation

| Document | Contents |
| --- | --- |
| [USAGE.md](USAGE.md) | Installation, commands, the Python interface, and troubleshooting |
| [DOCUMENTATION.md](DOCUMENTATION.md) | The circuit rules, the accepted C, the levels, and the limits |
| [docs/supported_c.md](docs/supported_c.md) | The detailed contract for the accepted C |
| [docs/design.md](docs/design.md) | The internal design |
| [docs/benchmarks.md](docs/benchmarks.md) | The measured data |
| [docs/decisions.md](docs/decisions.md) | The design decisions and their causes |
| [CHANGELOG.md](CHANGELOG.md) | The changes in each version |

The files README.md, USAGE.md, and DOCUMENTATION.md use the writing rules of
ASD-STE100 Simplified Technical English.

## Tests

These four commands must give no errors before each change is accepted:

```console
$ ruff check .
$ ruff format --check .
$ mypy --strict src
$ pytest -q
```

The script `scripts/fuzz.py` makes random functions. It compiles each
function and compares the circuit with the function for each input:

```console
$ python scripts/fuzz.py --count 2000 --seed 1
2000 random functions verified exhaustively at O3 (seeds 1..2000)
```

## License

qcc has the [MIT License](LICENSE).
