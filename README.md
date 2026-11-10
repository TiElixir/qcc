# qcc

[![Status: Beta](https://img.shields.io/badge/status-beta-f59e0b.svg)](pyproject.toml)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**qcc** compiles pure C functions into reversible quantum circuits. Clang
turns the C into LLVM IR, qcc validates that IR with its own parser, and the
result is an `X`/`CX`/`CCX` network that computes the function into a clean
output register while leaving its inputs untouched. The circuit can be
simulated, costed, checked against the C semantics on every input, and emitted
as OpenQASM 3.

```c
#include <stdint.h>
#include <stdbool.h>

bool is_solution(uint8_t x) {
    return (uint8_t)(x * x + 3) % 17 == 5;
}
```

```console
$ qcc verify examples/benchmarks.c --function is_solution
ok: is_solution: verified on all 256 inputs [O3: 43 qubits, 840 gates, T estimate 2401]
```

That oracle takes 209 qubits and a T estimate of 66,038 at the baseline level.
The numbers for every benchmark are measured, not estimated, and regenerated
by a script:

| Function | Qubits (O0 → O3) | T estimate (O0 → O3) | Reduction |
| --- | ---: | ---: | ---: |
| `add` | 41 → 25 | 448 → 98 | 4.6x |
| `multiply` | 97 → 33 | 35,840 → 1,281 | 28.0x |
| `compare` | 49 → 25 | 504 → 112 | 4.5x |
| `popcount` | 303 → 50 | 3,136 → 686 | 4.6x |
| `is_solution` | 209 → 43 | 66,038 → 2,401 | 27.5x |

Full rows, including gate counts, depth, and the intermediate `O2` level, are
in [docs/benchmarks.md](docs/benchmarks.md).

## How it works

```text
C source
  → Clang textual LLVM IR
  → validated qcc SSA           (calls inlined, intrinsics expanded)
  → circuit synthesis           (O0–O2: per-instruction Bennett lowering
                                 O3:    whole-function dataflow synthesis)
  → X/CX/CCX circuit
  → simulate · verify · resource report · OpenQASM 3
```

Every arrow is a validation boundary. A construct qcc cannot compile is a
hard, located diagnostic; it is never approximated.

## Requirements

- Python 3.10 or newer
- Clang available as `clang` on `PATH`

The runtime has no third-party Python dependencies. Qiskit is optional and
only needed for the Qiskit adapter and the Grover demo's statevector path.

## Installation

From a repository checkout:

```console
$ python -m pip install -e ".[dev]"
```

On Debian or Ubuntu, install Clang separately if it is missing:

```console
$ sudo apt-get install --no-install-recommends -y clang
```

Optional Qiskit adapter:

```console
$ python -m pip install -e ".[qiskit]"
```

## Quickstart

```console
$ qcc check examples/add.c --function add
ok: examples/add.c (add)

$ qcc verify examples/add.c --function add
ok: add: verified on all 65536 inputs [O3: 25 qubits, 49 gates, T estimate 98]

$ qcc compile examples/add.c --function add --opt O3 --emit qasm3 -o add.qasm
$ qcc stats examples/add.c --function add --opt O3
```

The OpenQASM 3 output uses named registers and only the primitive gates:

```qasm
OPENQASM 3.0;
include "stdgates.inc";

qubit[8] a;
qubit[8] b;
qubit[8] output;
qubit[1] ancilla;

cx a[0], output[0];
```

| Command | Purpose |
| --- | --- |
| `qcc check` | Compile and validate; `--dump-ir` prints the LLVM IR Clang produced |
| `qcc compile` | Emit the circuit (`--emit qasm3`) |
| `qcc stats` | Print the JSON resource report |
| `qcc verify` | Compile, then check the circuit against the C function's semantics |

`compile`, `stats`, and `check` default to `--opt O0`, the stable baseline
whose resource numbers are comparable across releases. `verify` defaults to
`O3`. Pass `--opt O3` when you want the small circuit.

## Verification

`qcc verify` (and `qcc.verify.verify` in Python) simulates the compiled
circuit against a reference evaluation of the parsed function and checks
three things for each input:

1. the output register holds the function's value,
2. every input register still holds its argument, and
3. every work qubit is back in `|0⟩`.

All inputs are swept when the arguments total at most 20 bits (configurable
with `--exhaustive-bits`); larger functions fall back to a seeded random
sample, and the report says which happened. A failure is a concrete
counterexample:

```python
import qcc
from qcc.verify import verify

function = qcc.parse_c("examples/add.c", "add")
circuit = qcc.compile_c(function, function="add", opt=3)
result = verify(circuit, function)
assert result.ok and result.exhaustive and result.vectors == 65536
```

The sweep is bit-sliced: one Python integer carries a wire's value across
thousands of inputs, so 65,536 inputs take well under a second.

`scripts/fuzz.py` applies the same check to random programs. Each seed builds
a random SSA function (arithmetic, comparisons, casts, constant division,
nested `if`/`else` with early returns), compiles it, and verifies it on every
input:

```console
$ python scripts/fuzz.py --count 2000 --seed 1
2000 random functions verified exhaustively at O3 (seeds 1..2000)
```

## Python API

```python
import qcc

source = """
#include <stdint.h>
uint8_t add(uint8_t a, uint8_t b) { return a + b; }
"""

circuit = qcc.compile_c(source=source, function="add", opt=3)
report = circuit.report()
qasm = circuit.to_qasm3()

# qcc.simulate() returns the output-register value.
output = qcc.simulate(circuit, {"a": 5, "b": 7})

# Circuit.simulate() returns named registers and declared ancillas.
state = circuit.simulate({"a": 5, "b": 7})
assert state["output"] == output == 12
```

Public entry points:

- `qcc.compile_c(...)` — compile C source text, a source path, or a parsed
  function;
- `qcc.parse_c(source_or_path, function)` — the validated SSA function, with
  calls inlined and intrinsics expanded;
- `qcc.verify.verify(...)` / `assert_verified(...)` — check a circuit against
  the function it was compiled from;
- `Circuit.report()` — resource counts and optimizer metadata;
- `Circuit.to_qasm3()` / `qcc.to_qasm3(...)` — validated OpenQASM 3;
- `Circuit.to_qiskit()` / `qcc.to_qiskit(...)` — the optional Qiskit adapter;
- `qcc.simulate(...)` — the standard-library classical simulator.

Registers are LSB-first. Declared ancillas must finish in `0`; the simulator
checks this by default. The Python API defaults to `opt=1`.

## Supported C subset

The authoritative contract is [docs/supported_c.md](docs/supported_c.md).
In summary, qcc accepts defined pure functions with fixed-width integer
arguments and one fixed-width integer result, including:

- addition, subtraction, multiplication, bitwise operations, and casts;
- every integer comparison, signed and unsigned;
- `?:`, nested `if`/`else`, and early returns;
- shifts and rotates by a constant or by a runtime amount;
- compile-time bounded loops (fully unrolled by Clang);
- unsigned division and remainder by a nonzero constant;
- calls to other functions defined in the same file (inlined); and
- the integer builtins Clang lowers to intrinsics: min/max, `abs`,
  `__builtin_popcount`, `__builtin_clz`/`ctz`, `__builtin_bswap`, bit
  reversal, rotates, and saturating add/subtract.

It rejects pointers, arrays, memory, globals, floating point, I/O, recursion,
external calls, function pointers, inline assembly, `switch`, runtime loops,
runtime divisors, signed division, and undefined behavior. One unsupported
function does not poison its file: the other functions in it still compile,
and the diagnostic for the rejected one names the construct and its source
line.

```console
$ qcc check tests/fixtures/v05_mixed.c --function factorial
qcc: error: tests/fixtures/v05_mixed.c:17:26: error [unsupported-construct]: recursive call @factorial -> @factorial; recursion is not supported because every function call is inlined into one fixed-size circuit
```

## Optimization levels

| Level | Behavior |
| --- | --- |
| `O0` | Bennett compute-copy-uncompute baseline: one register per SSA value |
| `O1` | Reserved; currently produces the `O0` circuit |
| `O2` | `O0` plus SSA simplification, gate cancellation, eager uncomputation, and clean-ancilla reuse |
| `O3` | Whole-function dataflow synthesis (below) |

`O3` does not lower instruction by instruction. It turns the function into
one hash-consed expression graph and synthesizes that:

- **Free wiring.** Constants, shifts, casts, masks, bitwise NOT, and byte
  swaps relabel wires instead of emitting gates. A function made only of
  these compiles to `CX` copies with no Toffoli and no work qubit.
- **If-conversion.** Branches become a select tree shaped like the source's
  control flow, so no path-condition logic is synthesized.
- **In-place arithmetic.** Adders and comparators are Cuccaro-style ripple
  circuits that borrow the addend's own wires for carries: one ancilla for
  an n-bit add instead of n. Multiplication is shift-and-add over controlled
  adders; constant division is in-place restoring division.
- **Direct output.** The returned value is computed straight into the output
  register, so only intermediate values pay Bennett's factor of two.

All levels obey the same contract: correct output, preserved inputs, clean
work qubits. A level selects a strategy; it is not a guarantee that a
particular circuit shrinks. `O3` requires a parsed C function; the legacy
structural-SSA dictionary input compiles at `O2` and the report says so.

Resource reports distinguish logical MCX operations from the primitive
`X`/`CX`/`CCX` decomposition. T counts use the seven-T-per-CCX convention and
are estimates, not hardware measurements. An `O3` report includes the `O0`
circuit's figures as its `before` snapshot.

```console
$ python scripts/benchmarks.py           # regenerate docs/benchmarks.md
$ python scripts/benchmarks.py --smoke   # non-destructive check
```

## Grover example

```console
$ python examples/grover_demo.py --no-qiskit
```

The deterministic fallback finds the expected solution `[6]` for the
checked-in three-bit oracle. With Qiskit installed the demo can also use
statevector/Aer tooling. See [examples/README.md](examples/README.md) for the
other fixtures.

## Development

```console
$ ruff check .
$ ruff format --check .
$ mypy --strict src
$ pytest -q
```

CI runs this gate on Python 3.10 through 3.14 with Clang installed. Tests
that need Qiskit's OpenQASM 3 importer are skipped when it is unavailable.

## Documentation

- [Supported C subset](docs/supported_c.md) — acceptance and diagnostic
  contract;
- [Architecture and invariants](docs/design.md) — frontend, lowering,
  synthesis, layout, QASM, and optimizer design;
- [Measured benchmarks](docs/benchmarks.md) — generated resource rows;
- [Architecture decisions](docs/decisions.md) — non-obvious semantic and
  implementation decisions;
- [Implementation plan](docs/plan.md) — milestone history and boundaries;
- [Changelog](CHANGELOG.md).

## License

qcc is distributed under the [MIT License](LICENSE).
