"""SSA/frontend coverage for v0.3 arithmetic lowering."""

from __future__ import annotations

from itertools import product

import pytest

import qcc
from qcc.errors import UnsupportedConstructError
from qcc.frontend.llvm_parser import parse_module
from qcc.lower import UnsupportedSSAOperation, lower_ssa
from qcc.sim.classical import assign_inputs, batch_simulate, read_register


def _batch_states(
    circuit: object, inputs: tuple[dict[str, int], ...]
) -> tuple[int, ...]:
    return batch_simulate(
        circuit,  # type: ignore[arg-type]
        (assign_inputs(circuit, values) for values in inputs),  # type: ignore[arg-type]
        check_ancillas=False,
    )


def _binary_function(opcode: str, width: int, rhs: object) -> dict[str, object]:
    return {
        "name": opcode,
        "args": {"left": {"width": width}, "right": {"width": width}},
        "return_type": {"width": width},
        "instructions": [
            {
                "opcode": opcode,
                "result": "result",
                "operands": ["%left", rhs],
                "width": width,
            },
            {"opcode": "ret", "operands": ["%result"], "width": width},
        ],
    }


def test_mul_lowering_is_exhaustive_and_bennett_clean() -> None:
    for width in range(1, 7):
        function = _binary_function("mul", width, "%right")
        circuit = qcc.compile_c(function)
        mask = (1 << width) - 1
        cases = tuple(
            {"left": left, "right": right}
            for left, right in product(range(1 << width), repeat=2)
        )
        states = _batch_states(circuit, cases)
        for values, state in zip(cases, states, strict=True):
            left, right = values["left"], values["right"]
            assert read_register(state, circuit.layout.output) == (left * right) & mask
            assert all(((state >> qubit) & 1) == 0 for qubit in circuit.layout.ancillas)


def test_constant_mul_specialization_is_exhaustive() -> None:
    for width in range(1, 7):
        for constant in (0, 1, 3, (1 << width) - 1):
            function = _binary_function("mul", width, constant)
            circuit = qcc.compile_c(function)
            mask = (1 << width) - 1
            cases = tuple({"left": left, "right": 0} for left in range(1 << width))
            states = _batch_states(circuit, cases)
            for values, state in zip(cases, states, strict=True):
                left = values["left"]
                assert (
                    read_register(state, circuit.layout.output)
                    == (left * constant) & mask
                )
                assert all(
                    ((state >> qubit) & 1) == 0 for qubit in circuit.layout.ancillas
                )


def test_constant_udiv_and_urem_are_exhaustive_widths_one_through_six() -> None:
    for width in range(1, 7):
        for opcode in ("udiv", "urem"):
            for divisor in range(1, 1 << width):
                function = _binary_function(opcode, width, divisor)
                circuit = qcc.compile_c(function)
                cases = tuple(
                    {"left": value, "right": 0} for value in range(1 << width)
                )
                states = _batch_states(circuit, cases)
                for values, state in zip(cases, states, strict=True):
                    value = values["left"]
                    expected = value // divisor if opcode == "udiv" else value % divisor
                    assert read_register(state, circuit.layout.output) == expected
                    assert all(
                        ((state >> qubit) & 1) == 0 for qubit in circuit.layout.ancillas
                    )


def test_unsigned_division_requires_a_constant_nonzero_divisor() -> None:
    dynamic = _binary_function("udiv", 4, "%right")
    with pytest.raises(UnsupportedSSAOperation, match="constant divisor"):
        lower_ssa(dynamic)

    zero = _binary_function("urem", 4, 0)
    with pytest.raises(UnsupportedSSAOperation, match="zero divisor"):
        lower_ssa(zero)


def test_signed_division_and_remainder_have_clear_frontend_errors() -> None:
    for opcode in ("sdiv", "srem"):
        ir = (
            f"define i8 @signed(i8 %left, i8 %right) {{\n"
            "entry:\n"
            f"  %result = {opcode} i8 %left, %right\n"
            "  ret i8 %result\n"
            "}\n"
        )
        with pytest.raises(UnsupportedConstructError, match="signed division"):
            parse_module(ir, source_name="signed.ll")

        structural = _binary_function(opcode, 4, "%right")
        with pytest.raises(UnsupportedSSAOperation, match="signed"):
            lower_ssa(structural)
