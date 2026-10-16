```c
#include <stdint.h>

uint8_t is_solution(uint8_t x) {
    return x == 42;
}
```

# qcc

`qcc` is a small, deliberately bounded compiler for turning a documented
subset of pure C into reversible quantum circuits. It asks Clang for textual
LLVM IR, validates the supported operations, lowers them to reversible
X/CX/CCX networks, and can emit OpenQASM 3. The project is designed to make
its boundaries and resource costs explicit rather than to behave like a
general-purpose C compiler.

## Status: v0.3 specification in progress

The v0.2 compiler is the implementation baseline in this checkout. This
documentation branch specifies the v0.3 target: fixed-width multiplication,
constant-divisor restoring division/remainder, deterministic MCX decomposition,
named OpenQASM 3 register mapping, resource and T-count estimates, and a
Grover-oracle integration path. This branch does not claim that v0.3 has been
implemented, that its acceptance checks have passed, or that its resource
figures have been measured.

The practical boundary remains a deliberately small pure-C subset: defined
fixed-width integer values, arguments, arithmetic and predicates covered by
the current implementation, reversible acyclic control flow, constant shifts,
and constant-bound loops after unrolling. It does not include arbitrary C,
pointers, memory, floating point, I/O, recursion, variable shifts, runtime
loops, residual CFG cycles, runtime divisors, signed division/remainder, or
undefined-behavior inputs. Qiskit is optional and is not a core runtime
dependency.

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

The v0.3 target maps the result to named registers and emits primitive
OpenQASM 3. The current v0.2 baseline can report the circuit, but its
equality lowering may still contain an intermediate MCX; primitive QASM
emission therefore remains a v0.3 acceptance item until decomposition is
implemented and verified.

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

On the current v0.2 baseline, the same `add` command returns an illustrative
resource report like this:

```json
{
  "ancillas": 17,
  "ccx": 64,
  "cx": 184,
  "depth": 170,
  "gate_count": 248,
  "gate_counts": {
    "ccx": 64,
    "cx": 184
  },
  "gates": 248,
  "mcx": 0,
  "optimizer": null,
  "qubit_count": 41,
  "qubits": 41,
  "x": 0
}
```

This is current-baseline output, not a v0.3 acceptance result. v0.3 adds
explicit logical, post-decomposition primitive, and T-estimate fields under
the conventions in [design.md](docs/design.md).

`qcc stats` reports JSON fields such as `qubit_count`, `ancillas`, `depth`,
`gate_count`, and `gate_counts` for `x`, `cx`, `ccx`, and any not-yet-expanded
`mcx` operations. v0.3 resource conventions also define a post-decomposition
CCX count and a derived T-count estimate; these are documented in
[design.md](docs/design.md) and are not asserted by this docs-only branch.

## Grover demo behavior

`examples/grover_oracle.c` is a reversible bit-flip fixture:

```console
$ qcc compile examples/grover_oracle.c --function grover_oracle --emit qasm3 -o grover_oracle.qasm
$ qcc stats examples/grover_oracle.c --function grover_oracle
```

It XORs `0x2a` into the output register. Applying the resulting circuit twice
restores the input and clean work registers, so a host program can use it as
an oracle component. It is not a complete Grover search: qcc does not prepare
superposition, apply the diffusion operator, measure, or run amplitude
amplification. The `is_solution` predicate above is the intended one-bit
solution interface for a future phase-kickback/Grover host. Qiskit examples
may be installed with the optional extra, but Qiskit is not required to
compile, simulate, validate, or emit qcc circuits.

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
