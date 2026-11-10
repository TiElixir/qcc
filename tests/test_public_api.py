"""Public convenience API coverage for the released qcc surface."""

from __future__ import annotations

from pathlib import Path

import pytest

import qcc
from qcc.sim.classical import AncillaDirtyError


def test_circuit_convenience_methods_cover_mcx_simulation_and_qasm() -> None:
    layout = qcc.CircuitLayout(
        {"input": (0, 1, 2), "output": (3,)}, ancillas=(4,), num_qubits=5
    )
    circuit = qcc.Circuit((qcc.Gate.mcx((0, 1, 2), 3),), num_qubits=5, layout=layout)

    assert qcc.simulate(circuit, {"input": 7}) == 1
    result = circuit.simulate({"input": 7})
    assert result["output"] == 1
    assert all(bit == 0 for bit in result["ancillas"].values())
    qasm = circuit.to_qasm3()
    assert qasm == qcc.to_qasm3(circuit)
    assert "ccx" in qasm
    assert "mcx" not in qasm


def test_public_simulation_checks_declared_clean_ancillas() -> None:
    layout = qcc.CircuitLayout({"output": (0,)}, ancillas=(1,), num_qubits=2)
    dirty = qcc.Circuit((qcc.Gate.x(1),), num_qubits=2, layout=layout)

    with pytest.raises(AncillaDirtyError, match="ancilla"):
        qcc.simulate(dirty)


def test_compile_c_accepts_source_keyword_and_rejects_unknown_opt_level() -> None:
    source = "unsigned char identity(unsigned char value) { return value; }\n"

    circuit = qcc.compile_c(source=source, function="identity", opt=0)
    assert circuit.layout is not None

    assert qcc.compile_c(source, function="identity", opt=3).layout is not None

    with pytest.raises(ValueError, match="between 0 and 3"):
        qcc.compile_c(source, function="identity", opt=4)


def test_optimized_reports_include_measured_level_snapshots() -> None:
    source = Path(__file__).parent / "fixtures" / "add.c"

    circuit = qcc.compile_c(source, function="add", opt=2)
    report = circuit.report()

    assert report.requested_level == 2
    assert report.effective_level == 2
    assert report.before is not None
    assert report.after is not None
    assert report.before["gate_count"] == 248
    assert report.after["gate_count"] == 128
    assert report.as_dict()["before"] == report.before
    assert report.as_dict()["after"] == report.after
