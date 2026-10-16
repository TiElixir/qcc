# Architecture decisions

## D-004 — integrate the five v0.1 subsystem implementations directly

- **Decision:** Keep the independently implemented frontend, netlist, lowering,
  and backend APIs as small adapters around one canonical `qcc.ir.netlist`
  representation, resolving name collisions in the public package rather than
  duplicating circuit implementations.
- **Alternatives:** Rewrite every subsystem into one monolithic implementation;
  retain the lowering branch's fallback-only circuit model.
- **Reason:** The canonical netlist already validates domains, layouts, gate
  operands, inversion, and resources. Adapters preserve those checks and keep
  the pipeline testable without making the frontend depend on backend details.

## D-005 — Clang textual LLVM is the v0.1 compiler boundary

- **Decision:** `qcc.compile_c` invokes the Clang wrapper, parses its textual
  LLVM result, and then selects and lowers the requested function.
- **Alternatives:** Parse C directly; call Clang through an LLVM Python binding;
  make callers provide LLVM IR.
- **Reason:** This satisfies the source-level API without introducing a C parser
  or a runtime LLVM dependency, while keeping the supported LLVM subset small
  and explicitly diagnosable.

This record captures the decisions that define the bootstrap and v0.1
boundary. The implementation plan in [plan.md](plan.md) is the source for the
milestones referenced below.

## D-001: Use a `src/` layout and Hatchling

**Status:** accepted

The distribution is named `qcc`, uses `src/qcc`, and is built with Hatchling.
The `src/` layout prevents an unchecked working-tree import from masking
packaging errors. Hatchling keeps the build backend small and declarative.
Python 3.10 is the minimum supported runtime, with CI covering 3.10 through
3.12.

## D-002: Keep the core runtime standard-library-only

**Status:** accepted

The compiler, circuit model, simulator, and emitter have no required runtime
dependencies beyond Python's standard library. Pytest, Hypothesis, Ruff, and
mypy are development extras. Qiskit is an explicitly optional extra for host
integration and examples, not a dependency of compilation.

## D-003: Let Clang front C and own the accepted LLVM parser

**Status:** accepted

qcc invokes Clang rather than maintaining a C parser or depending on private
Clang bindings. It owns a small textual LLVM IR parser so the accepted subset,
version-tolerant syntax, and diagnostics remain under project control. Parser
permissiveness applies to harmless attributes and naming variation only;
unsupported opcodes and types remain errors. This follows the repository plan's
frontend boundary and LLVM-version risk strategy.

## D-004: Start with a narrow reversible baseline

**Status:** accepted

v0.1 supports straight-line fixed-width integer functions and lowers to
X/CX/CCX gates. Bennett compute-copy-uncompute is the baseline cleanup
strategy. It makes temporary cleanup and correctness easy to inspect before
later optimizer work trades resources for fewer gates. Comparisons and
predication wait for v0.2 rather than expanding the first release implicitly.

## D-005: Make fixed-width behavior explicit

**Status:** accepted

Integer widths are part of the IR and arithmetic is modulo `2**width`. qcc
rejects or documents constructs whose C behavior depends on undefined or
implementation-defined behavior. Differential tests compare only defined
native-C inputs. This prevents a reversible circuit from appearing correct
because a host compiler happened to choose one undefined result.

## D-006: Make quality checks executable in CI

**Status:** accepted

Every supported Python version installs Clang and runs the project gate:
`ruff check .`, `ruff format --check .`, `mypy --strict src`, and `pytest -q`.
The configuration lives in `pyproject.toml`, and CI installs the same `.[dev]`
extra used by contributors. This keeps formatting, lint, types, and behavior
checks aligned.

## D-007: Keep bootstrap ownership boundaries explicit

**Status:** accepted

This bootstrap change adds package metadata and a `py.typed` marker but does
not create the public `src/qcc/__init__.py`. The lowering/integration owner
will add that public package surface along with the implementation modules.
The frontend, circuit, lowering, CLI, and tests are intentionally separate
work items so their commits can be reviewed and merged without a placeholder
API becoming a hidden contract.
