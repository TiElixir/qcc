"""Run the qcc Grover host demonstration.

The C source is compiled by qcc first.  Its clean reversible output is then
used as a bit-flip oracle with the output qubit prepared in ``|->``; phase
kickback turns that bit flip into the Grover phase oracle.  Qiskit is optional:
without it, the same native truth table is reported through a deterministic
classical fallback.
"""

from __future__ import annotations

import argparse
import ctypes
import math
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(__file__).with_name("grover_oracle.c")
FUNCTION = "grover_oracle"
MAX_STATEVECTOR_QUBITS = 16


@dataclass(frozen=True)
class DemoResult:
    """The verified host-level result of one Grover demo run."""

    backend: str
    domain_size: int
    solutions: tuple[int, ...]
    iterations: int
    top_results: tuple[tuple[int, float], ...]


def _import_qcc() -> Any:
    """Import qcc from an installed package or this checkout's ``src`` tree."""

    try:
        import qcc

        return qcc
    except ModuleNotFoundError as exc:
        if exc.name != "qcc":
            raise
        source_root = str(ROOT / "src")
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        import qcc

        return qcc


def _layout_register(circuit: Any, name: str) -> tuple[int, ...]:
    layout = getattr(circuit, "layout", None)
    if layout is None:
        raise ValueError("compiled oracle has no register layout")
    inputs = getattr(layout, "inputs", None)
    if isinstance(inputs, dict) and name in inputs:
        return tuple(int(qubit) for qubit in inputs[name])
    registers = getattr(layout, "registers", None)
    if isinstance(registers, dict) and name in registers:
        return tuple(int(qubit) for qubit in registers[name])
    register = getattr(layout, "register", None)
    if callable(register):
        try:
            return tuple(int(qubit) for qubit in register(name))
        except KeyError as exc:
            raise ValueError(f"compiled oracle has no {name!r} register") from exc
    raise ValueError(f"compiled oracle has no {name!r} register")


def input_qubits(circuit: Any) -> tuple[int, ...]:
    """Return the search register in qcc's little-endian order."""

    return _layout_register(circuit, "x")


def output_qubits(circuit: Any) -> tuple[int, ...]:
    """Return the compiled oracle's output register."""

    layout = getattr(circuit, "layout", None)
    output = getattr(layout, "output", None) if layout is not None else None
    if output is not None:
        return tuple(int(qubit) for qubit in output)
    return _layout_register(circuit, "output")


def compile_oracle(source: Path = SOURCE) -> Any:
    """Compile the example predicate through the public qcc pipeline."""

    qcc = _import_qcc()
    return qcc.compile_c(source, function=FUNCTION)


def _native_solutions(source: Path, width: int) -> tuple[int, ...]:
    """Compile the predicate natively and brute-force its bounded domain."""

    clang = shutil.which("clang")
    if clang is None:
        raise RuntimeError("clang is required to count native Grover solutions")

    with tempfile.TemporaryDirectory(prefix="qcc-grover-native-") as directory:
        source_copy = Path(directory) / source.name
        library_path = Path(directory) / "grover_oracle.so"
        source_copy.write_text(source.read_text())
        completed = subprocess.run(
            [
                clang,
                "-std=c11",
                "-O2",
                "-shared",
                "-fPIC",
                str(source_copy),
                "-o",
                str(library_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"native oracle compilation failed: {detail}")

        native_library = ctypes.CDLL(str(library_path))
        native_function = getattr(native_library, FUNCTION)
        native_function.argtypes = [ctypes.c_ubyte]
        native_function.restype = ctypes.c_ubyte
        return tuple(
            value for value in range(1 << width) if int(native_function(value)) != 0
        )


def iteration_count(domain_size: int, solution_count: int) -> int:
    """Choose the standard Grover iteration count from ``N`` and ``M``."""

    if domain_size < 1:
        raise ValueError("domain_size must be positive")
    if solution_count < 1 or solution_count > domain_size:
        raise ValueError("solution_count must be within the search domain")
    return max(1, math.floor(math.pi * math.sqrt(domain_size / solution_count) / 4))


def _diffusion(circuit: Any, search: Sequence[int]) -> None:
    if len(search) < 2:
        raise ValueError(
            "the Grover diffusion operator needs at least two search qubits"
        )
    for qubit in search:
        circuit.h(qubit)
        circuit.x(qubit)
    target = search[-1]
    circuit.h(target)
    circuit.mcx(list(search[:-1]), target)
    circuit.h(target)
    for qubit in search:
        circuit.x(qubit)
        circuit.h(qubit)


def build_grover_circuit(compiled_oracle: Any, iterations: int) -> Any:
    """Build a Qiskit Grover circuit around the compiled qcc oracle."""

    if iterations < 1:
        raise ValueError("iterations must be positive")
    from qcc.qiskit import to_quantum_circuit

    oracle = to_quantum_circuit(compiled_oracle)
    width = int(compiled_oracle.num_qubits)
    if width > MAX_STATEVECTOR_QUBITS:
        raise ValueError(
            f"statevector demo is limited to {MAX_STATEVECTOR_QUBITS} qubits; "
            f"compiled oracle uses {width}"
        )
    search = input_qubits(compiled_oracle)
    output = output_qubits(compiled_oracle)
    if not search or not output:
        raise ValueError("compiled oracle must expose search and output registers")

    circuit = oracle.copy()
    circuit.clear()
    for qubit in search:
        circuit.h(qubit)
    phase_qubit = output[0]
    circuit.x(phase_qubit)
    circuit.h(phase_qubit)
    for _ in range(iterations):
        circuit.compose(oracle, inplace=True)
        _diffusion(circuit, search)
    return circuit


def _top_results(
    probabilities: dict[int, float], top_k: int
) -> tuple[tuple[int, float], ...]:
    return tuple(
        sorted(probabilities.items(), key=lambda item: (-item[1], item[0]))[:top_k]
    )


def _verify_top_results(
    top_results: Sequence[tuple[int, float]], solutions: Sequence[int]
) -> None:
    expected = set(solutions)
    if not expected:
        raise ValueError("cannot verify results without native solutions")
    top_values = tuple(value for value, _ in top_results[: len(expected)])
    if set(top_values) != expected:
        raise AssertionError(
            f"top Grover results {top_values!r} do not match native solutions "
            f"{tuple(solutions)!r}"
        )


def _classical_fallback(
    domain_size: int, solutions: Sequence[int], top_k: int
) -> tuple[tuple[int, float], ...]:
    solution_set = set(solutions)
    probabilities = {
        value: (1.0 if value in solution_set else 0.0) for value in range(domain_size)
    }
    return _top_results(probabilities, top_k)


def _run_qiskit(
    compiled_oracle: Any, iterations: int, top_k: int, shots: int, seed: int
) -> tuple[tuple[tuple[int, float], ...], tuple[tuple[int, float], ...] | None]:
    from qcc.qiskit import (
        aer_available,
        counts_to_marginal_probabilities,
        marginal_probabilities,
        run_aer,
        statevector,
    )

    circuit = build_grover_circuit(compiled_oracle, iterations)
    search = input_qubits(compiled_oracle)
    state = statevector(circuit)
    state_probabilities = marginal_probabilities(
        state, search, num_qubits=circuit.num_qubits
    )
    state_top = _top_results(state_probabilities, top_k)

    aer_top: tuple[tuple[int, float], ...] | None = None
    if aer_available():
        counts = run_aer(circuit, shots=shots, seed=seed)
        aer_probabilities = counts_to_marginal_probabilities(
            counts, search, num_qubits=circuit.num_qubits
        )
        aer_top = _top_results(aer_probabilities, top_k)
    return state_top, aer_top


def _print_results(label: str, results: Sequence[tuple[int, float]]) -> None:
    print(label)
    for value, probability in results:
        print(f"  x={value}: {probability:.6f}")


def run_demo(
    *,
    use_qiskit: bool | None = None,
    shots: int = 1024,
    seed: int = 7,
    source: Path = SOURCE,
) -> DemoResult:
    """Run the demo and verify its leading results against native brute force.

    ``use_qiskit=False`` is useful for deterministic smoke tests even on a
    machine that has the optional dependency installed.
    """

    compiled_oracle = compile_oracle(source)
    search_width = len(input_qubits(compiled_oracle))
    solutions = _native_solutions(source, search_width)
    iterations = iteration_count(1 << search_width, len(solutions))
    top_k = min(5, 1 << search_width)

    print(f"Compiled {source} with qcc ({compiled_oracle.num_qubits} total qubits).")
    print(f"Native brute force found {len(solutions)} solution(s): {list(solutions)}")
    print(f"Grover iterations selected from N and M: {iterations}")

    from qcc.qiskit import qiskit_available

    should_use_qiskit = qiskit_available() if use_qiskit is None else use_qiskit
    if not should_use_qiskit:
        if use_qiskit is False:
            print("Qiskit path disabled; using deterministic classical fallback.")
        else:
            print(
                "Qiskit is an optional dependency and is not installed; "
                "using deterministic classical fallback."
            )
        top_results = _classical_fallback(1 << search_width, solutions, top_k)
        _verify_top_results(top_results, solutions)
        _print_results("Deterministic fallback top results:", top_results)
        print("Verified top results against native brute force: yes")
        return DemoResult(
            "classical", 1 << search_width, solutions, iterations, top_results
        )

    state_top, aer_top = _run_qiskit(compiled_oracle, iterations, top_k, shots, seed)
    _verify_top_results(state_top, solutions)
    _print_results("Qiskit statevector top results:", state_top)
    if aer_top is not None:
        _verify_top_results(aer_top, solutions)
        _print_results(f"Qiskit Aer top results ({shots} shots):", aer_top)
    else:
        print("Qiskit Aer is not installed; skipped optional shot sampling.")
    print("Verified top results against native brute force: yes")
    return DemoResult("qiskit", 1 << search_width, solutions, iterations, state_top)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-qiskit",
        action="store_true",
        help="force the deterministic fallback even when Qiskit is installed",
    )
    parser.add_argument(
        "--shots",
        type=int,
        default=1024,
        help="Aer shots when qiskit-aer is installed (default: 1024)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=7,
        help="deterministic Aer seed (default: 7)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run_demo(
        use_qiskit=False if args.no_qiskit else None, shots=args.shots, seed=args.seed
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
