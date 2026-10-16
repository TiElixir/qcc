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

## Status: v0.1 pre-release

The v0.1 milestone is under active construction. This bootstrap establishes
the package contract, design documentation, and development checks; the
frontend, circuit representation, lowering pipeline, and command-line
implementation land separately. Until those components are present, this
checkout is not a runnable compiler.

The v0.1 target is intentionally narrow:

- Python 3.10 or newer at runtime.
- Clang is the only required C frontend; qcc does not parse C source itself.
- Straight-line functions over fixed-width unsigned integers, primarily
  `uint8_t`, with the operations listed in [the supported-C contract](docs/supported_c.md).
- Deterministic fixed-width arithmetic with reversible compute-copy-uncompute
  scheduling and an OpenQASM 3 output path.

The `is_solution` example above is the motivating interface for the project,
but equality predicates are scheduled for v0.2. v0.1 does not promise support
for arbitrary C, pointers, memory, floating point, I/O, recursion, surviving
control-flow cycles, or undefined-behavior inputs. Qiskit integration is
optional and is not a runtime dependency.

## Development setup

Create an environment with Python 3.10+ and install the development extras:

```text
python -m pip install -e ".[dev]"
```

Install the optional host-integration dependency only when needed:

```text
python -m pip install -e ".[qiskit]"
```

The project quality gate is:

```text
ruff check .
ruff format --check .
mypy --strict src
pytest -q
```

Clang must also be available for compiler integration and end-to-end tests.

## Project documents

- [Implementation plan](docs/plan.md)
- [Architecture and invariants](docs/design.md)
- [Supported C subset](docs/supported_c.md)
- [Architecture decisions](docs/decisions.md)

## License

qcc is distributed under the MIT License; see [LICENSE](LICENSE).
