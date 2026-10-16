"""Native C versus reversible-netlist differential tests."""

from __future__ import annotations

import ctypes
import shutil
import subprocess

import pytest

import qcc
from qcc.sim.classical import read_register, read_registers, simulate

SOURCE = r"""
#include <stdint.h>

uint8_t add8(uint8_t a, uint8_t b) { return a + b; }
uint8_t xor8(uint8_t a, uint8_t b) { return a ^ b; }
uint8_t and8(uint8_t a, uint8_t b) { return a & b; }
uint8_t composed8(uint8_t a, uint8_t b) {
    return (uint8_t)((a ^ b) + (a & b));
}
"""


@pytest.fixture(scope="module")
def native_library(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    """Compile the differential fixture once with the system Clang."""

    clang = shutil.which("clang")
    if clang is None:
        pytest.fail("clang is required for differential tests")
    directory = tmp_path_factory.mktemp("native")
    source = directory / "fixture.c"
    library = directory / "fixture.so"
    source.write_text(SOURCE)
    subprocess.run(
        [clang, "-std=c11", "-O2", "-shared", "-fPIC", str(source), "-o", str(library)],
        check=True,
        capture_output=True,
        text=True,
    )
    return ctypes.CDLL(str(library))


@pytest.mark.parametrize(
    "name,reference",
    [
        ("add8", lambda a, b: (a + b) & 0xFF),
        ("xor8", lambda a, b: a ^ b),
        ("and8", lambda a, b: a & b),
        ("composed8", lambda a, b: ((a ^ b) + (a & b)) & 0xFF),
    ],
)
def test_all_uint8_binary_inputs_match_native(
    native_library: ctypes.CDLL,
    name: str,
    reference: object,
) -> None:
    """Compare every 8-bit input pair and assert all declared ancillas clean."""

    native = getattr(native_library, name)
    native.argtypes = [ctypes.c_uint8, ctypes.c_uint8]
    native.restype = ctypes.c_uint8
    circuit = qcc.compile_c(SOURCE, function=name)
    assert circuit.layout is not None
    assert len(circuit.layout.output) == 8
    reference_function = reference
    assert callable(reference_function)

    for a in range(256):
        for b in range(256):
            state = simulate(circuit, {"a": a, "b": b})
            output = read_register(state, circuit.layout.output)
            expected = int(reference_function(a, b))  # type: ignore[operator]
            assert output == expected
            assert output == int(native(a, b))
            registers = read_registers(state, circuit)
            assert registers["a"] == a
            assert registers["b"] == b
            assert all(((state >> qubit) & 1) == 0 for qubit in circuit.layout.ancillas)


def test_circuit_followed_by_reverse_restores_basis_state() -> None:
    """The full reversible netlist is a permutation with a valid inverse."""

    circuit = qcc.compile_c(SOURCE, function="composed8")
    assert circuit.layout is not None
    for a in (0, 1, 127, 128, 255):
        for b in (0, 1, 127, 128, 255):
            initial = (a << circuit.layout.inputs["a"][0]) | (
                b << circuit.layout.inputs["b"][0]
            )
            # The compact helper only needs to check the full register domain;
            # the dedicated simulator test above checks the semantic result.
            from qcc.sim.classical import simulate as run_simulator

            final = run_simulator(circuit, initial=initial, check_ancillas=False)
            restored = run_simulator(
                circuit.inverse(), initial=final, check_ancillas=False
            )
            assert restored == initial
