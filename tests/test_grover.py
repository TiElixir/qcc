"""Host-layer checks for the optional Grover demonstration."""

from __future__ import annotations

import pytest
from examples import grover_demo

from qcc.ir.netlist import Circuit, Gate
from qcc.sim.classical import simulate


def test_non_qiskit_demo_smoke_uses_deterministic_fallback(capsys) -> None:
    result = grover_demo.run_demo(use_qiskit=False, shots=32)
    captured = capsys.readouterr().out

    assert result.backend == "classical"
    assert result.solutions == (6,)
    assert result.iterations == 2
    assert result.top_results[0][0] == 6
    assert "deterministic classical fallback" in captured
    assert "Verified top results against native brute force: yes" in captured


def test_qiskit_module_import_does_not_require_optional_dependency() -> None:
    import qcc.qiskit as qiskit_adapter

    assert callable(qiskit_adapter.qiskit_available)


def test_statevector_cross_check_for_small_primitive_circuit() -> None:
    pytest.importorskip("qiskit")
    from qcc.qiskit import marginal_probabilities, statevector, to_quantum_circuit

    circuit = Circuit(
        (
            Gate.x(0),
            Gate.cx(0, 1),
            Gate.ccx(0, 1, 2),
            Gate.mcx((0, 1, 2), 3),
        ),
        num_qubits=4,
    )
    translated = to_quantum_circuit(circuit)
    assert translated.num_qubits <= 16

    for initial in range(1 << circuit.num_qubits):
        basis = translated.copy()
        # Build the input basis state before the translated circuit.
        basis.clear()
        for qubit in range(circuit.num_qubits):
            if (initial >> qubit) & 1:
                basis.x(qubit)
        basis.compose(translated, inplace=True)
        probabilities = marginal_probabilities(
            statevector(basis), range(circuit.num_qubits), num_qubits=4
        )
        observed = max(probabilities, key=probabilities.__getitem__)
        assert observed == simulate(circuit, initial=initial, check_ancillas=False)


def test_statevector_cross_check_for_compiled_grover_oracle() -> None:
    pytest.importorskip("qiskit")
    from qcc.qiskit import marginal_probabilities, statevector

    compiled = grover_demo.compile_oracle()
    circuit = grover_demo.build_grover_circuit(
        compiled, grover_demo.iteration_count(8, 1)
    )
    assert circuit.num_qubits <= 16
    probabilities = marginal_probabilities(
        statevector(circuit),
        grover_demo.input_qubits(compiled),
        num_qubits=circuit.num_qubits,
    )
    assert max(probabilities, key=probabilities.__getitem__) == 6
