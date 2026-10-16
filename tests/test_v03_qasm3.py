"""OpenQASM 3 syntax and internal-validation coverage for v0.3."""

from __future__ import annotations

import pytest

from qcc import CircuitBuilder, CircuitLayout
from qcc.backend.qasm3 import (
    QASM3Error,
    QASM3ValidationError,
    emit_qasm3,
    is_valid_qasm3,
    validate_qasm3,
)


def test_qasm3_emits_named_registers_with_valid_primitive_syntax() -> None:
    layout = CircuitLayout(
        {"input": (0, 1), "output": (2,), "workspace": (3,)},
        ancillas=(3,),
        num_qubits=4,
    )
    circuit = CircuitBuilder(layout).x(0).cx(1, 2).ccx(0, 1, 3).build()

    qasm = emit_qasm3(circuit)

    assert qasm == (
        "OPENQASM 3.0;\n"
        'include "stdgates.inc";\n\n'
        "qubit[2] input;\n"
        "qubit[1] output;\n"
        "qubit[1] workspace;\n\n"
        "x input[0];\n"
        "cx input[1], output[0];\n"
        "ccx input[0], input[1], workspace[0];\n"
    )
    assert validate_qasm3(qasm, expected_layout=layout)
    assert is_valid_qasm3(qasm)


@pytest.mark.parametrize(
    "source",
    (
        'OPENQASM 2.0;\ninclude "stdgates.inc";\nqubit[1] q;\n',
        "OPENQASM 3.0;\nqubit[1] q;\nx q[0];\n",
        (
            "OPENQASM 3.0;\n"
            'include "stdgates.inc";\n'
            'include "stdgates.inc";\n'
            "qubit[1] q;\n"
        ),
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[0] q;\n',
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[1] q;\nqubit[1] q;\n',
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[1] q;\nx q[1];\n',
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[1] q;\nmeasure q[0];\n',
        'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[1] q;\ncx q[0];\n',
    ),
)
def test_internal_qasm3_validation_rejects_malformed_programs(source: str) -> None:
    with pytest.raises(QASM3ValidationError):
        validate_qasm3(source)
    assert not is_valid_qasm3(source)


def test_internal_qasm3_validation_checks_the_declared_layout() -> None:
    qasm = 'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[2] input;\nx input[0];\n'
    expected = CircuitLayout({"different": (0, 1)}, num_qubits=2)

    with pytest.raises(QASM3ValidationError, match="layout mismatch"):
        validate_qasm3(qasm, expected_layout=expected)


def test_qasm3_validation_rejects_an_undeclared_register_from_emission() -> None:
    class InvalidCircuit:
        num_qubits = 1
        operations = (("x", "missing[0]"),)

    with pytest.raises(QASM3Error):
        emit_qasm3(InvalidCircuit())
