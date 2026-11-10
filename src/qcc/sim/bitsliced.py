"""Bit-sliced simulation: run one circuit on many inputs at once.

The classical simulator in :mod:`qcc.sim.classical` packs all wires of one
input into one integer.  This module transposes that layout: each wire is one
arbitrary-precision integer whose bit ``k`` is the wire's value for input
number ``k``.  An X/CX/CCX gate then costs one or two big-integer operations
no matter how many inputs are being simulated, so sweeping all 65,536 inputs
of a 16-bit function takes about as long as simulating a handful one by one.

Inputs are supplied as *columns*: ``columns[q]`` is the slice for wire ``q``.
:func:`exhaustive_columns` builds the columns that enumerate every assignment
of a set of wires, in chunks so that wide input spaces never need more than
``2**chunk_bits`` bits per wire.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from qcc.ir.netlist import Circuit


def simulate_columns(
    circuit: Circuit, columns: Mapping[int, int] | Sequence[int], count: int
) -> list[int]:
    """Simulate ``count`` inputs at once and return one slice per wire.

    ``columns`` maps a wire index to its slice (missing wires start at zero).
    Bit ``k`` of the returned slice for wire ``q`` is that wire's final value
    for input ``k``.
    """

    if count < 1:
        raise ValueError("count must be positive")
    mask = (1 << count) - 1
    state = [0] * circuit.num_qubits
    items = columns.items() if isinstance(columns, Mapping) else enumerate(columns)
    for wire, column in items:
        if column < 0 or column > mask:
            raise ValueError(f"column for wire {wire} does not fit {count} inputs")
        state[int(wire)] = column
    for gate in circuit.gates:
        qubits = gate.qubits
        target = int(qubits[-1])
        if len(qubits) == 1:
            state[target] ^= mask
        elif len(qubits) == 2:
            state[target] ^= state[int(qubits[0])]
        elif len(qubits) == 3:
            state[target] ^= state[int(qubits[0])] & state[int(qubits[1])]
        else:
            active = state[int(qubits[0])]
            for control in qubits[1:-1]:
                active &= state[int(control)]
            state[target] ^= active
    return state


@dataclass(frozen=True, slots=True)
class InputChunk:
    """One batch of an input sweep.

    ``columns[i]`` is the slice for the ``i``-th swept bit.  Input ``k`` of
    the chunk assigns the swept bits one combined value, bit ``i`` of which
    goes to swept bit ``i``: ``samples[k]`` for a sampled chunk, and
    ``base + k`` for a chunk of an exhaustive sweep.
    """

    base: int
    count: int
    columns: tuple[int, ...]
    samples: tuple[int, ...] | None = None

    def value(self, index: int) -> int:
        """Return the combined input value of input ``index`` in this chunk."""

        if self.samples is not None:
            return self.samples[index]
        return self.base + index


def truth_table_column(bit: int, bits: int) -> int:
    """Return the slice of variable ``bit`` over all ``2**bits`` assignments."""

    if not 0 <= bit < bits:
        raise ValueError("bit must be one of the enumerated variables")
    period = 1 << (bit + 1)
    block = ((1 << (1 << bit)) - 1) << (1 << bit)
    total = 1 << bits
    # Repeating a ``period``-bit block is a multiplication by 0b...0001...0001.
    return block * (((1 << total) - 1) // ((1 << period) - 1))


def exhaustive_columns(bits: int, *, chunk_bits: int = 16) -> Iterator[InputChunk]:
    """Yield chunks that together enumerate every ``bits``-bit input once."""

    if bits < 0:
        raise ValueError("bits cannot be negative")
    if chunk_bits < 1:
        raise ValueError("chunk_bits must be positive")
    low_bits = min(bits, chunk_bits)
    count = 1 << low_bits
    ones = (1 << count) - 1
    low = tuple(truth_table_column(bit, low_bits) for bit in range(low_bits))
    for high in range(1 << (bits - low_bits)):
        fixed = tuple(
            ones if (high >> offset) & 1 else 0 for offset in range(bits - low_bits)
        )
        yield InputChunk(high << low_bits, count, low + fixed)


def sample_columns(values: Sequence[int], bits: int) -> InputChunk:
    """Build the chunk that assigns ``values[k]`` to input ``k``."""

    if not values:
        raise ValueError("at least one sample is required")
    columns = [0] * bits
    for index, value in enumerate(values):
        if value < 0 or value >> bits:
            raise ValueError(f"sample {value} does not fit in {bits} bits")
        remaining = value
        bit = 0
        while remaining:
            if remaining & 1:
                columns[bit] |= 1 << index
            remaining >>= 1
            bit += 1
    return InputChunk(0, len(values), tuple(columns), tuple(values))


def first_set_bit(value: int) -> int:
    """Return the index of the lowest set bit of a nonzero integer."""

    if value == 0:
        raise ValueError("value has no set bit")
    return (value & -value).bit_length() - 1


__all__ = [
    "InputChunk",
    "exhaustive_columns",
    "first_set_bit",
    "sample_columns",
    "simulate_columns",
    "truth_table_column",
]
