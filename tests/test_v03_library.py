"""Exhaustive v0.3 multiplication-block coverage for widths one through six."""

from __future__ import annotations

from collections.abc import Callable
from itertools import product
from typing import Any

import pytest

from qcc import CircuitBuilder, CircuitLayout
from qcc.sim.classical import read_register, simulate


def _multiply_into() -> Callable[..., Any]:
    """Load the v0.3 public multiplier API without hiding its absence."""

    try:
        from qcc.lib.multiply import multiply_into
    except ImportError:
        pytest.fail(
            "v0.3 requires the public qcc.lib.multiply.multiply_into block",
            pytrace=False,
        )
    return multiply_into


def _multiplier_circuit(
    width: int,
) -> tuple[
    object,
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    left = tuple(range(width))
    right = tuple(range(width, 2 * width))
    product_register = tuple(range(2 * width, 4 * width))
    ancillas = tuple(range(4 * width, 5 * width + 1))
    layout = CircuitLayout(
        {"left": left, "right": right, "product": product_register},
        ancillas=ancillas,
        num_qubits=5 * width + 1,
    )
    builder = CircuitBuilder(layout)
    returned = _multiply_into()(builder, left, right, product_register)
    assert returned is builder
    return builder.build(), left, right, product_register, ancillas


def test_multiply_into_is_exhaustive_for_clean_products_at_widths_one_through_six() -> (
    None
):
    for width in range(1, 7):
        circuit, left, right, product_register, ancillas = _multiplier_circuit(width)
        for left_value, right_value in product(range(1 << width), repeat=2):
            initial = left_value | (right_value << width)
            result = simulate(circuit, initial)
            assert read_register(result, left) == left_value
            assert read_register(result, right) == right_value
            assert read_register(result, product_register) == left_value * right_value
            assert all(((result >> qubit) & 1) == 0 for qubit in ancillas)


def test_multiply_into_xors_into_a_dirty_product_register() -> None:
    for width in range(1, 5):
        circuit, left, right, product_register, ancillas = _multiplier_circuit(width)
        product_mask = (1 << (2 * width)) - 1
        for left_value, right_value, old_product in product(
            range(1 << width), repeat=3
        ):
            initial = left_value | (right_value << width) | (old_product << (2 * width))
            result = simulate(circuit, initial)
            assert read_register(result, left) == left_value
            assert read_register(result, right) == right_value
            assert (
                read_register(result, product_register)
                == (old_product ^ (left_value * right_value)) & product_mask
            )
            assert all(((result >> qubit) & 1) == 0 for qubit in ancillas)
