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

## Status: v0.2 specification in progress

The v0.1 straight-line integer boundary is the current implementation limit.
This checkout documents the v0.2 target—integer predicates, reversible
predication, SSA `phi` merges, constant shifts/rotates, and fully unrolled
constant-bound loops—but does not claim that those features are implemented
or that the v0.2 acceptance checks have passed.

Until the v0.2 implementation is complete, the practical supported subset is
the v0.1 straight-line subset: defined fixed-width integer constants, arguments,
add/subtract, bitwise operations, supported casts, and return, primarily with
`uint8_t`. The v0.2 additions above are target behavior rather than current
practical support. The deliberate limitations include no arbitrary C, pointers,
memory, floating point, I/O, recursion, variable shifts, runtime loops,
residual CFG cycles, or undefined-behavior inputs. Qiskit integration is
optional and is not a runtime dependency.

The `is_solution` example above remains the motivating interface for the
project. It requires the equality predicate, which is specified for v0.2 but
is not available in the current v0.1 implementation, so the snippet is a
future-facing API sketch rather than a runnable quickstart.

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
