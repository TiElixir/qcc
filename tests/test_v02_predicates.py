"""v0.2 differential coverage for every integer ``icmp`` predicate."""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

import qcc
from qcc.sim.classical import assign_inputs, batch_simulate, read_register
from v02_helpers import (
    assert_inverse_restores,
    build_native_library,
    native_function,
)

FIXTURE = Path(__file__).parent / "fixtures" / "v02_predicates.c"
PREDICATE_NAMES = (
    "eq",
    "ne",
    "ugt",
    "uge",
    "ult",
    "ule",
    "sgt",
    "sge",
    "slt",
    "sle",
)


@pytest.fixture(scope="module")
def native_library(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    return build_native_library(FIXTURE, tmp_path_factory)


@pytest.fixture(scope="module")
def circuit() -> object:
    return qcc.compile_c(FIXTURE, function="all_icmp_predicates")


def _signed8(value: int) -> int:
    return value if value < 128 else value - 256


def _expected_mask(a: int, b: int) -> int:
    signed_a = _signed8(a)
    signed_b = _signed8(b)
    predicates = (
        a == b,
        a != b,
        a > b,
        a >= b,
        a < b,
        a <= b,
        signed_a > signed_b,
        signed_a >= signed_b,
        signed_a < signed_b,
        signed_a <= signed_b,
    )
    return sum(int(result) << bit for bit, result in enumerate(predicates))


def test_all_icmp_predicates_match_native_for_every_16_bit_input(
    native_library: ctypes.CDLL, circuit: object
) -> None:
    """Compare eq/ne and all unsigned/signed order predicates exhaustively."""

    assert len(PREDICATE_NAMES) == 10
    native = native_function(
        native_library,
        "all_icmp_predicates",
        (ctypes.c_uint8, ctypes.c_uint8),
        ctypes.c_uint16,
    )
    cases = tuple((a, b) for a in range(256) for b in range(256))
    states = batch_simulate(
        circuit,
        (assign_inputs(circuit, {"a": a, "b": b}) for a, b in cases),
        check_ancillas=False,
    )
    for (a, b), state in zip(cases, states, strict=True):
        expected = _expected_mask(a, b)
        native_result = int(native(a, b))
        assert native_result == expected
        assert read_register(state, circuit.layout.output) == expected
        assert all(
            ((state >> int(qubit)) & 1) == 0 for qubit in circuit.layout.ancillas
        )


def test_icmp_circuit_inverse_restores_deterministic_basis_states(
    circuit: object,
) -> None:
    samples = ((0, 0), (0, 255), (1, 128), (127, 128), (255, 255))
    for a, b in samples:
        assert_inverse_restores(circuit, {"a": a, "b": b})
