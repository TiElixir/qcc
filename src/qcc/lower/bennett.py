"""Primitive reversible blocks and Bennett compute-copy-uncompute scheduling."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from .ancilla import AncillaAllocator, Register


class GateError(ValueError):
    """Raised when a primitive gate has invalid qubit operands."""


@dataclass(frozen=True)
class Gate:
    """An X, CNOT, or Toffoli gate in little-endian circuit space."""

    name: str
    qubits: tuple[int, ...]

    def __post_init__(self) -> None:
        name = self.name.lower()
        expected = {"x": 1, "cx": 2, "ccx": 3}.get(name)
        if expected is None:
            raise GateError(f"unsupported reversible gate {self.name!r}")
        if len(self.qubits) != expected:
            raise GateError(f"{name} expects {expected} qubits, got {len(self.qubits)}")
        if any(qubit < 0 for qubit in self.qubits):
            raise GateError("gate qubit indices cannot be negative")
        if len(set(self.qubits)) != len(self.qubits):
            raise GateError(f"{name} cannot reuse a qubit: {self.qubits!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "qubits", tuple(int(q) for q in self.qubits))

    @property
    def opcode(self) -> str:
        return self.name

    @property
    def operation(self) -> str:
        return self.name

    @property
    def controls(self) -> tuple[int, ...]:
        return self.qubits[:-1]

    @property
    def target(self) -> int:
        return self.qubits[-1]

    def as_tuple(self) -> tuple[str, tuple[int, ...]]:
        return self.name, self.qubits


class GateBuffer:
    """Mutable gate builder used internally before a netlist is constructed."""

    def __init__(self) -> None:
        self._gates: list[Gate] = []

    @property
    def gates(self) -> tuple[Gate, ...]:
        return tuple(self._gates)

    def __len__(self) -> int:
        return len(self._gates)

    def mark(self) -> int:
        return len(self._gates)

    def x(self, target: int) -> None:
        self._gates.append(Gate("x", (target,)))

    def cx(self, control: int, target: int) -> None:
        self._gates.append(Gate("cx", (control, target)))

    def ccx(self, control_a: int, control_b: int, target: int) -> None:
        self._gates.append(Gate("ccx", (control_a, control_b, target)))

    def append(self, gate: Gate) -> None:
        self._gates.append(gate)

    def extend(self, gates: Iterable[Gate]) -> None:
        self._gates.extend(gates)

    def block(self, start: int) -> tuple[Gate, ...]:
        if start < 0 or start > len(self._gates):
            raise GateError(f"invalid gate block start {start}")
        return tuple(self._gates[start:])

    def discard_from(self, start: int) -> tuple[Gate, ...]:
        block = self.block(start)
        del self._gates[start:]
        return block

    def uncompute(self, start: int = 0) -> None:
        """Append the inverse of every gate emitted since ``start``.

        X, CNOT, and Toffoli are self-inverse, so reversing the gate order is
        sufficient.  The output copy is intentionally emitted after this
        operation by the SSA lowering pass and is therefore not uncomputed.
        """

        self.extend(reversed(self.block(start)))

    def compute_copy_uncompute(
        self,
        compute: Callable[[], None],
        source: Sequence[int],
        destination: Sequence[int],
    ) -> None:
        """Run a computation, copy its result, and restore its workspace."""

        start = self.mark()
        compute()
        copy_bits(self, source, destination)
        self.uncompute(start)


class BennettScheduler(GateBuffer):
    """Named scheduler facade used by clients that build custom blocks."""

    def compute(self, block: Callable[[], None]) -> int:
        start = self.mark()
        block()
        return start

    def copy(self, source: Sequence[int], destination: Sequence[int]) -> None:
        copy_bits(self, source, destination)

    def uncompute_from(self, start: int) -> None:
        self.uncompute(start)


def _check_same_width(first: Register, second: Register, label: str) -> None:
    if first.width != second.width:
        raise GateError(
            f"{label} requires equal widths, got {first.width} and {second.width}"
        )


def _check_target_width(source: Sequence[int], target: Sequence[int]) -> None:
    if len(source) != len(target):
        raise GateError(
            f"source and target registers must have equal widths, got "
            f"{len(source)} and {len(target)}"
        )


def copy_bits(
    emitter: GateBuffer, source: Sequence[int], target: Sequence[int]
) -> None:
    """XOR ``source`` into a zero or existing target register."""

    _check_target_width(source, target)
    for source_bit, target_bit in zip(source, target, strict=False):
        if source_bit != target_bit:
            emitter.cx(source_bit, target_bit)


def xor_bits(
    emitter: GateBuffer, first: Register, second: Register, target: Register
) -> None:
    """Compute ``target ^= first ^ second`` without changing either source."""

    _check_same_width(first, second, "xor")
    _check_same_width(first, target, "xor")
    copy_bits(emitter, first.bits, target.bits)
    copy_bits(emitter, second.bits, target.bits)


def and_bits(
    emitter: GateBuffer, first: Register, second: Register, target: Register
) -> None:
    """Compute a bitwise AND into a fresh target register."""

    _check_same_width(first, second, "and")
    _check_same_width(first, target, "and")
    for first_bit, second_bit, target_bit in zip(
        first.bits, second.bits, target.bits, strict=True
    ):
        if first_bit == second_bit:
            if first_bit != target_bit:
                emitter.cx(first_bit, target_bit)
        else:
            emitter.ccx(first_bit, second_bit, target_bit)


def or_bits(
    emitter: GateBuffer, first: Register, second: Register, target: Register
) -> None:
    """Compute a bitwise OR into a fresh target register."""

    _check_same_width(first, second, "or")
    _check_same_width(first, target, "or")
    for first_bit, second_bit, target_bit in zip(
        first.bits, second.bits, target.bits, strict=True
    ):
        if first_bit == second_bit:
            if first_bit != target_bit:
                emitter.cx(first_bit, target_bit)
            continue
        emitter.cx(first_bit, target_bit)
        emitter.cx(second_bit, target_bit)
        emitter.ccx(first_bit, second_bit, target_bit)


def _append_modular_addition(
    emitter: GateBuffer,
    source: Register,
    target: Register,
    allocator: AncillaAllocator,
    label: str,
) -> Register:
    """Append the clean Cuccaro modular adder ``target += source``.

    The first source register is preserved.  The carry qubit is the running
    carry in the Cuccaro MAJ/UMA chain and is returned to zero by the reverse
    UMA pass.  The target register receives the sum modulo ``2**width``.
    """

    _check_same_width(source, target, "modular addition")
    carry = allocator.allocate_ancilla(1, f"{label}.carry")
    source_bits = source.bits
    target_bits = target.bits
    carry_bit = carry.bits[0]

    def majority(first: int, second: int, incoming: int) -> None:
        emitter.cx(first, second)
        emitter.cx(first, incoming)
        emitter.ccx(second, incoming, first)

    def unmajority_add(first: int, second: int, incoming: int) -> None:
        emitter.ccx(second, incoming, first)
        emitter.cx(first, incoming)
        emitter.cx(incoming, second)

    majority(source_bits[0], target_bits[0], carry_bit)
    for index in range(1, source.width):
        majority(source_bits[index], target_bits[index], source_bits[index - 1])
    for index in range(source.width - 1, 0, -1):
        unmajority_add(source_bits[index], target_bits[index], source_bits[index - 1])
    unmajority_add(source_bits[0], target_bits[0], carry_bit)
    return carry


def _append_addition_with_carry(
    emitter: GateBuffer,
    source: Register,
    target: Register,
    carry_in: int,
    carry_out: int,
) -> None:
    """Append ``target += source + carry_in`` and toggle ``carry_out``.

    This is the carry-out form of the same Cuccaro ripple used by the
    modular adder.  The source, incoming carry, and target are restored or
    updated in place, while the outgoing carry is XORed with the final carry.
    Keeping the primitive here lets the lowering pass build comparisons
    without depending on the public arithmetic-library API.
    """

    _check_same_width(source, target, "addition with carry")
    source_bits = source.bits
    target_bits = target.bits

    def majority(first: int, second: int, incoming: int) -> None:
        emitter.cx(first, second)
        emitter.cx(first, incoming)
        emitter.ccx(second, incoming, first)

    def unmajority_add(first: int, second: int, incoming: int) -> None:
        emitter.ccx(second, incoming, first)
        emitter.cx(first, incoming)
        emitter.cx(incoming, second)

    majority(source_bits[0], target_bits[0], carry_in)
    for index in range(1, source.width):
        majority(source_bits[index], target_bits[index], source_bits[index - 1])
    emitter.cx(source_bits[-1], carry_out)
    for index in range(source.width - 1, 0, -1):
        unmajority_add(source_bits[index], target_bits[index], source_bits[index - 1])
    unmajority_add(source_bits[0], target_bits[0], carry_in)


def sub_with_borrow(
    emitter: GateBuffer,
    subtrahend: Register,
    target: Register,
    borrow_in: int,
    borrow_out: int,
) -> None:
    """Append ``target -= subtrahend + borrow_in`` with a borrow flag.

    ``borrow_in`` is preserved and ``borrow_out`` is toggled exactly when the
    unsigned subtraction borrows.  The target and subtrahend are otherwise
    treated like the public subtraction helper, making this useful as a
    reversible unsigned-comparison primitive.
    """

    _check_same_width(subtrahend, target, "subtraction with borrow")
    for qubit in target.bits:
        emitter.x(qubit)
    _append_addition_with_carry(
        emitter,
        subtrahend,
        target,
        borrow_in,
        borrow_out,
    )
    for qubit in target.bits:
        emitter.x(qubit)


def add_modular(
    emitter: GateBuffer,
    source: Register,
    target: Register,
    allocator: AncillaAllocator,
    *,
    label: str = "add",
) -> Register:
    """Append ``target = target + source`` modulo the register width."""

    return _append_modular_addition(emitter, source, target, allocator, label)


def _append_modular_subtraction(
    emitter: GateBuffer,
    source: Register,
    target: Register,
    allocator: AncillaAllocator,
    label: str,
) -> Register:
    """Append clean ``target -= source`` using the complement identity."""

    _check_same_width(source, target, "modular subtraction")
    for qubit in target.bits:
        emitter.x(qubit)
    carry = _append_modular_addition(emitter, source, target, allocator, label)
    for qubit in target.bits:
        emitter.x(qubit)
    return carry


def sub_modular(
    emitter: GateBuffer,
    source: Register,
    target: Register,
    allocator: AncillaAllocator,
    *,
    label: str = "sub",
) -> Register:
    """Append ``target = target - source`` modulo the register width."""

    return _append_modular_subtraction(emitter, source, target, allocator, label)


def add_registers(
    emitter: GateBuffer,
    first: Register,
    second: Register,
    target: Register,
    allocator: AncillaAllocator,
    *,
    label: str = "add",
) -> None:
    """Compute ``target = first + second`` into a fresh register.

    The ripple block uses clean workspace for carries, copies its sum into the
    requested result, and is then reversed.  Thus both the work register and
    carries are clean before the next SSA instruction runs.
    """

    _check_same_width(first, second, "add")
    _check_same_width(first, target, "add")
    work = allocator.allocate_ancilla(first.width, f"{label}.work")
    start = emitter.mark()
    copy_bits(emitter, second.bits, work.bits)
    add_modular(emitter, first, work, allocator, label=label)
    computation = emitter.block(start)
    copy_bits(emitter, work.bits, target.bits)
    emitter.extend(reversed(computation))


def sub_registers(
    emitter: GateBuffer,
    first: Register,
    second: Register,
    target: Register,
    allocator: AncillaAllocator,
    *,
    label: str = "sub",
) -> None:
    """Compute ``target = first - second`` into a fresh register."""

    _check_same_width(first, second, "sub")
    _check_same_width(first, target, "sub")
    work = allocator.allocate_ancilla(first.width, f"{label}.work")
    start = emitter.mark()
    copy_bits(emitter, first.bits, work.bits)
    sub_modular(emitter, second, work, allocator, label=label)
    computation = emitter.block(start)
    copy_bits(emitter, work.bits, target.bits)
    emitter.extend(reversed(computation))


def inverse_block(emitter: GateBuffer, start: int) -> tuple[Gate, ...]:
    """Append and return the inverse of the block starting at ``start``."""

    block = emitter.block(start)
    inverse = tuple(reversed(block))
    emitter.extend(inverse)
    return inverse


__all__ = [
    "BennettScheduler",
    "Gate",
    "GateBuffer",
    "GateError",
    "add_modular",
    "add_registers",
    "and_bits",
    "copy_bits",
    "inverse_block",
    "or_bits",
    "sub_modular",
    "sub_registers",
    "sub_with_borrow",
    "xor_bits",
]
