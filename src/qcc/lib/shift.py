"""Constant shifts and rotates as pure LSB-first register wiring.

These helpers emit no gates and allocate no ancillas.  They return the wire
selected for each output bit, in LSB-first order.  ``None`` denotes a
constant-zero bit introduced by a logical shift; callers lowering a value can
materialize that zero with their normal clean-constant convention.  Rotates
always return existing wires.

For a register ``r = (r[0], ..., r[n-1])`` the mappings are:

* ``shift_left(r, k)  = (0, ..., 0, r[0], ..., r[n-k-1])``;
* ``shift_right(r, k) = (r[k], ..., r[n-1], 0, ..., 0)``;
* ``rotate_left(r, k)  = r[-k:] + r[:-k]``;
* ``rotate_right(r, k) = r[k:] + r[:k]``.

The formulas use the integer meaning of an LSB-first register.  Widths must
be positive, source wires must be unique, and amounts must be non-negative
integer constants.  Shifting by at least the width is valid and returns all
zero wires; rotate amounts are reduced modulo the width.
"""

from __future__ import annotations

from operator import index as _index

from qcc.ir.netlist import QubitLike

from ._common import Register, register

Wire = QubitLike | None


def _amount(amount: int, name: str = "amount") -> int:
    if isinstance(amount, bool):
        raise TypeError(f"{name} must be a non-negative integer")
    try:
        value = int(_index(amount))
    except TypeError as exc:
        raise TypeError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _width(values: tuple[QubitLike, ...], width: int | None) -> int:
    actual = len(values)
    if actual == 0:
        raise ValueError("register width must be positive")
    if width is None:
        return actual
    if isinstance(width, bool):
        raise TypeError("width must be a positive integer")
    try:
        declared = int(_index(width))
    except TypeError as exc:
        raise TypeError("width must be a positive integer") from exc
    if declared <= 0:
        raise ValueError("width must be positive")
    if declared != actual:
        raise ValueError(
            f"declared width {declared} differs from register width {actual}"
        )
    return declared


def shift_left(
    source: Register,
    amount: int,
    width: int | None = None,
) -> tuple[Wire, ...]:
    """Return the LSB-first wiring for a logical constant left shift."""

    values = register(source, "source")
    size = _width(values, width)
    distance = _amount(amount)
    if distance >= size:
        return (None,) * size
    return (None,) * distance + values[: size - distance]


def shift_right(
    source: Register,
    amount: int,
    width: int | None = None,
) -> tuple[Wire, ...]:
    """Return the LSB-first wiring for a logical constant right shift."""

    values = register(source, "source")
    size = _width(values, width)
    distance = _amount(amount)
    if distance >= size:
        return (None,) * size
    return values[distance:] + (None,) * distance


def rotate_left(
    source: Register,
    amount: int,
    width: int | None = None,
) -> tuple[QubitLike, ...]:
    """Return the LSB-first wiring for a constant left rotate."""

    values = register(source, "source")
    size = _width(values, width)
    distance = _amount(amount) % size
    if distance == 0:
        return values
    return values[-distance:] + values[:-distance]


def rotate_right(
    source: Register,
    amount: int,
    width: int | None = None,
) -> tuple[QubitLike, ...]:
    """Return the LSB-first wiring for a constant right rotate."""

    values = register(source, "source")
    size = _width(values, width)
    distance = _amount(amount) % size
    if distance == 0:
        return values
    return values[distance:] + values[:distance]


# Common spellings used by lowering clients.
left_shift = shift_left
right_shift = shift_right
rotl = rotate_left
rotr = rotate_right
shift_left_wiring = shift_left
shift_right_wiring = shift_right
rotate_left_wiring = rotate_left
rotate_right_wiring = rotate_right


__all__ = [
    "Wire",
    "left_shift",
    "right_shift",
    "rotate_left",
    "rotate_left_wiring",
    "rotate_right",
    "rotate_right_wiring",
    "rotl",
    "rotr",
    "shift_left",
    "shift_left_wiring",
    "shift_right",
    "shift_right_wiring",
]
