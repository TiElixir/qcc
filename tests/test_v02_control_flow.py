"""v0.2 differential coverage for diamonds, shifts, and bounded loops."""

from __future__ import annotations

import ctypes
from functools import cache
from pathlib import Path

import pytest

import qcc
from v02_helpers import (
    assert_inverse_restores,
    assert_output_and_clean,
    build_native_library,
    native_function,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURES = {
    "nested_if_else": FIXTURE_DIR / "v02_nested_if.c",
    "constant_shifts": FIXTURE_DIR / "v02_shifts.c",
    "popcount8": FIXTURE_DIR / "v02_loop.c",
}


@pytest.fixture(scope="module")
def native_libraries(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, ctypes.CDLL]:
    return {
        name: build_native_library(source, tmp_path_factory)
        for name, source in FIXTURES.items()
    }


@cache
def _circuit(function: str) -> object:
    return qcc.compile_c(FIXTURES[function], function=function)


def _nested_expected(x: int, y: int) -> int:
    if x == y:
        return x + 1 if x < 128 else y - 1
    if x < y:
        return x if x % 2 == 0 else y
    return y + 1 if y % 2 == 0 else x - 1


def test_nested_if_else_matches_native_for_every_16_bit_input(
    native_libraries: dict[str, ctypes.CDLL],
) -> None:
    native = native_function(
        native_libraries["nested_if_else"],
        "nested_if_else",
        (ctypes.c_uint8, ctypes.c_uint8),
        ctypes.c_uint8,
    )
    circuit = _circuit("nested_if_else")
    for x in range(256):
        for y in range(256):
            expected = _nested_expected(x, y)
            assert int(native(x, y)) == expected
            assert_output_and_clean(circuit, {"x": x, "y": y}, expected)


def test_constant_shifts_match_native_for_every_uint8_input(
    native_libraries: dict[str, ctypes.CDLL],
) -> None:
    native = native_function(
        native_libraries["constant_shifts"],
        "constant_shifts",
        (ctypes.c_uint8,),
        ctypes.c_uint16,
    )
    circuit = _circuit("constant_shifts")
    for value in range(256):
        expected = ((value << 3) & 0xFF) | ((value >> 2) << 8)
        assert int(native(value)) == expected
        assert_output_and_clean(circuit, {"value": value}, expected)


def test_constant_bound_popcount_loop_matches_native_for_every_uint8_input(
    native_libraries: dict[str, ctypes.CDLL],
) -> None:
    native = native_function(
        native_libraries["popcount8"],
        "popcount8",
        (ctypes.c_uint8,),
        ctypes.c_uint8,
    )
    circuit = _circuit("popcount8")
    for value in range(256):
        expected = value.bit_count()
        assert int(native(value)) == expected
        assert_output_and_clean(circuit, {"value": value}, expected)


@pytest.mark.parametrize(
    ("function", "samples"),
    (
        (
            "nested_if_else",
            ((0, 0), (0, 1), (1, 0), (127, 127), (128, 128), (255, 254)),
        ),
        (
            "constant_shifts",
            ((0,), (1,), (31,), (128,), (255,)),
        ),
        (
            "popcount8",
            ((0,), (1,), (3,), (85,), (170,), (255,)),
        ),
    ),
)
def test_control_flow_circuits_inverse_restore_basis_states(
    function: str, samples: tuple[tuple[int, ...], ...]
) -> None:
    circuit = _circuit(function)
    for sample in samples:
        inputs = (
            {"value": sample[0]}
            if function != "nested_if_else"
            else {"x": sample[0], "y": sample[1]}
        )
        assert_inverse_restores(circuit, inputs)
