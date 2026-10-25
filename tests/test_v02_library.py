"""Exhaustive v0.2 arithmetic-library coverage."""

from __future__ import annotations

from itertools import product

import pytest

from qcc import CircuitBuilder, CircuitLayout
from qcc.lib.compare import (
    bitwise_equal,
    compare_flags,
    unsigned_greater_equal,
    unsigned_greater_than,
    unsigned_less_equal,
    unsigned_less_than,
)
from qcc.lib.mux import controlled_xor_result, select
from qcc.lib.shift import (
    rotate_left,
    rotate_right,
    shift_left,
    shift_right,
)
from qcc.lower.bennett import GateBuffer
from qcc.sim.classical import batch_simulate, read_register, simulate


def _fast_simulate(circuit: object, initial: int) -> int:
    fast_simulator = getattr(circuit, "fast_simulate", None)
    if callable(fast_simulator):
        return int(fast_simulator(initial))
    return int(simulate(circuit, initial, check_ancillas=False))  # type: ignore[arg-type]


def _registers(width: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(range(width)), tuple(range(width, 2 * width))


def _comparison_circuit(width: int, predicate: object):
    left, right = _registers(width)
    flag = 2 * width
    ancillas = (flag + 1, flag + 2)
    layout = CircuitLayout(
        {"left": left, "right": right, "flag": (flag,)},
        ancillas=ancillas,
        num_qubits=flag + 3,
    )
    builder = CircuitBuilder(layout)
    assert callable(predicate)
    predicate(builder, left, right, flag)
    return builder.build(), left, right, flag, ancillas


@pytest.mark.parametrize(
    "predicate,reference",
    [
        (bitwise_equal, lambda left, right: left == right),
        (unsigned_less_than, lambda left, right: left < right),
        (unsigned_less_equal, lambda left, right: left <= right),
        (unsigned_greater_than, lambda left, right: left > right),
        (unsigned_greater_equal, lambda left, right: left >= right),
    ],
)
def test_comparison_predicates_are_exhaustive_for_widths_one_through_six(
    predicate: object, reference: object
) -> None:
    """Check every unsigned pair and prove the borrow ancillas are clean."""

    assert callable(reference)
    for width in range(1, 7):
        circuit, left, right, flag, ancillas = _comparison_circuit(width, predicate)
        for left_value, right_value in product(range(1 << width), repeat=2):
            initial = left_value | (right_value << width)
            result = _fast_simulate(circuit, initial)
            assert read_register(result, left) == left_value
            assert read_register(result, right) == right_value
            assert ((result >> flag) & 1) == int(reference(left_value, right_value))
            assert all(((result >> qubit) & 1) == 0 for qubit in ancillas)


def test_comparison_flags_can_be_built_together_exhaustively() -> None:
    for width in range(1, 7):
        left, right = _registers(width)
        less, equal, greater = 2 * width, 2 * width + 1, 2 * width + 2
        ancillas = (greater + 1, greater + 2)
        builder = CircuitBuilder(greater + 3)
        compare_flags(
            builder,
            left,
            right,
            less=less,
            equal=equal,
            greater=greater,
            ancillas=ancillas,
        )
        circuit = builder.build()
        for left_value, right_value in product(range(1 << width), repeat=2):
            result = _fast_simulate(circuit, left_value | (right_value << width))
            assert ((result >> less) & 1) == int(left_value < right_value)
            assert ((result >> equal) & 1) == int(left_value == right_value)
            assert ((result >> greater) & 1) == int(left_value > right_value)
            assert all(((result >> qubit) & 1) == 0 for qubit in ancillas)


def test_comparison_flags_are_xor_targets_and_gatebuffer_compatible() -> None:
    left, right = (0,), (1,)
    flag, ancillas = 2, (3, 4)
    builder = CircuitBuilder(5)
    unsigned_less_than(builder, left, right, flag, ancillas)
    circuit = builder.build()
    # With a dirty output target, the predicate is XORed into the old bit.
    assert ((_fast_simulate(circuit, (1 << 1) | (1 << flag)) >> flag) & 1) == 0

    buffer = GateBuffer()
    returned = unsigned_less_than(buffer, left, right, flag, ancillas)
    assert returned is buffer
    assert buffer.gates and all(
        gate.name in {"x", "cx", "ccx"} for gate in buffer.gates
    )


def test_select_is_exhaustive_for_all_condition_and_data_values() -> None:
    for width in range(1, 7):
        condition = 0
        when_false = tuple(range(1, width + 1))
        when_true = tuple(range(width + 1, 2 * width + 1))
        result_register = tuple(range(2 * width + 1, 3 * width + 1))
        builder = CircuitBuilder(3 * width + 1)
        select(builder, condition, when_false, when_true, result_register)
        circuit = builder.build()
        for condition_value, false_value, true_value, old_result in product(
            range(2), range(1 << width), range(1 << width), range(1 << width)
        ):
            initial = (
                (condition_value << condition)
                | (false_value << 1)
                | (true_value << (width + 1))
                | (old_result << (2 * width + 1))
            )
            final = _fast_simulate(circuit, initial)
            expected = old_result ^ (true_value if condition_value else false_value)
            assert read_register(final, result_register) == expected
            assert read_register(final, when_false) == false_value
            assert read_register(final, when_true) == true_value


def test_controlled_xor_result_is_exhaustive_for_widths_one_through_six() -> None:
    for width in range(1, 7):
        condition = 0
        source = tuple(range(1, width + 1))
        result_register = tuple(range(width + 1, 2 * width + 1))
        builder = CircuitBuilder(2 * width + 1)
        controlled_xor_result(builder, condition, source, result_register)
        circuit = builder.build()
        cases = tuple(
            (condition_value, source_value, old_result)
            for condition_value, source_value, old_result in product(
                range(2), range(1 << width), range(1 << width)
            )
        )
        finals = batch_simulate(
            circuit,
            (
                (condition_value << condition)
                | (source_value << 1)
                | (old_result << (width + 1))
                for condition_value, source_value, old_result in cases
            ),
            check_ancillas=False,
        )
        for (condition_value, source_value, old_result), final in zip(
            cases, finals, strict=True
        ):
            assert read_register(final, result_register) == (
                old_result ^ (source_value if condition_value else 0)
            )


def _read_wiring(wiring: tuple[int | None, ...], state: int) -> int:
    return sum(
        (1 << bit) if wire is not None and ((state >> wire) & 1) else 0
        for bit, wire in enumerate(wiring)
    )


def test_constant_shift_and_rotate_wiring_matches_python_for_widths_one_through_six():
    for width in range(1, 7):
        source = tuple(10 + 3 * bit for bit in range(width))
        mask = (1 << width) - 1
        for value in range(1 << width):
            state = sum(((value >> bit) & 1) << wire for bit, wire in enumerate(source))
            for amount in range(width + 3):
                assert (
                    _read_wiring(shift_left(source, amount), state)
                    == (value << amount) & mask
                )
                assert (
                    _read_wiring(shift_right(source, amount), state)
                    == (value >> amount) & mask
                )
                distance = amount % width
                left = (
                    value
                    if distance == 0
                    else ((value << distance) | (value >> (width - distance))) & mask
                )
                right = (
                    value
                    if distance == 0
                    else ((value >> distance) | (value << (width - distance))) & mask
                )
                assert _read_wiring(rotate_left(source, amount), state) == left
                assert _read_wiring(rotate_right(source, amount), state) == right


def test_library_blocks_reject_invalid_widths_and_overlaps() -> None:
    with pytest.raises(ValueError):
        unsigned_less_than(CircuitBuilder(6), (0,), (1, 2), 3, (4, 5))
    with pytest.raises(ValueError):
        unsigned_less_than(CircuitBuilder(6), (0,), (1,), 1, (2, 3))
    with pytest.raises(ValueError):
        unsigned_less_than(CircuitBuilder(4), (0,), (1,), 2, (2,))
    with pytest.raises(ValueError):
        select(CircuitBuilder(4), 0, (1,), (2,), (2,))
    with pytest.raises(ValueError):
        shift_left((0, 0), 1)
    with pytest.raises(ValueError):
        shift_right((0, 1), 1, width=3)
    with pytest.raises(ValueError):
        rotate_left((), 1)
    with pytest.raises(ValueError):
        rotate_right((0, 1), -1)
