"""Focused v0.2 lowering tests built from frontend-shaped SSA values."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import qcc
from qcc.errors import UnsupportedConstructError
from qcc.ir import BasicBlock, Function, Instruction, IntType, Parameter, SSAValue
from qcc.ir.netlist import Circuit
from qcc.ir.ssa import IntConstant
from qcc.lower import UnsupportedSSAOperation, lower_ssa
from qcc.sim.classical import read_register, simulate


def _run(function: object, inputs: dict[str, int]) -> int:
    circuit = qcc.compile_c(function)
    assert circuit.layout is not None
    state = simulate(circuit, inputs)
    return read_register(state, circuit.layout.output)


def _comparison(predicate: str) -> Function:
    width = IntType(4)
    boolean = IntType(1)
    left = Parameter("left", width)
    right = Parameter("right", width)
    result = SSAValue("result", boolean)
    return Function(
        f"compare_{predicate}",
        boolean,
        (left, right),
        (
            BasicBlock(
                "entry",
                (
                    Instruction(
                        "icmp",
                        result,
                        boolean,
                        (left, right),
                        predicate=predicate,
                        operand_type=width,
                    ),
                    Instruction("ret", None, boolean, (result,)),
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    "predicate,expected",
    [
        ("eq", lambda a, b: a == b),
        ("ne", lambda a, b: a != b),
        ("ult", lambda a, b: a < b),
        ("ule", lambda a, b: a <= b),
        ("ugt", lambda a, b: a > b),
        ("uge", lambda a, b: a >= b),
        ("slt", lambda a, b: a < b),
        ("sle", lambda a, b: a <= b),
        ("sgt", lambda a, b: a > b),
        ("sge", lambda a, b: a >= b),
    ],
)
def test_all_icmp_predicates_lower_to_clean_boolean_outputs(
    predicate: str, expected: object
) -> None:
    function = _comparison(predicate)
    signed = predicate.startswith("s")
    reference = expected
    assert callable(reference)
    for left in (0, 1, 7, 8, 15):
        for right in (0, 2, 7, 8, 15):
            left_value = left if not signed or left < 8 else left - 16
            right_value = right if not signed or right < 8 else right - 16
            assert _run(function, {"left": left, "right": right}) == int(
                reference(left_value, right_value)  # type: ignore[operator]
            )


def test_dict_select_and_constant_shifts_are_accepted() -> None:
    function = {
        "name": "select_and_shift",
        "args": {
            "condition": {"width": 1},
            "left": {"width": 4},
            "right": {"width": 4},
        },
        "return_type": {"width": 4},
        "instructions": [
            {
                "opcode": "select",
                "result": "selected",
                "condition": "%condition",
                "true_value": "%left",
                "false_value": "%right",
                "type": {"width": 4},
            },
            {
                "opcode": "rotl",
                "result": "rotated",
                "operands": ["%selected", {"value": 1, "type": {"width": 4}}],
                "type": {"width": 4},
            },
            {"opcode": "ret", "operands": ["%rotated"], "type": {"width": 4}},
        ],
    }
    for condition in (0, 1):
        for left in (0, 1, 5, 15):
            for right in (0, 2, 9, 14):
                selected = left if condition else right
                expected = ((selected << 1) | (selected >> 3)) & 0xF
                assert (
                    _run(
                        function,
                        {"condition": condition, "left": left, "right": right},
                    )
                    == expected
                )


def test_constant_shifts_cover_logical_arithmetic_and_rotate_directions() -> None:
    width = IntType(4)
    value = Parameter("value", width)
    for opcode in ("shl", "lshr", "ashr", "rotr"):
        result = SSAValue("result", width)
        function = Function(
            opcode,
            width,
            (value,),
            (
                BasicBlock(
                    "entry",
                    (
                        Instruction(
                            opcode,
                            result,
                            width,
                            (value, IntConstant(width, 1)),
                        ),
                        Instruction("ret", None, width, (result,)),
                    ),
                ),
            ),
        )
        for raw in range(16):
            signed = raw if raw < 8 else raw - 16
            if opcode == "shl":
                expected = (raw << 1) & 0xF
            elif opcode == "lshr":
                expected = raw >> 1
            elif opcode == "ashr":
                expected = (signed >> 1) & 0xF
            else:
                expected = ((raw >> 1) | ((raw & 1) << 3)) & 0xF
            assert _run(function, {"value": raw}) == expected


def test_select_phi_and_nested_diamonds_use_reversible_path_predicates() -> None:
    width = IntType(4)
    boolean = IntType(1)
    value = Parameter("value", width)
    outer = SSAValue("outer", boolean)
    inner = SSAValue("inner", boolean)
    low = SSAValue("low", width)
    middle = SSAValue("middle", width)
    high = SSAValue("high", width)
    result = SSAValue("result", width)
    function = Function(
        "nested_diamond",
        width,
        (value,),
        (
            BasicBlock(
                "entry",
                (
                    Instruction(
                        "icmp",
                        outer,
                        boolean,
                        (value, 8),
                        predicate="ult",
                        operand_type=width,
                    ),
                    Instruction("br", operands=(outer, "outer_then", "outer_else")),
                ),
            ),
            BasicBlock(
                "outer_then",
                (
                    Instruction(
                        "icmp",
                        inner,
                        boolean,
                        (value, 4),
                        predicate="ult",
                        operand_type=width,
                    ),
                    Instruction("br", operands=(inner, "low_block", "middle_block")),
                ),
            ),
            BasicBlock(
                "low_block",
                (
                    Instruction("add", low, width, (value, 1)),
                    Instruction("br", operands=("join",)),
                ),
            ),
            BasicBlock(
                "middle_block",
                (
                    Instruction("sub", middle, width, (value, 1)),
                    Instruction("br", operands=("join",)),
                ),
            ),
            BasicBlock(
                "outer_else",
                (
                    Instruction("copy", high, width, (value,)),
                    Instruction("br", operands=("join",)),
                ),
            ),
            BasicBlock(
                "join",
                (
                    Instruction(
                        "phi",
                        result,
                        width,
                        (
                            (low, "low_block"),
                            (middle, "middle_block"),
                            (high, "outer_else"),
                        ),
                    ),
                    Instruction("ret", None, width, (result,)),
                ),
            ),
        ),
    )
    circuit = qcc.compile_c(function)
    for raw in range(16):
        expected = (raw + 1) & 0xF if raw < 4 else (raw - 1) & 0xF
        if raw >= 8:
            expected = raw
        assert _run(function, {"value": raw}) == expected
    assert circuit.layout is not None
    # The default simulator check proves all declared path/value work bits are
    # clean after the output copy.
    simulate(circuit, {"value": 7})


def test_cycle_and_data_dependent_shift_are_rejected_explicitly() -> None:
    width = {"width": 4}
    boolean = {"width": 1}
    cyclic = {
        "name": "loop",
        "args": {"value": width, "condition": boolean},
        "return_type": width,
        "blocks": {
            "entry": {"instructions": [{"opcode": "br", "target": "loop"}]},
            "loop": {
                "instructions": [
                    {
                        "opcode": "br",
                        "condition": "%condition",
                        "true_target": "loop",
                        "false_target": "exit",
                    }
                ]
            },
            "exit": {"instructions": [{"opcode": "ret", "operands": ["%value"]}]},
        },
    }
    with pytest.raises(UnsupportedConstructError, match="cyclic CFG"):
        lower_ssa(cyclic)

    shift = {
        "args": {"value": width, "amount": width},
        "return_type": width,
        "instructions": [
            {
                "opcode": "shl",
                "result": "shifted",
                "operands": ["%value", "%amount"],
                "type": width,
            },
            {"opcode": "ret", "operands": ["%shifted"], "type": width},
        ],
    }
    with pytest.raises(UnsupportedSSAOperation, match="constant shift amount"):
        lower_ssa(shift)


def test_existing_anonymous_netlist_circuit_is_an_accepted_api_input() -> None:
    circuit = Circuit.empty(2)
    assert qcc.compile_c(circuit) is circuit


@dataclass(frozen=True)
class _UnsupportedFixture:
    opcode: str = "sdiv"


def test_unsupported_ssa_operation_is_not_silently_approximated() -> None:
    function = {
        "args": {"value": {"width": 4}},
        "return_type": {"width": 4},
        "instructions": [
            {
                "opcode": _UnsupportedFixture().opcode,
                "result": "x",
                "operands": ["%value", 2],
                "width": 4,
            },
            {"opcode": "ret", "operands": ["%x"], "type": {"width": 4}},
        ],
    }
    with pytest.raises(UnsupportedSSAOperation, match="unsupported"):
        lower_ssa(function)
