"""v0.2 differential coverage for min/max, absolute difference, and clamp."""

from __future__ import annotations

import ctypes
from functools import cache
from pathlib import Path

import pytest

import qcc
from qcc.sim.classical import assign_inputs, batch_simulate, read_register
from v02_helpers import (
    assert_inverse_restores,
    assert_output_and_clean,
    build_native_library,
    native_function,
)

FIXTURE = Path(__file__).parent / "fixtures" / "v02_arithmetic.c"


@pytest.fixture(scope="module")
def native_library(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    return build_native_library(FIXTURE, tmp_path_factory)


@cache
def _circuit(function: str) -> object:
    return qcc.compile_c(FIXTURE, function=function)


def _binary_expected(function: str, a: int, b: int) -> int:
    if function == "min8":
        return min(a, b)
    if function == "max8":
        return max(a, b)
    if function == "abs_diff8":
        return abs(a - b)
    raise AssertionError(f"unknown arithmetic function {function!r}")


@pytest.mark.parametrize("function", ("min8", "max8", "abs_diff8"))
def test_binary_arithmetic_matches_native_for_every_16_bit_input(
    native_library: ctypes.CDLL, function: str
) -> None:
    native = native_function(
        native_library,
        function,
        (ctypes.c_uint8, ctypes.c_uint8),
        ctypes.c_uint8,
    )
    circuit = _circuit(function)
    cases = tuple((a, b) for a in range(256) for b in range(256))
    states = batch_simulate(
        circuit,
        (assign_inputs(circuit, {"a": a, "b": b}) for a, b in cases),
        check_ancillas=False,
    )
    for (a, b), state in zip(cases, states, strict=True):
        expected = _binary_expected(function, a, b)
        assert int(native(a, b)) == expected
        assert read_register(state, circuit.layout.output) == expected
        assert all(
            ((state >> int(qubit)) & 1) == 0 for qubit in circuit.layout.ancillas
        )


CLAMP_SAMPLES = (
    (0, 37, 211),
    (36, 37, 211),
    (37, 37, 211),
    (128, 37, 211),
    (211, 37, 211),
    (212, 37, 211),
    (255, 37, 211),
    (99, 100, 200),
    (100, 100, 200),
    (150, 100, 200),
    (200, 100, 200),
    (201, 100, 200),
    (0, 0, 255),
    (255, 200, 255),
)


def test_clamp_matches_native_for_deterministic_24_bit_samples(
    native_library: ctypes.CDLL,
) -> None:
    native = native_function(
        native_library,
        "clamp8",
        (ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint8),
        ctypes.c_uint8,
    )
    circuit = _circuit("clamp8")
    for value, lower, upper in CLAMP_SAMPLES:
        expected = min(upper, max(lower, value))
        assert int(native(value, lower, upper)) == expected
        assert_output_and_clean(
            circuit,
            {"value": value, "lower": lower, "upper": upper},
            expected,
        )


@pytest.mark.parametrize(
    ("function", "samples"),
    (
        (
            "min8",
            ((0, 0), (0, 255), (127, 128), (255, 0), (255, 255)),
        ),
        (
            "max8",
            ((0, 0), (0, 255), (127, 128), (255, 0), (255, 255)),
        ),
        (
            "abs_diff8",
            ((0, 0), (0, 255), (127, 128), (255, 0), (255, 255)),
        ),
        ("clamp8", CLAMP_SAMPLES),
    ),
)
def test_arithmetic_circuits_inverse_restore_basis_states(
    function: str, samples: tuple[tuple[int, ...], ...]
) -> None:
    circuit = _circuit(function)
    for sample in samples:
        inputs = (
            {"value": sample[0], "lower": sample[1], "upper": sample[2]}
            if function == "clamp8"
            else {"a": sample[0], "b": sample[1]}
        )
        assert_inverse_restores(circuit, inputs)
