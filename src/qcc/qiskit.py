"""Optional Qiskit adapter for host-side circuit experiments.

Importing :mod:`qcc.qiskit` never imports Qiskit.  The compiler and classical
simulator remain standard-library-only; callers opt into Qiskit by invoking one
of the functions in this module after installing the optional dependency.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from importlib import import_module, util
from typing import Any, cast


class QiskitUnavailableError(ImportError):
    """Raised when an optional Qiskit integration path is requested."""


def qiskit_available() -> bool:
    """Return whether the base Qiskit package can be found."""

    return util.find_spec("qiskit") is not None


def aer_available() -> bool:
    """Return whether the optional Qiskit Aer package can be found."""

    return util.find_spec("qiskit_aer") is not None


def _require_qiskit() -> Any:
    try:
        return import_module("qiskit")
    except ImportError as exc:
        raise QiskitUnavailableError(
            "Qiskit is optional; install the 'qiskit' extra to use this adapter"
        ) from exc


def _member(value: Any, names: Sequence[str], default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return default


def _operations(circuit: Any) -> Iterable[Any]:
    operations = _member(circuit, ("gates", "operations", "ops", "instructions"), ())
    if callable(operations):
        operations = operations()
    return cast(Iterable[Any], operations)


def _gate_parts(gate: Any) -> tuple[str, tuple[int, ...]]:
    name = _member(gate, ("op", "name", "opcode", "operation", "kind"))
    if name is None:
        name = str(gate).split("(", 1)[0]
    name = str(getattr(name, "name", name)).lower()
    name = name.replace("cnot", "cx").replace("toffoli", "ccx")

    qubits = _member(gate, ("qubits", "wires", "bits"))
    if qubits is None:
        controls = _member(gate, ("controls",), ())
        target = _member(gate, ("target",), None)
        qubits = tuple(controls) + ((target,) if target is not None else ())
    return name, tuple(int(qubit) for qubit in qubits)


def _num_qubits(circuit: Any) -> int:
    value = _member(circuit, ("num_qubits", "qubit_count", "n_qubits", "width"))
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError("the circuit must expose a non-negative num_qubits value")
    return value


def to_quantum_circuit(circuit: Any) -> Any:
    """Translate qcc X/CX/CCX/MCX gates to a Qiskit circuit.

    Wires retain their qcc integer indices.  Layout names are intentionally
    left to the host layer because the adapter is also useful for anonymous
    circuit fragments.
    """

    qiskit = _require_qiskit()
    quantum_circuit_type = getattr(qiskit, "QuantumCircuit", None)
    if quantum_circuit_type is None:
        raise QiskitUnavailableError(
            "the installed Qiskit package does not provide QuantumCircuit"
        )
    result = quantum_circuit_type(_num_qubits(circuit))
    for gate in _operations(circuit):
        name, qubits = _gate_parts(gate)
        if name == "x":
            if len(qubits) != 1:
                raise ValueError("X gates require one qubit")
            result.x(qubits[0])
        elif name == "cx":
            if len(qubits) != 2:
                raise ValueError("CX gates require two qubits")
            result.cx(qubits[0], qubits[1])
        elif name == "ccx":
            if len(qubits) != 3:
                raise ValueError("CCX gates require three qubits")
            result.ccx(qubits[0], qubits[1], qubits[2])
        elif name == "mcx":
            if len(qubits) < 2:
                raise ValueError("MCX gates require controls and a target")
            result.mcx(list(qubits[:-1]), qubits[-1])
        else:
            raise ValueError(f"unsupported qcc gate for Qiskit: {name!r}")
    return result


def statevector(circuit: Any) -> Any:
    """Run a Qiskit circuit with its exact statevector simulator."""

    _require_qiskit()
    try:
        quantum_info = import_module("qiskit.quantum_info")
        statevector_type = quantum_info.Statevector
    except (ImportError, AttributeError) as exc:
        raise QiskitUnavailableError(
            "the installed Qiskit package does not provide Statevector"
        ) from exc
    return statevector_type.from_instruction(circuit)


def marginal_probabilities(
    state: Any, qubits: Sequence[int], *, num_qubits: int | None = None
) -> dict[int, float]:
    """Aggregate a statevector over selected Qiskit qubit indices.

    Qiskit stores computational-basis amplitudes with qubit zero as the least
    significant bit, matching qcc's LSB-first register convention.
    """

    data = getattr(state, "data", state)
    amplitudes = tuple(data)
    if not amplitudes:
        raise ValueError("statevector data cannot be empty")
    inferred_qubits = (len(amplitudes) - 1).bit_length()
    width = inferred_qubits if num_qubits is None else num_qubits
    if len(amplitudes) != 1 << width:
        raise ValueError("statevector length does not match num_qubits")

    selected = tuple(int(qubit) for qubit in qubits)
    if len(set(selected)) != len(selected):
        raise ValueError("selected qubits must be distinct")
    if any(qubit < 0 or qubit >= width for qubit in selected):
        raise ValueError("selected qubit is outside the statevector width")

    probabilities: dict[int, float] = {}
    for basis, amplitude in enumerate(amplitudes):
        value = sum(
            ((basis >> qubit) & 1) << offset for offset, qubit in enumerate(selected)
        )
        probabilities[value] = probabilities.get(value, 0.0) + float(
            abs(amplitude) ** 2
        )
    return probabilities


def run_aer(circuit: Any, *, shots: int = 1024, seed: int | None = 7) -> dict[str, int]:
    """Sample a circuit with Qiskit Aer, when the separate Aer package exists."""

    if shots < 1:
        raise ValueError("shots must be positive")
    try:
        aer = import_module("qiskit_aer")
        simulator_type = aer.AerSimulator
    except (ImportError, AttributeError) as exc:
        raise QiskitUnavailableError(
            "Qiskit Aer is optional; install 'qiskit-aer' to run shot sampling"
        ) from exc

    measured = circuit.copy()
    measured.measure_all()
    backend = simulator_type()
    options: dict[str, Any] = {"shots": shots}
    if seed is not None:
        options["seed_simulator"] = seed
    counts = backend.run(measured, **options).result().get_counts()
    return {str(key): int(value) for key, value in counts.items()}


def counts_to_marginal_probabilities(
    counts: Mapping[str, int], qubits: Sequence[int], *, num_qubits: int
) -> dict[int, float]:
    """Convert Aer's classical bit strings into selected-qubit probabilities."""

    if num_qubits < 1:
        raise ValueError("num_qubits must be positive")
    total = sum(int(value) for value in counts.values())
    if total <= 0:
        raise ValueError("counts must contain at least one shot")
    selected = tuple(int(qubit) for qubit in qubits)
    if any(qubit < 0 or qubit >= num_qubits for qubit in selected):
        raise ValueError("selected qubit is outside the circuit width")

    probabilities: dict[int, float] = {}
    for raw_bits, count in counts.items():
        bits = str(raw_bits).replace(" ", "")
        if len(bits) != num_qubits:
            raise ValueError("Aer count width does not match num_qubits")
        basis = int(bits, 2)
        value = sum(
            ((basis >> qubit) & 1) << offset for offset, qubit in enumerate(selected)
        )
        probabilities[value] = probabilities.get(value, 0.0) + int(count) / total
    return probabilities


__all__ = [
    "QiskitUnavailableError",
    "aer_available",
    "counts_to_marginal_probabilities",
    "marginal_probabilities",
    "qiskit_available",
    "run_aer",
    "statevector",
    "to_quantum_circuit",
]
