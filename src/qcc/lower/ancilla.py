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
    """Allocate disjoint input, value, output, and clean-ancilla registers.

    The default allocator is monotonic and is the v0.3 lowering baseline.  A
    reusable allocator can be requested for a lifetime-aware lowering pass;
    released non-input/non-output registers are then leased again by width.
    Physical bits remain unique in the final layout even when several logical
    register leases used them over time.
    """

    def __init__(self, *, first_qubit: int = 0, reuse: bool = False) -> None:
        if first_qubit < 0:
            raise AllocationError("first qubit cannot be negative")
        self._next_qubit = first_qubit
        self._registers: list[Register] = []
        self._inputs: dict[str, Register] = {}
        self._used_names: set[str] = set()
        self._reuse = bool(reuse)
        self._free: dict[int, list[tuple[int, ...]]] = {}
        self._active: dict[tuple[int, ...], Register] = {}
        self._physical_bits: list[tuple[int, ...]] = []
        self._physical_kinds: dict[tuple[int, ...], str] = {}
        self._allocation_events: list[Register] = []

    def _allocate(self, width: int, name: str, kind: str) -> Register:
        if width <= 0:
            raise AllocationError(f"{name!r} must have positive width")
        if not name:
            raise AllocationError("register name cannot be empty")
        if name in self._used_names:
            raise AllocationError(f"register name {name!r} is already allocated")
        reusable = self._reuse and kind not in {"input", "output"}
        available = self._free.get(width, []) if reusable else []
        if available:
            bits = available.pop(0)
        else:
            bits = tuple(range(self._next_qubit, self._next_qubit + width))
            self._next_qubit += width
            self._physical_bits.append(bits)
            self._physical_kinds[bits] = kind
        register = Register(name=name, width=width, bits=bits, kind=kind)
        self._registers.append(register)
        self._allocation_events.append(register)
        self._used_names.add(name)
        self._active[bits] = register
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
    def reuse_enabled(self) -> bool:
        """Whether released temporary registers may be leased again."""

        return self._reuse

    def allocation_mark(self) -> int:
        """Return a token for allocations made after this point."""

        return len(self._allocation_events)

    def allocations_since(self, mark: int) -> tuple[Register, ...]:
        """Return register leases created after ``mark``."""

        if mark < 0 or mark > len(self._allocation_events):
            raise AllocationError("invalid allocation mark")
        return tuple(self._allocation_events[mark:])

    def release(self, register: Register) -> None:
        """Return a clean temporary lease to the reusable allocator.

        Release is intentionally unavailable on the monotonic baseline.  The
        optimized lowering path only calls it after appending the inverse of
        the register's producer, so a released lease is known to be clean.
        """

        if not self._reuse:
            raise AllocationError("register release requires a reusable allocator")
        if register.kind in {"input", "output"}:
            raise AllocationError(f"cannot release {register.kind} register")
        active = self._active.get(register.bits)
        if active != register:
            raise AllocationError(f"register {register.name!r} is not an active lease")
        del self._active[register.bits]
        available = self._free.setdefault(register.width, [])
        if register.bits not in available:
            available.append(register.bits)
            available.sort()

    @property
    def active_registers(self) -> tuple[Register, ...]:
        """Return currently live register leases in allocation order."""

        return tuple(
            register
            for register in self._registers
            if self._active.get(register.bits) == register
        )

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
            for bits in self._physical_bits
            if self._physical_kinds[bits] not in {"input", "output"}
            for bit in bits
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
