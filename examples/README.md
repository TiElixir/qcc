```c
#include <stdint.h>

uint8_t is_solution(uint8_t x) {
    return x == 42;
}
```

# qcc examples

These examples use the deliberately small fixed-width C subset accepted by
qcc. The compiler treats `uint8_t` arithmetic modulo 256 and lowers supported
expressions to reversible `X`, `CX`, and `CCX` gates. The `is_solution` snippet
above is the required one-bit solution interface; save it as `is_solution.c`
when experimenting with the predicate path.

## Install and quickstart

From the repository root, install the development extras in a Python 3.10+
environment:

```console
$ python -m pip install -e ".[dev]"
```

Compile the modular adder, emit OpenQASM 3, and inspect resources:

```console
$ qcc compile examples/add.c --function add --emit qasm3 -o add.qasm
$ qcc stats examples/add.c --function add
$ qcc check examples/add.c --function add
```

The QASM file has named registers and starts in this form:

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
the conventions in [design.md](../docs/design.md).

The resource command prints JSON with fields including `qubit_count`,
`ancillas`, `depth`, `gate_count`, and `gate_counts`. Exact counts are
schedule-dependent. v0.3 also defines the post-MCX-decomposition CCX count
and a derived T-count estimate; this documentation does not claim those
v0.3 results have been produced in the current checkout.

The required predicate can be requested with the same CLI shape:

```console
$ qcc compile is_solution.c --function is_solution --emit qasm3 -o is_solution.qasm
$ qcc stats is_solution.c --function is_solution
```

The v0.2 baseline may report an intermediate MCX for equality and may reject
primitive QASM emission until the v0.3 clean-ancilla decomposition is in
place. That limitation is intentional and is part of the v0.3 acceptance
work, rather than a reason to silently approximate the predicate.

## Grover oracle fixture

`grover_oracle.c` contains a self-inverse bit-flip oracle:

```console
$ qcc compile examples/grover_oracle.c --function grover_oracle --emit qasm3 -o grover_oracle.qasm
$ qcc stats examples/grover_oracle.c --function grover_oracle
```

It XORs the fixed mask `0x2a` into the output register. Applying the generated
circuit twice restores the input and clean work registers. The fixture is an
oracle component for a host program with its own work/phase register; it does
not by itself prepare a superposition, run the diffusion operator, measure, or
perform a complete Grover search. The `is_solution` function is the intended
one-bit predicate for the future phase-kickback path.

Qiskit can be installed for host-side experiments:

```console
$ python -m pip install -e ".[qiskit]"
```

It remains optional; qcc's compiler, simulator, resource report, QASM emitter,
and validation path do not require it.

## Source and diagnostics

The examples are source inputs, not standalone executables; qcc invokes Clang
and compiles the selected function. Use `--dump-ir` to inspect the textual
LLVM IR received by the lowering pipeline. Unsupported C, runtime divisors,
signed division/remainder, pointers, memory, floating point, I/O, and
undefined behavior remain outside the documented subset.
