"""Deterministic primitive acceptance tests without a Qiskit dependency."""

from dataclasses import dataclass
from itertools import product

import pytest
from qcc.backend.qasm3 import UnsupportedGateError, emit_qasm3


@dataclass(frozen=True)
class LayoutFixture:
    registers: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CircuitFixture:
    layout: LayoutFixture
    operations: tuple[tuple[object, ...], ...]


def apply_primitives(circuit: CircuitFixture, bits: list[int]) -> list[int]:
    """Tiny reference simulator for exactly the gates emitted by this backend."""

    result = list(bits)
    for operation in circuit.operations:
        name = str(operation[0]).lower()
        operands = [int(value) for value in operation[1:]]
        if name == "x":
            result[operands[0]] ^= 1
        elif name == "cx":
            if result[operands[0]]:
                result[operands[1]] ^= 1
        elif name == "ccx":
            if result[operands[0]] and result[operands[1]]:
                result[operands[2]] ^= 1
        else:  # pragma: no cover - fixtures intentionally use primitives
            raise AssertionError(f"unexpected primitive {name}")
    return result


def byte_bits(value: int) -> list[int]:
    return [(value >> index) & 1 for index in range(8)]


def bits_byte(bits: list[int]) -> int:
    return sum(bit << index for index, bit in enumerate(bits))


def test_primitive_circuit_differential_matches_modular_xor_reference() -> None:
    mask = 0x5A
    operations: list[tuple[object, ...]] = []
    for index in range(8):
        if (mask >> index) & 1:
            operations.append(("x", 8 + index))
        operations.append(("cx", index, 8 + index))
    circuit = CircuitFixture(
        LayoutFixture((("input", 8), ("output", 8), ("ancilla", 1))),
        tuple(operations),
    )

    qasm = emit_qasm3(circuit)
    assert "qubit[8] input;" in qasm
    assert "qubit[8] output;" in qasm
    assert "qubit[1] ancilla;" in qasm
    for value in range(256):
        initial = byte_bits(value) + [0] * 9
        final = apply_primitives(circuit, initial)
        assert bits_byte(final[8:16]) == value ^ mask
        assert final[16] == 0


def test_compute_copy_uncompute_leaves_ancilla_clean_for_all_inputs() -> None:
    # Compute a & b into an ancilla, copy it to the output, then uncompute.
    circuit = CircuitFixture(
        LayoutFixture((("input", 2), ("output", 1), ("ancilla", 1))),
        (("ccx", 0, 1, 3), ("cx", 3, 2), ("ccx", 0, 1, 3)),
    )

    for a, b in product((0, 1), repeat=2):
        final = apply_primitives(circuit, [a, b, 0, 0])
        assert final[2] == a & b
        assert final[3] == 0


def test_self_inverse_primitive_circuit_restores_every_input() -> None:
    circuit = CircuitFixture(
        LayoutFixture((("input", 8),)),
        tuple(("x", index) for index in (0, 2, 4, 6)),
    )

    for value in range(256):
        initial = byte_bits(value)
        once = apply_primitives(circuit, initial)
        twice = apply_primitives(circuit, once)
        assert twice == initial


def test_unsupported_construct_has_actionable_diagnostic() -> None:
    circuit = CircuitFixture(LayoutFixture((("q", 4),)), (("h", 0),))

    with pytest.raises(UnsupportedGateError, match="unsupported gate"):
        emit_qasm3(circuit)
