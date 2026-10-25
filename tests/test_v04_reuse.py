"""v0.4 lifetime-aware clean-ancilla reuse coverage."""

from __future__ import annotations

import ctypes
from itertools import product
from pathlib import Path

import pytest

import qcc
from qcc.frontend.clang import compile_c as clang_to_llvm
from qcc.frontend.llvm_parser import parse_module
from qcc.lower import lower_ssa
from qcc.sim.classical import assign_inputs, read_register, simulate
from v02_helpers import build_native_library, native_function

ROOT = Path(__file__).parent
POPCOUNT_FIXTURE = ROOT / "fixtures" / "v02_loop.c"
CFG_FIXTURE = ROOT / "fixtures" / "v02_nested_if.c"


def _composed_function(width: int = 4) -> dict[str, object]:
    return {
        "name": "composed_reuse",
        "args": {"a": {"width": width}, "b": {"width": width}},
        "return_type": {"width": width},
        "instructions": [
            {
                "opcode": "xor",
                "result": "xor_value",
                "operands": ["%a", "%b"],
                "width": width,
            },
            {
                "opcode": "and",
                "result": "and_value",
                "operands": ["%a", "%b"],
                "width": width,
            },
            {
                "opcode": "add",
                "result": "result",
                "operands": ["%xor_value", "%and_value"],
                "width": width,
            },
            {"opcode": "ret", "operands": ["%result"], "width": width},
        ],
    }


def _add_chain(width: int = 4) -> dict[str, object]:
    return {
        "name": "add_chain",
        "args": {
            "a": {"width": width},
            "b": {"width": width},
            "c": {"width": width},
            "d": {"width": width},
        },
        "return_type": {"width": width},
        "instructions": [
            {
                "opcode": "add",
                "result": "first",
                "operands": ["%a", "%b"],
                "width": width,
            },
            {
                "opcode": "add",
                "result": "second",
                "operands": ["%first", "%c"],
                "width": width,
            },
            {
                "opcode": "add",
                "result": "result",
                "operands": ["%second", "%d"],
                "width": width,
            },
            {"opcode": "ret", "operands": ["%result"], "width": width},
        ],
    }


def _output(circuit: object, inputs: dict[str, int]) -> int:
    layout = circuit.layout
    state = simulate(circuit, inputs)
    assert layout is not None
    assert all(((state >> qubit) & 1) == 0 for qubit in layout.ancillas)
    return read_register(state, layout.output)


def test_composed_expression_reuses_dead_values_and_preserves_baseline() -> None:
    function = _composed_function()
    baseline = qcc.compile_c(function, opt=0)
    opt_one = qcc.compile_c(function, opt=1)
    forced_baseline = qcc.compile_c(function, opt=2, reuse_ancillas=False)
    optimized = qcc.compile_c(function, opt=2)

    assert opt_one.gates == baseline.gates
    assert opt_one.num_qubits == baseline.num_qubits
    assert forced_baseline.gates == baseline.gates
    assert forced_baseline.num_qubits == baseline.num_qubits
    assert optimized.num_qubits < baseline.num_qubits
    assert len(optimized.layout.ancillas) == len(set(optimized.layout.ancillas))

    for a, b in product(range(16), repeat=2):
        inputs = {"a": a, "b": b}
        expected = ((a ^ b) + (a & b)) & 0xF
        assert _output(optimized, inputs) == expected
        initial = assign_inputs(optimized, inputs)
        final = simulate(optimized, initial=initial, check_ancillas=False)
        assert (
            simulate(optimized.inverse(), initial=final, check_ancillas=False)
            == initial
        )


def test_in_place_add_chain_is_used_only_for_dead_operands() -> None:
    function = _add_chain()
    baseline = qcc.compile_c(function, opt=0)
    optimized = qcc.compile_c(function, opt=2)

    assert optimized.num_qubits < baseline.num_qubits
    for values in ((0, 0, 0, 0), (1, 2, 3, 4), (15, 15, 15, 15), (7, 8, 1, 6)):
        inputs = dict(zip(("a", "b", "c", "d"), values, strict=True))
        assert _output(optimized, inputs) == sum(values) & 0xF
        initial = assign_inputs(optimized, inputs)
        final = simulate(optimized, initial=initial, check_ancillas=False)
        assert (
            simulate(optimized.inverse(), initial=final, check_ancillas=False)
            == initial
        )


def test_opt_two_falls_back_to_the_baseline_for_cfg_values() -> None:
    source = clang_to_llvm(CFG_FIXTURE, opt=1)
    function = parse_module(source).get_function("nested_if_else")
    baseline = lower_ssa(function)
    explicit_baseline = lower_ssa(function, reuse_ancillas=False)
    optimized_switch = lower_ssa(function, reuse_ancillas=True)

    assert explicit_baseline == baseline
    # The current policy only proves straight-line lifetimes.  This fixture is
    # intentionally used as a switch regression: an explicit safe switch must
    # never alter an unsupported shape.
    assert optimized_switch == baseline


@pytest.fixture(scope="module")
def popcount_native(
    tmp_path_factory: pytest.TempPathFactory,
) -> ctypes.CDLL:
    return build_native_library(POPCOUNT_FIXTURE, tmp_path_factory)


def test_popcount_opt_two_reduces_qubits_and_remains_differentially_clean(
    popcount_native: ctypes.CDLL,
) -> None:
    native = native_function(
        popcount_native,
        "popcount8",
        (ctypes.c_uint8,),
        ctypes.c_uint8,
    )
    baseline = qcc.compile_c(POPCOUNT_FIXTURE, function="popcount8", opt=0)
    optimized = qcc.compile_c(POPCOUNT_FIXTURE, function="popcount8", opt=2)

    assert optimized.num_qubits < baseline.num_qubits
    assert baseline.num_qubits == 303
    assert optimized.num_qubits == 191

    for value in range(256):
        inputs = {"value": value}
        assert _output(optimized, inputs) == int(native(value))

    for value in (0, 1, 3, 85, 170, 255):
        inputs = {"value": value}
        initial = assign_inputs(optimized, inputs)
        final = simulate(optimized, initial=initial, check_ancillas=False)
        assert (
            simulate(optimized.inverse(), initial=final, check_ancillas=False)
            == initial
        )
