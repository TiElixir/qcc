"""Register and clean-ancilla allocation for reversible lowering.

The lowering pipeline uses little-endian bit tuples: bit zero is the first
entry in a register.  Keeping allocation here, rather than in the gate
emitter, makes it possible for the netlist implementation to remain a small
and immutable data model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


class AllocationError(ValueError):
    """Raised when a register cannot be allocated."""


@dataclass(frozen=True)
class Register:
    """A contiguous logical register in the emitted circuit."""

    name: str
    width: int
    bits: tuple[int, ...]
    kind: str = "ancilla"

    def __post_init__(self) -> None:
        if self.width <= 0:
            raise AllocationError(f"register {self.name!r} must have positive width")
        if len(self.bits) != self.width:
            raise AllocationError(
                f"register {self.name!r} has width {self.width} but "
                f"{len(self.bits)} bits"
            )
        if len(set(self.bits)) != len(self.bits):
            raise AllocationError(f"register {self.name!r} contains duplicate bits")

    @property
    def lsb(self) -> int:
        """Return the least-significant bit's qubit index."""

        return self.bits[0]

    @property
    def msb(self) -> int:
        """Return the most-significant bit's qubit index."""

        return self.bits[-1]


@dataclass(frozen=True)
class CircuitLayout:
    """Named input/output registers and the qubits used as clean ancillas.

    ``inputs`` maps source-level argument names to little-endian qubit tuples.
    ``output`` is the little-endian return register.  The output is deliberately
    separate from ``ancillas``: Bennett scheduling leaves it populated while
    every qubit in ``ancillas`` is returned to zero.
    """

    inputs: Mapping[str, tuple[int, ...]]
    output: tuple[int, ...]
    ancillas: tuple[int, ...]

    def __post_init__(self) -> None:
        normalized = {
            str(name): tuple(int(bit) for bit in bits)
            for name, bits in self.inputs.items()
        }
        output = tuple(int(bit) for bit in self.output)
        ancillas = tuple(int(bit) for bit in self.ancillas)
        all_bits = [bit for bits in normalized.values() for bit in bits]
        all_bits.extend(output)
        all_bits.extend(ancillas)
        if len(all_bits) != len(set(all_bits)):
            raise AllocationError("circuit layout assigns one qubit to multiple roles")
        if any(bit < 0 for bit in all_bits):
            raise AllocationError(
                "circuit layout cannot contain negative qubit indices"
            )
        object.__setattr__(self, "inputs", MappingProxyType(normalized))
        object.__setattr__(self, "output", output)
        object.__setattr__(self, "ancillas", ancillas)

    @property
    def input_widths(self) -> Mapping[str, int]:
        """Return source argument widths without copying input bit tuples."""

        return MappingProxyType({name: len(bits) for name, bits in self.inputs.items()})

    @property
    def output_width(self) -> int:
        return len(self.output)

    @property
    def input_bits(self) -> tuple[int, ...]:
        """Return all input qubits in argument order."""

        return tuple(bit for bits in self.inputs.values() for bit in bits)


class AncillaAllocator:
    """Allocate disjoint input, value, output, and clean-ancilla registers."""

    def __init__(self, *, first_qubit: int = 0) -> None:
        if first_qubit < 0:
            raise AllocationError("first qubit cannot be negative")
        self._next_qubit = first_qubit
        self._registers: list[Register] = []
        self._inputs: dict[str, Register] = {}
        self._used_names: set[str] = set()

    def _allocate(self, width: int, name: str, kind: str) -> Register:
        if width <= 0:
            raise AllocationError(f"{name!r} must have positive width")
        if not name:
            raise AllocationError("register name cannot be empty")
        if name in self._used_names:
            raise AllocationError(f"register name {name!r} is already allocated")
        bits = tuple(range(self._next_qubit, self._next_qubit + width))
        self._next_qubit += width
        register = Register(name=name, width=width, bits=bits, kind=kind)
        self._registers.append(register)
        self._used_names.add(name)
        return register

    def allocate_input(self, name: str, width: int) -> Register:
        """Allocate a source-level argument register."""

        if name in self._inputs:
            raise AllocationError(f"input {name!r} is already allocated")
        register = self._allocate(width, name, "input")
        self._inputs[name] = register
        return register

    def allocate_value(self, name: str, width: int) -> Register:
        """Allocate a fresh zero-initialized SSA value register."""

        return self._allocate(width, name, "value")

    def allocate_ancilla(self, width: int, name: str | None = None) -> Register:
        """Allocate clean workspace used by a reversible arithmetic block."""

        if name is None:
            name = f"ancilla{len(self._registers)}"
        return self._allocate(width, name, "ancilla")

    def allocate_constant(self, width: int, name: str) -> Register:
        """Allocate a register used to hold an SSA integer literal."""

        return self._allocate(width, name, "constant")

    def allocate_output(self, width: int, name: str = "output") -> Register:
        """Allocate the separate Bennett result register."""

        return self._allocate(width, name, "output")

    @property
    def next_qubit(self) -> int:
        return self._next_qubit

    @property
    def num_qubits(self) -> int:
        return self._next_qubit

    @property
    def registers(self) -> tuple[Register, ...]:
        return tuple(self._registers)

    @property
    def inputs(self) -> Mapping[str, Register]:
        return MappingProxyType(dict(self._inputs))

    def layout(self, output: Register) -> CircuitLayout:
        """Build the public layout for ``output``."""

        if output.kind != "output":
            raise AllocationError("layout output must be an output register")
        ancillas = tuple(
            bit
            for register in self._registers
            if register.kind not in {"input", "output"}
            for bit in register.bits
        )
        return CircuitLayout(
            inputs={name: register.bits for name, register in self._inputs.items()},
            output=output.bits,
            ancillas=ancillas,
        )


# The shorter name is useful to callers that do not need to distinguish the
# allocator from a pool of clean temporary registers.
AncillaPool = AncillaAllocator


__all__ = [
    "AllocationError",
    "AncillaAllocator",
    "AncillaPool",
    "CircuitLayout",
    "Register",
]
