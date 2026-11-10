# Changelog

All notable changes to qcc are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); before 1.0, a minor version may
change behavior.

## 0.5.0 — unreleased

### Added

- **Optimization level O3**, a whole-function dataflow synthesizer
  (`qcc.synth`). Wiring-only operations emit no gates, branches are
  if-converted, arithmetic is computed in place with Cuccaro-style adders,
  and the result is produced directly in the output register. Measured
  against O0 on `examples/benchmarks.c`:

  | Function | Qubits | T estimate |
  | --- | ---: | ---: |
  | `add` | 41 → 25 | 448 → 98 |
  | `multiply` | 97 → 33 | 35840 → 1281 |
  | `compare` | 49 → 25 | 504 → 112 |
  | `popcount` | 303 → 50 | 3136 → 686 |
  | `is_solution` | 209 → 43 | 66038 → 2401 |

- **`qcc verify`** and the `qcc.verify` module: check a compiled circuit
  against the C function's semantics on every input (up to 20 input bits) or
  a seeded sample, covering output value, input preservation, and clean work
  qubits. Failures report a concrete counterexample.
- `qcc.sim.bitsliced`: a simulator that evaluates many inputs per gate.
- `qcc.parse_c(source_or_path, function)`: the validated SSA function.
- **Function calls** to functions defined in the same file, by inlining.
- **Integer intrinsics**: `llvm.umin/umax/smin/smax`, `abs`, `ctpop`,
  `ctlz`, `cttz`, `bswap`, `bitreverse`, `fshl`, `fshr`, `uadd.sat`,
  `usub.sat`.
- **Shifts and power-of-two-width rotates by a runtime amount**, as barrel
  shifters.
- `scripts/fuzz.py`: a differential fuzzer that exhaustively verifies random
  SSA functions with nested branches and early returns.
- `--opt O3` on every CLI command; `MAX_LEVEL` is now 3.

### Changed

- A file is validated per function. One unsupported function no longer
  rejects the others in its file, and an unknown function name is an
  `unknown-function` error that lists the available names.
- A pointer parameter is reported at its first dereference, with a source
  line, instead of at the function signature.
- `--dump-ir` prints the LLVM IR produced by Clang.
- `scripts/benchmarks.py` measures O0, O2, and O3 and adds a summary table.
- When `opt=3` is requested for structural SSA input, the circuit is built
  at level 2 and the report shows requested 3, effective 2.

### Fixed

- Compatibility with current Clang (tested with 22.1): `or disjoint`,
  `freeze`, opaque-pointer parameters, and intrinsic calls are parsed.
  Previously most of the test suite failed on a current toolchain.
- Comparing a value with itself (`icmp` with identical operands) raised
  `GateError: ccx cannot reuse a qubit` in the O0–O2 lowering.

### Known limitations

- O1 emits the same circuit as O0.
- `switch`, recursion, external calls, pointers, arrays, runtime divisors,
  and signed division remain unsupported.

## 0.4.0

Optimizer levels O0–O2, eager uncomputation, clean-ancilla reuse, and
measured before/after resource reports.

## 0.3.0

Multiplication, constant division and remainder, MCX decomposition, named
OpenQASM 3 registers, the optional Qiskit adapter, and the Grover example.

## 0.2.0

Comparisons, `select`, nested `if`/`else`, constant shifts, and bounded
loops.

## 0.1.0

Straight-line `uint8_t` arithmetic compiled to X/CX/CCX with Bennett
cleanup, a classical simulator, and OpenQASM 3 output.
