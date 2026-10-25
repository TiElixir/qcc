```c
#include <stdint.h>
#include <stdbool.h>

bool is_solution(uint8_t x) {
    return (uint8_t)(x * x + 3) % 17 == 5;
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

The Python `Circuit.report()` API also exposes post-decomposition Toffoli and
estimated T-count fields.

The resource command prints JSON with fields including `qubit_count`,
`ancillas`, `depth`, `gate_count`, and `gate_counts`. Exact counts are
schedule-dependent. The Python report also exposes the post-MCX-decomposition
CCX count and derived T-count estimate; v0.4 O0/O2 measurements are recorded in
`docs/benchmarks.md`.

The required predicate can be requested with the same CLI shape:

```console
$ qcc compile is_solution.c --function is_solution --emit qasm3 -o is_solution.qasm
$ qcc stats is_solution.c --function is_solution
```

The v0.3 backend decomposes intermediate MCX gates using declared clean
ancillas before emitting primitive QASM, and rejects circuits whose workspace
contract cannot be proven.

## Grover oracle fixture

`grover_oracle.c` contains a self-inverse three-bit bit-flip oracle:

```console
$ qcc compile examples/grover_oracle.c --function grover_oracle --emit qasm3 -o grover_oracle.qasm
$ qcc stats examples/grover_oracle.c --function grover_oracle
```

It marks `x == 6`, the solution of the motivating expression over `[0, 7]`.
`examples/grover_demo.py` runs the host-side phase-kickback/diffusion flow and
verifies the result against native brute force. Applying the generated oracle
twice restores the input and clean work registers.

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
