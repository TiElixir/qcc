"""Reversible subtraction built from Cuccaro addition and complements.

The primitive operation in this module preserves the subtrahend and updates a
same-width target register:

``subtract_modular(builder, subtrahend, target)``
    maps ``target`` to ``target - subtrahend (mod 2**n)``.

It complements the target, invokes the Cuccaro adder, and complements the
result.  The adder's carry workspace is therefore restored as well.  The
borrow variant uses the same identity and toggles an output bit exactly when
``target < subtrahend + borrow_in``.
"""

from __future__ import annotations

from collections.abc import Sequence
from operator import index as _index

from qcc.ir.netlist import Circuit, CircuitBuilder, CircuitLayout, QubitLike
from qcc.lib.adder import (
    Register,
    add_modular,
    add_with_carry,
    copy_register,
)


def _register(values: Register, name: str) -> tuple[QubitLike, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of qubits")
    result = tuple(values)
    if not result:
        raise ValueError(f"{name} must not be empty")
    numeric = tuple(int(_index(value)) for value in result)
    if len(set(numeric)) != len(numeric):
        raise ValueError(f"{name} contains duplicate qubits")
    return result


def _one_qubit(value: QubitLike | Sequence[QubitLike], name: str) -> QubitLike:
    if isinstance(value, int):
        return value
    values = tuple(value)
    if len(values) != 1:
        raise ValueError(f"{name} must be one qubit")
    return values[0]


def _same_width(a: tuple[QubitLike, ...], b: tuple[QubitLike, ...]) -> None:
    if len(a) != len(b):
        raise ValueError(f"register widths differ: {len(a)} and {len(b)}")


def _disjoint(*groups: tuple[QubitLike, ...]) -> None:
    seen: set[int] = set()
    for group in groups:
        numeric = {int(value) for value in group}
        overlap = seen & numeric
        if overlap:
            rendered = ", ".join(f"q[{value}]" for value in sorted(overlap))
            raise ValueError(f"subtraction operands must be disjoint ({rendered})")
        seen.update(numeric)


def complement_register(builder: CircuitBuilder, register: Register) -> CircuitBuilder:
    """Append a bitwise complement of an LSB-first register."""

    values = _register(register, "register")
    for qubit in values:
        builder.x(qubit)
    return builder


complement = complement_register


def subtract_modular(
    builder: CircuitBuilder,
    subtrahend: Register,
    target: Register,
    carry: QubitLike | Sequence[QubitLike] | None = None,
    *,
    carry_in: QubitLike | Sequence[QubitLike] | None = None,
) -> CircuitBuilder:
    """Append ``target <- target - subtrahend (mod 2**n)``.

    The two input registers are preserved according to their roles: the
    subtrahend is unchanged and the target becomes the result.  ``carry`` is a
    clean workspace and is restored; it may be selected from declared layout
    ancillas by omitting it.  The complement construction is
    ``~(~target + subtrahend)`` in fixed width arithmetic.
    """

    aa = _register(subtrahend, "subtrahend")
    bb = _register(target, "target")
    _same_width(aa, bb)
    _disjoint(aa, bb)
    if carry is not None and carry_in is not None:
        raise ValueError("carry and carry_in are aliases; supply only one")
    selected = carry if carry is not None else carry_in
    complement_register(builder, bb)
    add_modular(builder, aa, bb, selected)
    complement_register(builder, bb)
    return builder


sub_modular = subtract_modular
subtract = subtract_modular


def subtract_with_borrow(
    builder: CircuitBuilder,
    subtrahend: Register,
    target: Register,
    borrow_in: QubitLike,
    borrow_out: QubitLike,
) -> CircuitBuilder:
    """Append subtraction with XORed borrow-in and borrow-out bits.

    The target becomes ``target - subtrahend - borrow_in`` modulo ``2**n``;
    the input borrow bit is preserved and ``borrow_out`` is toggled iff the
    unsigned subtraction borrows.  The construction uses the carry identity
    ``~target + subtrahend + borrow_in`` and cleans the complemented target
    through the matching adder uncompute.
    """

    aa = _register(subtrahend, "subtrahend")
    bb = _register(target, "target")
    bin_bit = _one_qubit(borrow_in, "borrow_in")
    bout_bit = _one_qubit(borrow_out, "borrow_out")
    _same_width(aa, bb)
    _disjoint(aa, bb, (bin_bit,), (bout_bit,))
    complement_register(builder, bb)
    add_with_carry(builder, aa, bb, bin_bit, bout_bit)
    complement_register(builder, bb)
    return builder


sub_with_borrow = subtract_with_borrow


def subtract_into(
    builder: CircuitBuilder,
    minuend: Register,
    subtrahend: Register,
    target: Register,
    carry: QubitLike | Sequence[QubitLike] | None = None,
) -> CircuitBuilder:
    """Copy ``minuend`` into a clean target and subtract ``subtrahend``.

    This is an out-of-place helper.  It preserves both source registers and
    performs ``target ^= minuend - subtrahend``; with a zero target it is the
    usual ``target = minuend - subtrahend`` operation.  The target must be
    disjoint from both sources and the carry workspace.
    """

    aa = _register(minuend, "minuend")
    bb = _register(subtrahend, "subtrahend")
    dst = _register(target, "target")
    _same_width(aa, bb)
    _same_width(aa, dst)
    _disjoint(aa, bb, dst)
    if carry is not None:
        carry_bit = _one_qubit(carry, "carry")
        _disjoint(aa, bb, dst, (carry_bit,))
    copy_register(builder, aa, dst)
    subtract_modular(builder, bb, dst, carry)
    return builder


def _fragment_layout(
    subtrahend: tuple[QubitLike, ...],
    target: tuple[QubitLike, ...],
    carry: QubitLike,
    *,
    num_qubits: int,
    layout: CircuitLayout | None,
) -> CircuitLayout | None:
    if layout is not None:
        if layout.num_qubits != num_qubits:
            raise ValueError("fragment layout width does not match its operands")
        return layout
    return CircuitLayout(
        {"subtrahend": subtrahend, "target": target},
        ancillas=(carry,),
        num_qubits=num_qubits,
    )


def modular_subtractor(
    subtrahend: Register,
    target: Register,
    carry: QubitLike | None = None,
    *,
    num_qubits: int | None = None,
    layout: CircuitLayout | None = None,
) -> Circuit:
    """Return an immutable ``target -= subtrahend`` fragment."""

    aa = _register(subtrahend, "subtrahend")
    bb = _register(target, "target")
    _same_width(aa, bb)
    _disjoint(aa, bb)
    if carry is None:
        highest = max((int(value) for value in aa + bb), default=-1)
        carry = highest + 1
    cin = _one_qubit(carry, "carry")
    _disjoint(aa, bb, (cin,))
    width = max((int(value) for value in aa + bb + (cin,)), default=-1) + 1
    if num_qubits is not None:
        width = int(_index(num_qubits))
        if width <= max(int(value) for value in aa + bb + (cin,)):
            raise ValueError("fragment width is smaller than an operand")
    fragment_layout = _fragment_layout(aa, bb, cin, num_qubits=width, layout=layout)
    builder = CircuitBuilder(width, layout=fragment_layout, auto_grow=False)
    subtract_modular(builder, aa, bb, cin)
    return builder.build()


subtractor = modular_subtractor


__all__ = [
    "complement",
    "complement_register",
    "modular_subtractor",
    "sub_modular",
    "sub_with_borrow",
    "subtract",
    "subtract_into",
    "subtract_modular",
    "subtract_with_borrow",
    "subtractor",
]
