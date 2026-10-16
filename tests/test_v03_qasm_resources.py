"""v0.3 OpenQASM 3 decomposition and resource-report coverage."""

from __future__ import annotations

import re
from collections.abc import Iterable

import pytest

import qcc
from qcc import Circuit, CircuitLayout, Gate
from qcc.backend.qasm3 import decompose_mcx, emit_qasm3, validate_qasm3
from qcc.backend.report import resource_report
from qcc.sim.classical import assign_inputs, simulate

_DECLARATION = re.compile(r"^qubit(?:\[(?P<size>[0-9]+)\])? (?P<name>\w+);$")
_INSTRUCTION = re.compile(r"^(?P<name>x|cx|ccx) (?P<operands>[^;]+);$")
_REFERENCE = re.compile(r"^(?P<name>\w+)\[(?P<index>[0-9]+)\]$")


def _qasm_circuit(source: str, layout: CircuitLayout) -> Circuit:
    """Parse the emitted primitive subset back into the netlist model."""

    declarations: dict[str, int] = {}
    operations: list[Gate] = []
    for line in source.splitlines():
        declaration = _DECLARATION.fullmatch(line)
        if declaration:
            declarations[declaration.group("name")] = int(
                declaration.group("size") or "1"
            )
            continue
        instruction = _INSTRUCTION.fullmatch(line)
        if not instruction:
            continue
        operands: list[int] = []
        for raw_operand in instruction.group("operands").split(","):
            reference = _REFERENCE.fullmatch(raw_operand.strip())
            assert reference is not None
            name = reference.group("name")
            index = int(reference.group("index"))
            if name in layout.registers:
                operands.append(int(layout.registers[name][index]))
            elif name == "ancilla":
                operands.append(int(layout.ancillas[index]))
            else:
                raise AssertionError(f"unknown emitted register {name!r}")
        operations.append(Gate(instruction.group("name").upper(), tuple(operands)))
    assert declarations
    return Circuit(operations, num_qubits=layout.num_qubits)


def test_clean_ancilla_mcx_decomposition_is_basis_state_equivalent() -> None:
    layout = CircuitLayout({"data": (0, 1, 2, 3)}, ancillas=(4,), num_qubits=5)
    source = Circuit((Gate.mcx((0, 1, 2), 3),), num_qubits=5, layout=layout)
    primitive_gates = tuple(
        Gate(name.upper(), qubits) for name, qubits in decompose_mcx((0, 1, 2), 3, (4,))
    )
    decomposed = Circuit(primitive_gates, num_qubits=5, layout=layout)

    assert len(decomposed.gates) == 3
    assert all(gate.name == "CCX" for gate in decomposed.gates)
    for data in range(16):
        assert simulate(source, initial=data, check_ancillas=False) == simulate(
            decomposed, initial=data, check_ancillas=False
        )


@pytest.mark.parametrize(
    ("source", "function", "samples"),
    (
        (
            "#include <stdint.h>\n"
            "uint8_t equal(uint8_t x, uint8_t y) { return x == y; }\n",
            "equal",
            ((0, 0), (1, 2), (255, 255)),
        ),
        (
            "#include <stdint.h>\n"
            "uint8_t choose(uint8_t x, uint8_t y) { return x < y ? x : y; }\n",
            "choose",
            ((0, 1), (2, 1), (255, 255)),
        ),
    ),
)
def test_compiled_predication_qasm_round_trips_through_the_internal_validator(
    source: str, function: str, samples: Iterable[tuple[int, int]]
) -> None:
    circuit = qcc.compile_c(source, function=function)
    assert circuit.layout is not None
    qasm = emit_qasm3(circuit)

    assert validate_qasm3(qasm, expected_layout=circuit.layout)
    assert "mcx" not in qasm.lower()
    round_tripped = _qasm_circuit(qasm, circuit.layout)
    for first, second in samples:
        initial = assign_inputs(circuit, {"x": first, "y": second})
        expected = simulate(circuit, initial=initial, check_ancillas=False)
        actual = simulate(round_tripped, initial=initial, check_ancillas=False)
        assert actual == expected


def test_resource_report_has_stable_clean_ancilla_toffoli_formula() -> None:
    layout = CircuitLayout({"input": (0, 1, 2, 3)}, ancillas=(4, 5), num_qubits=6)
    circuit = Circuit(
        (
            Gate.x(0),
            Gate.cx(0, 1),
            Gate.ccx(0, 1, 2),
            Gate.mcx((0, 1, 2, 3), 4),
        ),
        num_qubits=6,
        layout=layout,
    )

    report = resource_report(circuit)

    assert report.qubits == 6
    assert report.ancillas == 2
    assert report.gates == 4
    assert report.toffoli == 1
    assert report.mcx == 1
    assert report.depth == 4
    assert report.toffoli_estimate == 6  # 1 source CCX + (2*4 - 3) for MCX
    assert report.primitive_gate_count == 8
    assert report.t_count == 42  # exact CCX Clifford+T estimate: 6 * 7
    assert circuit.report() == report
    assert report.as_dict()["t_count"] == 42


def test_named_registers_are_emitted_for_anonymous_primitive_circuit() -> None:
    layout = CircuitLayout(
        {"controls": (0, 1), "target": (2,)}, ancillas=(3,), num_qubits=4
    )
    circuit = Circuit((Gate.ccx(0, 1, 2),), num_qubits=4, layout=layout)

    qasm = emit_qasm3(circuit)

    assert "qubit[2] controls;" in qasm
    assert "qubit[1] target;" in qasm
    assert "qubit[1] ancilla;" in qasm
    assert "ccx controls[0], controls[1], target[0];" in qasm
    assert validate_qasm3(qasm)


def test_qiskit_importer_is_optional() -> None:
    qiskit = pytest.importorskip("qiskit")
    qasm3 = getattr(qiskit, "qasm3", None)
    if qasm3 is None:
        pytest.skip("Qiskit OpenQASM 3 importer is unavailable")
    layout = CircuitLayout({"data": (0, 1, 2, 3)}, ancillas=(4,), num_qubits=5)
    qasm = emit_qasm3(Circuit((Gate.mcx((0, 1, 2), 3),), num_qubits=5, layout=layout))
    try:
        imported = qasm3.loads(qasm)
    except ImportError:
        pytest.skip("Qiskit OpenQASM 3 importer dependency is unavailable")
    assert imported.num_qubits == 5
