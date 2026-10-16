"""Deterministic resource-report coverage for v0.3 circuits."""

from __future__ import annotations

from qcc import CircuitBuilder
from qcc.backend.report import report, report_resources, resource_report


def test_resource_report_matches_primitive_circuit_counts_and_depth() -> None:
    circuit = CircuitBuilder(4).x(0).cx(0, 1).ccx(0, 1, 2).build()

    result = resource_report(circuit)

    assert result.as_dict() == {
        "qubits": 4,
        "ancillas": 0,
        "gates": 3,
        "x": 1,
        "ccx": 1,
        "cx": 1,
        "depth": 3,
    }
    assert result.counts == {"x": 1, "cx": 1, "ccx": 1}
    assert result.gate_counts == result.counts
    assert result.num_qubits == 4
    assert result.gate_count == 3
    assert result.ancilla_count == 0
    assert report(circuit) == result
    assert report_resources(circuit) == result


def test_resource_report_counts_multi_controlled_intermediate_gates() -> None:
    circuit = CircuitBuilder(5).mcx((0, 1, 2, 3), 4).build()

    result = resource_report(circuit)

    assert result.gates == 1
    assert result.depth == 1
    assert result.as_dict()["mcx"] == 1
    assert result.gate_counts["mcx"] == 1
