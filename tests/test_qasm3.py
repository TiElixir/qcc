"""Acceptance tests for the primitive OpenQASM 3 backend."""

from dataclasses import dataclass

import pytest

from qcc.backend.qasm3 import (
    MCXDecompositionError,
    emit_qasm3,
    validate_qasm3,
)


@dataclass(frozen=True)
class LayoutFixture:
    registers: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CircuitFixture:
    layout: LayoutFixture
    operations: tuple[tuple[object, ...], ...]


def primitive_fixture() -> CircuitFixture:
    return CircuitFixture(
        layout=LayoutFixture((("input", 2), ("output", 1), ("ancilla", 1))),
        operations=(
            ("x", 0),
            ("cx", 1, 2),
            ("ccx", 0, 1, 3),
        ),
    )


def test_qasm3_has_openqasm_header_named_layout_and_primitive_gates() -> None:
    qasm = emit_qasm3(primitive_fixture())

    assert qasm.startswith('OPENQASM 3.0;\ninclude "stdgates.inc";')
    assert "qubit[2] input;" in qasm
    assert "qubit[1] output;" in qasm
    assert "qubit[1] ancilla;" in qasm
    assert "x input[0];" in qasm
    assert "cx input[1], output[0];" in qasm
    assert "ccx input[0], input[1], ancilla[0];" in qasm
    assert validate_qasm3(qasm)


def test_qasm3_preserves_non_contiguous_physical_layout_names() -> None:
    @dataclass(frozen=True)
    class PhysicalLayout:
        registers: dict[str, tuple[int, ...]]

    circuit = CircuitFixture(
        layout=PhysicalLayout({"input": (3, 1), "output": (0,), "ancilla": (2,)}),
        operations=(("cx", 3, 0), ("x", 1), ("ccx", 3, 1, 2)),
    )

    qasm = emit_qasm3(circuit)

    assert "cx input[0], output[0];" in qasm
    assert "x input[1];" in qasm
    assert "ccx input[0], input[1], ancilla[0];" in qasm
    assert validate_qasm3(qasm)


def test_qasm3_validation_rejects_bad_ranges_and_unknown_gates() -> None:
    with pytest.raises(ValueError, match="out of range"):
        validate_qasm3('OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[1] q;\nx q[1];\n')

    with pytest.raises(ValueError, match="unsupported gate"):
        validate_qasm3('OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit q;\nh q;\n')


def test_mcx_is_decomposed_with_a_named_clean_ancilla() -> None:
    circuit = CircuitFixture(
        LayoutFixture((("data", 4), ("ancilla", 1))), (("mcx", 0, 1, 2, 3),)
    )

    qasm = emit_qasm3(circuit)

    assert "mcx" not in qasm
    assert qasm.count("ccx ") == 3
    assert validate_qasm3(qasm)


def test_mcx_without_the_required_clean_ancilla_has_a_strict_error() -> None:
    circuit = CircuitFixture(LayoutFixture((("q", 4),)), (("mcx", 0, 1, 2, 3),))

    with pytest.raises(MCXDecompositionError, match="clean ancilla"):
        emit_qasm3(circuit)
