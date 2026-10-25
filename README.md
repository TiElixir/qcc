# qcc

[![Status: Beta](https://img.shields.io/badge/status-beta-f59e0b.svg)](pyproject.toml)
![Python 3.10–3.12](https://img.shields.io/badge/Python-3.10%E2%80%933.12-3776AB?logo=python&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**qcc** is a deliberately bounded compiler from a documented pure-C subset to
reversible quantum circuits. It uses Clang to produce textual LLVM IR, validates
that IR with a project-owned parser, lowers accepted programs to reversible
`X`/`CX`/`CCX` networks, and can emit OpenQASM 3.

The goal is inspectable semantics, explicit limitations, and honest resource
accounting—not general-purpose C coverage or hardware-specific optimization.

## At a glance

```text
C source
  → Clang textual LLVM IR
  → validated qcc IR
  → reversible lowering and clean-ancilla scheduling
  → X/CX/CCX circuit
  → simulation, resource report, or OpenQASM 3
```

The v0.4 feature line is implemented. The latest repository tag is `v0.4.0`,
while the current package metadata reports `0.4.1`.

## Motivating example

This is the small predicate used throughout the documentation:

```c
#include <stdint.h>
#include <stdbool.h>

bool is_solution(uint8_t x) {
    return (uint8_t)(x * x + 3) % 17 == 5;
}
```

qcc treats the accepted fixed-width arithmetic explicitly. For this function,
the compiler validates the generated LLVM shape, builds a reversible oracle,
and preserves the input while computing the result into a clean output
register.

## Requirements

- Python 3.10 or newer (CI covers 3.10–3.12)
- Clang available as `clang` on `PATH`
- A repository checkout for the checked-in examples and benchmark scripts

The Python runtime has no required third-party dependencies. Qiskit is optional
and is only needed for the optional host-side adapter/demo path.

## Installation

From a repository checkout:

```console
$ python -m pip install -e ".[dev]"
```

On Debian or Ubuntu, install Clang separately if it is not already available:

```console
$ sudo apt-get update
$ sudo apt-get install --no-install-recommends -y clang
```

To enable the optional Qiskit adapter:

```console
$ python -m pip install -e ".[qiskit]"
```

Qiskit Aer is a separate optional package if you want shot-based sampling:

```console
$ python -m pip install qiskit-aer
```

## Quickstart

Run these commands from the repository root. The CLI defaults to `--opt O0`,
which is the deterministic compatibility baseline; request `O2` explicitly
when you want the v0.4 optimization path.

```console
$ qcc check examples/add.c --function add
ok: examples/add.c (add)

$ qcc compile examples/add.c --function add --opt O2 --emit qasm3 -o add.qasm
$ qcc stats examples/add.c --function add --opt O2
```

The generated OpenQASM 3 uses named registers and contains the primitive gate
subset emitted by qcc:

```qasm
OPENQASM 3.0;
include "stdgates.inc";

qubit[8] a;
qubit[8] b;
qubit[8] output;
qubit[17] ancilla;
```

`qcc stats` reports logical netlist counts, depth, qubits, clean ancillas,
multi-controlled-X counts, and the documented Toffoli/T-count estimates. The
full report also records the requested/effective optimizer level and measured
before/after snapshots for optimized compilations.

## Python API

```python
import qcc

source = """
#include <stdint.h>
uint8_t add(uint8_t a, uint8_t b) { return a + b; }
"""

circuit = qcc.compile_c(source=source, function="add", opt=2)
report = circuit.report()
qasm = circuit.to_qasm3()

# qcc.simulate() returns the output-register value.
output = qcc.simulate(circuit, {"a": 5, "b": 7})

# Circuit.simulate() returns named registers and declared ancillas.
state = circuit.simulate({"a": 5, "b": 7})
assert state["output"] == output
```

Useful public entry points include:

- `qcc.compile_c(...)` — compile C source text, a source path, or supported
  structural SSA input;
- `Circuit.report()` — inspect resource counts and optimizer metadata;
- `Circuit.to_qasm3()` / `qcc.to_qasm3(...)` — emit validated OpenQASM 3;
- `Circuit.to_qiskit()` / `qcc.to_qiskit(...)` — use the optional Qiskit
  adapter; and
- `qcc.simulate(...)` — run a compiled circuit with the standard-library
  classical simulator.

Registers are LSB-first. Declared ancillas must finish in `0`; the simulator
checks this invariant by default.

The Python API defaults to `opt=1`; the CLI defaults to `--opt O0` so that its
baseline resource output is easy to compare across releases.

## Supported C subset

The authoritative contract is [docs/supported_c.md](docs/supported_c.md).
In summary, qcc accepts defined pure functions with fixed-width integer
arguments and one fixed-width integer result, including:

- integer constants and arguments;
- fixed-width addition, subtraction, multiplication, bitwise operations, and
  explicit-width casts;
- all LLVM integer equality and ordered predicates, with two's-complement
  semantics for signed comparisons;
- `select`, nested acyclic `if`/`else`, and normalized `phi`/branch CFGs;
- constant-count shifts and recognized constant rotates;
- compile-time bounded loops after complete unrolling; and
- unsigned division/remainder by a compile-time **nonzero** constant.

The compiler rejects pointers, arrays, memory, globals, floating point, I/O,
atomics, recursion, function calls, function pointers, inline assembly,
variable shifts/rotates, runtime loops, runtime divisors, signed
division/remainder, and undefined/poison behavior. In particular, division or
remainder by zero is rejected because C defines it as undefined; qcc does not
assign it an arbitrary reversible result.

## Optimization and resource semantics

The optimizer levels are deliberately conservative:

| Level | Behavior |
| --- | --- |
| `O0` | Deterministic v0.3 Bennett compute-copy-uncompute baseline |
| `O1` | Defined constant folding, SSA simplification, dead-value cleanup, and safe local gate rewrites |
| `O2` | `O1` plus proven eager uncomputation and clean-ancilla reuse |

An optimization level selects an allowed pass set; it is not a performance
guarantee. `O2` may produce the same circuit as `O0` or `O1` when a rewrite or
cleanup proof is unavailable. Unsupported source constructs remain hard
diagnostics at every level.

Resource reports distinguish logical MCX operations from the primitive
`X`/`CX`/`CCX` decomposition. T counts are estimates using qcc's documented
seven-T-per-CCX convention; they are not hardware measurements.

Measured O0/O2 rows are available in
[docs/benchmarks.md](docs/benchmarks.md). The report generator records
host-dependent compile times and rewrites that generated document, while the
non-destructive smoke check is:

```console
$ python scripts/benchmarks.py --smoke
```

## Grover example

The repository includes a small oracle and a host-side demonstration. From the
checkout root:

```console
$ python examples/grover_demo.py --no-qiskit
```

The deterministic fallback finds the expected solution `[6]` for the checked-in
three-bit oracle. With the optional Qiskit path enabled, the demo can also use
statevector/Aer tooling; Qiskit is not required for compilation, OpenQASM
emission, or classical simulation.

See [examples/README.md](examples/README.md) for the other checked-in source
fixtures and command examples.

## Development

Install the development extras, then run the same checks used by CI:

```console
$ ruff check .
$ ruff format --check .
$ mypy --strict src
$ pytest -q
```

The GitHub Actions workflow tests Python 3.10, 3.11, and 3.12 and installs
Clang before running the quality gate. Optional Qiskit importer tests are
skipped when their external importer dependency is unavailable.

## Documentation

- [Supported C subset](docs/supported_c.md) — authoritative acceptance and
  diagnostic contract;
- [Architecture and invariants](docs/design.md) — frontend, lowering, layout,
  arithmetic, QASM, and optimizer design;
- [Measured benchmarks](docs/benchmarks.md) — generated O0/O2 resource rows;
- [Architecture decisions](docs/decisions.md) — non-obvious semantic and
  implementation decisions; and
- [Implementation plan](docs/plan.md) — milestone history and boundaries.

## License

qcc is distributed under the [MIT License](LICENSE).
