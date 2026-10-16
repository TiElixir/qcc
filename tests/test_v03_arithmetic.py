"""Exhaustive v0.3 multiplication and constant-divmod coverage."""

from __future__ import annotations

from itertools import product

import pytest

from qcc import CircuitBuilder
from qcc.lib.divmod import constant_divmod, constant_divmod_workspace_width
from qcc.lib.multiply import (
    controlled_multiply_modular,
    multiply_modular,
    multiply_widening,
)
from qcc.lower.bennett import GateBuffer
from qcc.sim.classical import read_register, simulate


def _registers(width: int, start: int = 0) -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(range(start + offset * width, start + (offset + 1) * width))
        for offset in range(4)
    )


def test_shift_and_add_multiplication_is_exhaustive_widths_one_through_six() -> None:
    for width in range(1, 7):
        multiplicand, multiplier, target, _ = _registers(width)
        workspace = tuple(range(4 * width, 5 * width + 1))
        builder = CircuitBuilder(5 * width + 1)
        multiply_modular(
            builder,
            multiplicand,
            multiplier,
            target,
            workspace,
        )
        circuit = builder.build()
        mask = (1 << width) - 1
        for left, right in product(range(1 << width), repeat=2):
            state = left | (right << width)
            result = simulate(circuit, state)
            assert read_register(result, multiplicand) == left
            assert read_register(result, multiplier) == right
            assert read_register(result, target) == (left * right) & mask
            assert all(((result >> qubit) & 1) == 0 for qubit in workspace)


def test_widening_multiplication_is_exhaustive_widths_one_through_six() -> None:
    for width in range(1, 7):
        multiplicand = tuple(range(width))
        multiplier = tuple(range(width, 2 * width))
        target = tuple(range(2 * width, 4 * width))
        workspace = tuple(range(4 * width, 6 * width + 1))
        builder = CircuitBuilder(6 * width + 1)
        multiply_widening(
            builder,
            multiplicand,
            multiplier,
            target,
            workspace,
        )
        circuit = builder.build()
        for left, right in product(range(1 << width), repeat=2):
            state = left | (right << width)
            result = simulate(circuit, state)
            assert read_register(result, target) == left * right
            assert all(((result >> qubit) & 1) == 0 for qubit in workspace)


def test_controlled_multiplication_is_exhaustive_and_cleans_workspace() -> None:
    for width in range(1, 7):
        control = 0
        multiplicand = tuple(range(1, width + 1))
        multiplier = tuple(range(width + 1, 2 * width + 1))
        target = tuple(range(2 * width + 1, 3 * width + 1))
        workspace = tuple(range(3 * width + 1, 4 * width + 2))
        builder = CircuitBuilder(4 * width + 2)
        controlled_multiply_modular(
            builder,
            control,
            multiplicand,
            multiplier,
            target,
            workspace,
        )
        circuit = builder.build()
        mask = (1 << width) - 1
        for enabled, left, right in product(
            range(2), range(1 << width), range(1 << width)
        ):
            state = enabled | (left << 1) | (right << (width + 1))
            result = simulate(circuit, state)
            expected = (left * right) & mask if enabled else 0
            assert read_register(result, target) == expected
            assert all(((result >> qubit) & 1) == 0 for qubit in workspace)


def test_multiplication_accumulates_into_a_dirty_target() -> None:
    for width in range(1, 5):
        multiplicand, multiplier, target, _ = _registers(width)
        workspace = tuple(range(4 * width, 5 * width + 1))
        builder = CircuitBuilder(5 * width + 1)
        multiply_modular(builder, multiplicand, multiplier, target, workspace)
        circuit = builder.build()
        mask = (1 << width) - 1
        for left, right, old_target in product(
            range(1 << width), range(1 << width), range(1 << width)
        ):
            state = left | (right << width) | (old_target << (2 * width))
            result = simulate(circuit, state)
            assert read_register(result, target) == (old_target + left * right) & mask
            assert all(((result >> qubit) & 1) == 0 for qubit in workspace)


def test_constant_divmod_is_exhaustive_for_widths_one_through_six() -> None:
    for width in range(1, 7):
        dividend = tuple(range(width))
        quotient = tuple(range(width, 2 * width))
        remainder = tuple(range(2 * width, 3 * width))
        workspace_width = constant_divmod_workspace_width(width)
        workspace = tuple(range(3 * width, 3 * width + workspace_width))
        for divisor in range(1, (1 << width) + 2):
            builder = CircuitBuilder(3 * width + workspace_width)
            constant_divmod(
                builder,
                dividend,
                divisor,
                quotient,
                remainder,
                workspace,
            )
            circuit = builder.build()
            for value in range(1 << width):
                result = simulate(circuit, value)
                assert read_register(result, dividend) == value
                assert read_register(result, quotient) == value // divisor
                assert read_register(result, remainder) == value % divisor
                assert all(((result >> qubit) & 1) == 0 for qubit in workspace)


def test_constant_divmod_xor_targets_and_divisor_policy() -> None:
    for width in range(1, 5):
        dividend = tuple(range(width))
        quotient = tuple(range(width, 2 * width))
        remainder = tuple(range(2 * width, 3 * width))
        workspace_width = constant_divmod_workspace_width(width)
        workspace = tuple(range(3 * width, 3 * width + workspace_width))
        for divisor in range(1, (1 << width) + 2):
            builder = CircuitBuilder(3 * width + workspace_width)
            constant_divmod(
                builder,
                dividend,
                divisor,
                quotient,
                remainder,
                workspace,
            )
            circuit = builder.build()
            for value, old_quotient, old_remainder in product(
                range(1 << width), range(1 << width), range(1 << width)
            ):
                state = value | (old_quotient << width) | (old_remainder << (2 * width))
                result = simulate(circuit, state)
                assert read_register(result, quotient) == (
                    old_quotient ^ (value // divisor)
                )
                assert read_register(result, remainder) == (
                    old_remainder ^ (value % divisor)
                )
                assert all(((result >> qubit) & 1) == 0 for qubit in workspace)

    width = 4
    registers = _registers(width)
    workspace = tuple(
        range(4 * width, 4 * width + constant_divmod_workspace_width(width))
    )
    for emitter in (CircuitBuilder(4 * width + len(workspace)), GateBuffer()):
        with pytest.raises(ValueError, match="nonzero"):
            constant_divmod(
                emitter,
                registers[0],
                0,
                registers[1],
                registers[2],
                workspace,
            )


def test_gatebuffer_accepts_mcx_intermediates_for_controlled_multiply() -> None:
    width = 3
    source = tuple(range(width))
    multiplier = tuple(range(width, 2 * width))
    target = tuple(range(2 * width, 3 * width))
    workspace = tuple(range(3 * width, 4 * width + 1))
    buffer = GateBuffer()
    returned = controlled_multiply_modular(
        buffer,
        4 * width + 1,
        source,
        multiplier,
        target,
        workspace,
    )
    assert returned is buffer
    assert any(gate.name == "mcx" for gate in buffer.gates)
