"""Focused v0.4 optimizer behavior and reduction coverage."""

from __future__ import annotations

from qcc.opt import (
    constant_fold,
    dead_value_eliminate,
    optimize_circuit,
    optimize_gates,
    optimize_ssa,
)

import qcc
from qcc import Circuit, Gate
from qcc.backend.report import resource_report
from qcc.ir.ssa import (
    BasicBlock,
    Function,
    Instruction,
    IntConstant,
    IntType,
    Parameter,
    SSAValue,
)
from qcc.sim.classical import read_register, simulate

_ADD_SOURCE = """
#include <stdint.h>
uint8_t add8(uint8_t a, uint8_t b) { return a + b; }
"""
_XOR_SOURCE = """
#include <stdint.h>
uint8_t xor_same(uint8_t a) { return a ^ a; }
"""
_CONSTANT_SOURCE = """
#include <stdint.h>
uint8_t constant8(uint8_t unused) { return (uint8_t)(3 + 5); }
"""


def _run_inverse_check(circuit: Circuit, state: int) -> None:
    forward = simulate(circuit, initial=state, check_ancillas=False)
    restored = simulate(circuit.inverse(), initial=forward, check_ancillas=False)
    assert restored == state


def _ssa_constant_and_dead_value() -> Function:
    u8 = IntType(8)
    argument = Parameter("a", u8)
    dead = SSAValue("dead", u8)
    result = SSAValue("result", u8)
    return Function(
        "constant_and_dead",
        u8,
        (argument,),
        (
            BasicBlock(
                "entry",
                (
                    Instruction(
                        "add",
                        result=dead,
                        type=u8,
                        operands=(argument, IntConstant(u8, 1)),
                    ),
                    Instruction(
                        "add",
                        result=result,
                        type=u8,
                        operands=(IntConstant(u8, 3), IntConstant(u8, 5)),
                    ),
                    Instruction("ret", type=u8, operands=(result,)),
                ),
            ),
        ),
    )


def test_level_zero_is_an_identity_and_preserves_the_v03_stream() -> None:
    baseline = qcc.compile_c(_ADD_SOURCE, function="add8", opt=0)
    default_level = qcc.compile_c(_ADD_SOURCE, function="add8", opt=1)

    assert baseline.gates == default_level.gates
    assert baseline.layout == default_level.layout
    assert optimize_circuit(baseline, 0) is baseline
    assert optimize_gates(baseline.gates, 0) == baseline.gates


def test_peephole_cancellation_commutes_only_safe_gates_and_blocks_mcx() -> None:
    commuting = Circuit(
        (Gate.cx(0, 2), Gate.cx(1, 2), Gate.cx(0, 2)),
        num_qubits=3,
    )
    reduced = optimize_circuit(commuting, 2)
    assert reduced.gates == (Gate.cx(1, 2),)
    for state in range(8):
        assert simulate(commuting, state) == simulate(reduced, state)

    blocked = Circuit(
        (
            Gate.mcx((0, 1, 2), 3),
            Gate.x(4),
            Gate.mcx((0, 1, 2), 3),
        ),
        num_qubits=5,
    )
    blocked_result = optimize_circuit(blocked, 2)
    assert blocked_result.gates == blocked.gates
    for state in range(32):
        assert simulate(blocked, state) == simulate(
            blocked_result, state, check_ancillas=False
        )


def test_add_optimizer_reduces_gate_depth_and_t_resources() -> None:
    baseline = qcc.compile_c(_ADD_SOURCE, function="add8", opt=0)
    optimized = qcc.compile_c(_ADD_SOURCE, function="add8", opt=2)

    assert optimized.layout == baseline.layout
    assert optimized.resources().gates < baseline.resources().gates
    assert optimized.resources().depth < baseline.resources().depth
    assert optimized.report().t_count < baseline.report().t_count
    assert optimized.report().optimizer == 2
    assert resource_report(optimized).as_dict()["optimizer"] == 2

    assert baseline.layout is not None
    for a in range(0, 256, 17):
        for b in range(0, 256, 29):
            inputs = {"a": a, "b": b}
            assert simulate(baseline, inputs) == simulate(optimized, inputs)
    for state in (0, 1, 0x1234, 0xFFFF):
        _run_inverse_check(optimized, state)


def test_xor_identity_and_constant_expression_reduce_without_remapping_wires() -> None:
    xor_baseline = qcc.compile_c(_XOR_SOURCE, function="xor_same", opt=0)
    xor_optimized = qcc.compile_c(_XOR_SOURCE, function="xor_same", opt=2)
    constant_baseline = qcc.compile_c(_CONSTANT_SOURCE, function="constant8", opt=0)
    constant_optimized = qcc.compile_c(_CONSTANT_SOURCE, function="constant8", opt=2)

    for baseline, optimized in (
        (xor_baseline, xor_optimized),
        (constant_baseline, constant_optimized),
    ):
        assert optimized.layout == baseline.layout
        assert optimized.num_qubits == baseline.num_qubits
        assert optimized.resources().gates < baseline.resources().gates
        assert optimized.resources().qubits == baseline.resources().qubits
        assert optimized.resources().ancillas == baseline.resources().ancillas
        assert baseline.layout is not None
        for value in range(256):
            inputs = (
                {"a": value} if "a" in baseline.layout.inputs else {"unused": value}
            )
            assert simulate(baseline, inputs) == simulate(optimized, inputs)
        for state in (0, 1, 0xFFFF):
            _run_inverse_check(optimized, state)


def test_ssa_helpers_fold_constants_and_remove_dead_values() -> None:
    function = _ssa_constant_and_dead_value()
    folded = constant_fold(function)
    assert isinstance(folded, Function)
    folded_instructions = folded.entry.instructions
    assert folded_instructions[1].opcode == "const"
    assert isinstance(folded_instructions[-1].operands[0], IntConstant)

    dce = dead_value_eliminate(function)
    assert isinstance(dce, Function)
    assert all(
        instruction.result is not None for instruction in dce.entry.instructions[:1]
    )
    assert all(
        instruction.result is None or instruction.result.name != "dead"
        for instruction in dce.entry.instructions
    )
    assert optimize_ssa(function, 0) is function
    optimized_ssa = optimize_ssa(function, 2)
    assert isinstance(optimized_ssa, Function)
    assert tuple(instruction.opcode for instruction in optimized_ssa.instructions) == (
        "ret",
    )


def test_ssa_dead_value_elimination_reduces_qubits_and_keeps_result() -> None:
    function = _ssa_constant_and_dead_value()
    baseline = qcc.compile_c(function, function="constant_and_dead", opt=0)
    optimized = qcc.compile_c(function, function="constant_and_dead", opt=2)

    assert optimized.num_qubits < baseline.num_qubits
    assert optimized.resources().gates < baseline.resources().gates
    assert optimized.layout is not None
    for value in range(256):
        before = simulate(baseline, {"a": value}, check_ancillas=False)
        after = simulate(optimized, {"a": value}, check_ancillas=False)
        assert read_register(before, baseline.layout.output) == 8  # type: ignore[union-attr]
        assert read_register(after, optimized.layout.output) == 8
    _run_inverse_check(optimized, 0)
