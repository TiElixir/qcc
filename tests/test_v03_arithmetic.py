"""v0.3 arithmetic differential tests against native fixed-width C."""

from __future__ import annotations

import ctypes
from functools import cache
from pathlib import Path

import pytest

import qcc
from qcc.errors import CompilerError
from v02_helpers import (
    assert_inverse_restores,
    assert_output_and_clean,
    build_native_library,
    native_function,
)

FIXTURE = Path(__file__).parent / "fixtures" / "v03_arithmetic.c"
ZERO_DIVISOR_FIXTURE = Path(__file__).parent / "fixtures" / "v03_zero_divisor.c"


@pytest.fixture(scope="module")
def native_library(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    return build_native_library(FIXTURE, tmp_path_factory)


@cache
def _circuit(function: str) -> object:
    return qcc.compile_c(FIXTURE, function=function)


def test_multiply8_matches_native_for_every_16_bit_input(
    native_library: ctypes.CDLL,
) -> None:
    native = native_function(
        native_library,
        "multiply8",
        (ctypes.c_uint8, ctypes.c_uint8),
        ctypes.c_uint16,
    )
    circuit = _circuit("multiply8")

    for left in range(256):
        for right in range(256):
            expected = left * right
            assert int(native(left, right)) == expected
            assert_output_and_clean(circuit, {"a": left, "b": right}, expected)


@pytest.mark.parametrize(
    ("function", "divisor", "remainder"),
    (
        ("udiv3_8", 3, False),
        ("urem3_8", 3, True),
        ("udiv5_8", 5, False),
        ("urem5_8", 5, True),
        ("udiv255_8", 255, False),
        ("urem255_8", 255, True),
    ),
)
def test_constant_udiv_and_urem_match_native_for_every_uint8_input(
    native_library: ctypes.CDLL,
    function: str,
    divisor: int,
    remainder: bool,
) -> None:
    native = native_function(
        native_library,
        function,
        (ctypes.c_uint8,),
        ctypes.c_uint8,
    )
    circuit = _circuit(function)

    for value in range(256):
        expected = value % divisor if remainder else value // divisor
        assert int(native(value)) == expected
        assert_output_and_clean(circuit, {"value": value}, expected)


@pytest.mark.parametrize("function", ("udiv_zero8", "urem_zero8"))
def test_v03_zero_divisor_is_rejected_by_the_compiler(function: str) -> None:
    """A zero divisor is a compile-time error, never a guessed circuit policy."""

    with pytest.raises(CompilerError) as captured:
        qcc.compile_c(ZERO_DIVISOR_FIXTURE, function=function)

    message = captured.value.message.lower()
    assert "zero" in message
    assert "div" in message or "remainder" in message


@pytest.mark.parametrize(
    ("function", "inputs"),
    (
        ("multiply8", {"a": 0, "b": 255}),
        ("multiply8", {"a": 127, "b": 128}),
        ("multiply8", {"a": 255, "b": 255}),
        ("udiv3_8", {"value": 0}),
        ("udiv3_8", {"value": 255}),
        ("urem5_8", {"value": 255}),
        ("udiv255_8", {"value": 254}),
    ),
)
def test_v03_arithmetic_circuits_inverse_restore_basis_states(
    function: str,
    inputs: dict[str, int],
) -> None:
    assert_inverse_restores(_circuit(function), inputs)
