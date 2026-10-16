"""Shared native and reversible-circuit helpers for the v0.2 tests."""

from __future__ import annotations

import ctypes
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from qcc.sim.classical import assign_inputs, read_register, simulate


def build_native_library(
    source: Path, tmp_path_factory: pytest.TempPathFactory
) -> ctypes.CDLL:
    """Build one fixture with Clang for ctypes differential checks."""

    clang = shutil.which("clang")
    if clang is None:
        pytest.fail("clang is required for v0.2 native differential tests")
    directory = tmp_path_factory.mktemp(f"native-{source.stem}")
    library_path = directory / f"{source.stem}.so"
    subprocess.run(
        [
            clang,
            "-std=c11",
            "-O2",
            "-shared",
            "-fPIC",
            str(source),
            "-o",
            str(library_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return ctypes.CDLL(str(library_path))


def native_function(
    library: ctypes.CDLL,
    name: str,
    argtypes: Sequence[Any],
    restype: Any,
) -> Any:
    """Configure and return a ctypes entry point from a fixture library."""

    function = getattr(library, name)
    function.argtypes = list(argtypes)
    function.restype = restype
    return function


def _layout_output(circuit: Any) -> Sequence[int]:
    layout = getattr(circuit, "layout", None)
    if layout is None:
        raise AssertionError("compiled circuit has no register layout")
    output = getattr(layout, "output", None)
    if output is not None:
        return tuple(output)
    registers = getattr(layout, "registers", None)
    if isinstance(registers, Mapping) and "output" in registers:
        return tuple(registers["output"])
    raise AssertionError("compiled circuit layout has no output register")


def assert_output_and_clean(
    circuit: Any, inputs: Mapping[str, int], expected: int
) -> int:
    """Check one result and explicitly verify every declared ancilla is zero."""

    state = simulate(circuit, inputs)
    output = read_register(state, _layout_output(circuit))
    assert output == expected
    layout = circuit.layout
    ancillas = tuple(getattr(layout, "ancillas", ()))
    assert all(((state >> int(qubit)) & 1) == 0 for qubit in ancillas)
    return state


def assert_inverse_restores(circuit: Any, inputs: Mapping[str, int]) -> None:
    """Check algebraic inverse identity on a deterministic basis state."""

    initial = assign_inputs(circuit, inputs)
    forward = simulate(circuit, initial=initial, check_ancillas=False)
    inverse = circuit.inverse()
    restored = simulate(inverse, initial=forward, check_ancillas=False)
    assert restored == initial
    assert inverse.inverse() == circuit
