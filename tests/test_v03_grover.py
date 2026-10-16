"""The required is_solution Grover predicate, checked against native ctypes."""

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

FIXTURE = Path(__file__).parent / "fixtures" / "v03_grover.c"


@pytest.fixture(scope="module")
def native_library(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    return build_native_library(FIXTURE, tmp_path_factory)


@cache
def _circuit() -> object:
    return qcc.compile_c(FIXTURE, function="is_solution")


def test_is_solution_matches_native_for_every_uint8_candidate(
    native_library: ctypes.CDLL,
) -> None:
    native = native_function(
        native_library,
        "is_solution",
        (ctypes.c_uint8,),
        ctypes.c_uint8,
    )
    circuit = _circuit()

    for candidate in range(256):
        expected = int(native(candidate))
        assert expected == int(candidate == 42)
        assert_output_and_clean(circuit, {"x": candidate}, expected)


def test_is_solution_circuit_is_reversible_on_grover_boundary_cases() -> None:
    for candidate in (0, 1, 41, 42, 43, 255):
        assert_inverse_restores(_circuit(), {"x": candidate})
