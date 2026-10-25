```c
#include <stdint.h>
#include <stdbool.h>

bool is_solution(uint8_t x) {
    return (uint8_t)(x * x + 3) % 17 == 5;
}
```

# qcc

`qcc` is a small, deliberately bounded compiler for turning a documented
subset of pure C into reversible quantum circuits. It asks Clang for textual
LLVM IR, validates the supported operations, lowers them to reversible
X/CX/CCX networks, and can emit OpenQASM 3. The project is designed to make
its boundaries and resource costs explicit rather than to behave like a
general-purpose C compiler.

## Status: v0.4 complete; v0.4.0 tagged

v0.3 is complete and tagged `v0.3.0`, and the v0.4 milestone is tagged
`v0.4.0`. This checkout adds v0.4 optimizer levels
O0/O1/O2, semantics-preserving SSA and peephole passes, lifetime-based eager
cleanup, conservative clean-ancilla reuse, invariant tests, and measured
benchmark rows in [docs/benchmarks.md](docs/benchmarks.md). O0 remains the
compatibility baseline; O2 reductions are observed measurements, not promises
about other source programs or hardware.

The practical boundary remains a deliberately small pure-C subset: defined
fixed-width integer values, arguments, arithmetic and predicates covered by
the current implementation, reversible acyclic control flow, constant shifts,
constant-bound loops after unrolling, and constant-divisor `udiv`/`urem`. It
does not include arbitrary C, pointers, memory, floating point, I/O, recursion,
variable shifts, runtime loops, residual CFG cycles, runtime divisors, signed
division/remainder, or undefined-behavior inputs. Qiskit is optional and is not
a core runtime dependency.

## Install and quickstart

Use Python 3.10+ and install the development extras:

```console
$ python -m pip install -e ".[dev]"
```

Clang must be available on `PATH`. Save the `is_solution` function above as
`is_solution.c`, then use the CLI to request OpenQASM 3 or a resource report:

```console
$ qcc compile is_solution.c --function is_solution --emit qasm3 -o is_solution.qasm
$ qcc stats is_solution.c --function is_solution
$ qcc check is_solution.c --function is_solution
```

The v0.3 backend maps the result to named registers and decomposes MCX gates
using declared clean ancillas before emitting primitive OpenQASM 3.

For a currently runnable primitive-QASM smoke test, compile the checked-in
modular adder:

```console
$ qcc compile examples/add.c --function add --emit qasm3 -o add.qasm
$ qcc stats examples/add.c --function add
```

The generated file begins with the following OpenQASM 3 shape; the complete
gate list and counts depend on the selected lowering schedule:

```qasm
OPENQASM 3.0;
include "stdgates.inc";

qubit[8] a;
qubit[8] b;
qubit[8] output;
qubit[17] ancilla;
```

The same `add` command returns a resource report like this:

```json
{
  "after": {
    "ancillas": 17,
    "depth": 170,
    "gate_count": 248,
    "gate_counts": {
      "ccx": 64,
      "cx": 184,
      "x": 0
    },
    "qubit_count": 41,
    "t_count": 448
  },
  "ancillas": 17,
  "ccx": 64,
  "cx": 184,
  "depth": 170,
  "effective_level": 0,
  "gate_count": 248,
  "gate_counts": {
    "ccx": 64,
    "cx": 184
  },
  "gates": 248,
  "mcx": 0,
  "mcx_cx": 0,
  "optimizer": 0,
  "primitive_gates": 248,
  "qubit_count": 41,
  "qubits": 41,
  "requested_level": 0,
  "before": {
    "ancillas": 17,
    "depth": 170,
    "gate_count": 248,
    "gate_counts": {
      "ccx": 64,
      "cx": 184,
      "x": 0
    },
    "qubit_count": 41,
    "t_count": 448
  },
  "t_count": 448,
  "toffoli_estimate": 64,
  "x": 0
}
```

The Python API exposes the same report and convenience adapters:

```python
import qcc

circuit = qcc.compile_c(source="...", function="is_solution", opt=2)
report = circuit.report()
qasm = circuit.to_qasm3()
result = circuit.simulate({"x": 5})
```

`result` is a mapping of named registers plus an `ancillas` qubit-to-bit
mapping. `circuit.to_qiskit()` is available when the optional Qiskit extra is
installed. The Python report additionally exposes the post-decomposition
Toffoli count and estimated T count.

`qcc stats` reports JSON fields such as `qubit_count`, `ancillas`, `depth`,
`gate_count`, and `gate_counts` for `x`, `cx`, `ccx`, and any not-yet-expanded
`mcx` operations. The Python report object provides the selected optimizer
level, post-decomposition CCX count, and derived T-count estimate. Compare O0
and O2 rows in `docs/benchmarks.md` for measured before/after snapshots.

## Grover demo behavior

`examples/grover_oracle.c` is a small reversible bit-flip fixture for the
three-bit equivalent search predicate:

```console
$ qcc compile examples/grover_oracle.c --function grover_oracle --emit qasm3 -o grover_oracle.qasm
$ qcc stats examples/grover_oracle.c --function grover_oracle
```

It has one solution, `x == 6`, matching the motivating expression on the
three-bit domain. `examples/grover_demo.py` compiles the oracle, derives a
phase oracle using an output qubit in `|->`, chooses the iteration count from
the native solution count, and verifies the top result. With Qiskit installed
it runs statevector/Aer; otherwise it uses a deterministic classical fallback.

## Project documents

- [Implementation plan](docs/plan.md)
- [Architecture and invariants](docs/design.md)
- [Supported C subset](docs/supported_c.md)
- [Architecture decisions](docs/decisions.md)

## Development checks

The project quality gate is:

```console
$ ruff check .
$ ruff format --check .
$ mypy --strict src
$ pytest -q
```

## License

qcc is distributed under the MIT License; see [LICENSE](LICENSE).
