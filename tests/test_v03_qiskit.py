"""Optional OpenQASM 3 statevector integration tests."""

from __future__ import annotations

import pytest

from qcc import CircuitBuilder
from qcc.backend.qasm3 import emit_qasm3

pytest.importorskip("qiskit", reason="optional Qiskit integration")
try:
    from qiskit import qasm3
    from qiskit.quantum_info import Statevector
except ImportError as error:  # pragma: no cover - depends on optional install
    pytest.skip(
        f"Qiskit OpenQASM 3 support is unavailable: {error}",
        allow_module_level=True,
    )


def _load_qasm(source: str):
    try:
        return qasm3.loads(source)
    except ImportError as error:  # pragma: no cover - optional parser package
        pytest.skip(f"Qiskit OpenQASM 3 importer is unavailable: {error}")


def test_qasm3_primitive_circuit_matches_qiskit_statevector() -> None:
    circuit = CircuitBuilder(3).x(0).cx(0, 1).ccx(0, 1, 2).build()
    qiskit_circuit = _load_qasm(emit_qasm3(circuit))

    state = Statevector.from_instruction(qiskit_circuit)
    probabilities = state.probabilities()
    support = [
        index for index, probability in enumerate(probabilities) if probability > 1e-12
    ]

    assert support == [7]
    assert probabilities[7] == pytest.approx(1.0)


def test_qasm3_can_be_loaded_and_evolved_from_a_basis_state() -> None:
    circuit = CircuitBuilder(2).x(0).cx(0, 1).build()
    qiskit_circuit = _load_qasm(emit_qasm3(circuit))

    state = Statevector.from_int(0, 2**qiskit_circuit.num_qubits).evolve(qiskit_circuit)
    probabilities = state.probabilities()
    support = [
        index for index, probability in enumerate(probabilities) if probability > 1e-12
    ]

    assert support == [3]
    assert probabilities[3] == pytest.approx(1.0)
