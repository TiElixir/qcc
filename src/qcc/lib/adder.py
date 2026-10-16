"""Cuccaro ripple-carry arithmetic over LSB-first registers.

This module implements the majority/UnMajority-and-Add construction from
Steven A. Cuccaro, Thomas G. Draper, Samuel A. Kutin, and David Petrie
Moulton, *A new quantum ripple-carry addition circuit*,
arXiv:quant-ph/0410184.  The implementation uses the paper's simple
three-CNOT UMA form.  It is intentionally explicit and lowering-friendly:
callers append gates to a :class:`~qcc.ir.netlist.CircuitBuilder`, or use the
fragment helpers to obtain an immutable :class:`~qcc.ir.netlist.Circuit`.

All register arguments are sequences in little-endian order.  When its
workspace starts at zero, the modular operation maps ``(a, b, carry=0)`` to
``(a, a + b mod 2**n, carry=0)`` and needs a clean
carry workspace.  For a nonzero workspace the same reversible network treats
it as an incoming carry.  The carry-in/carry-out operation maps
``(a, b, cin, cout)`` to ``(a, a + b + cin mod 2**n, cin,
cout xor floor((a+b+cin)/2**n))``.

The simple construction has deterministic primitive counts:

* modular: ``2*n`` CCX and ``4*n`` CX gates;
* carry-in/carry-out: ``2*n`` CCX and ``4*n + 1`` CX gates.

The one-qubit carry workspace is returned to its input value by both
variants.  These counts deliberately describe the straightforward circuit,
not the paper's depth-optimized scheduling variant.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from operator import index as _index
from typing import TypeAlias

from qcc.ir.netlist import Circuit, CircuitBuilder, CircuitLayout, QubitLike

Register: TypeAlias = Sequence[QubitLike]


def _register(values: Iterable[QubitLike], name: str) -> tuple[QubitLike, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of qubits")
    result = tuple(values)
    if not result:
        raise ValueError(f"{name} must not be empty")
    numeric = tuple(int(_index(value)) for value in result)
    if any(value < 0 for value in numeric):
        raise ValueError(f"{name} contains a negative qubit index")
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
            raise ValueError(f"arithmetic operands must be disjoint ({rendered})")
        seen.update(numeric)


def _select_clean_carry(
    builder: CircuitBuilder,
    used: tuple[QubitLike, ...],
) -> QubitLike:
    if builder.layout is not None:
        occupied = {int(value) for value in used}
        for candidate in builder.layout.ancillas:
            if int(candidate) not in occupied:
                return candidate
    raise ValueError(
        "add_modular needs a clean carry qubit; pass carry=... or declare one "
        "in CircuitLayout.ancillas"
    )


def majority(
    builder: CircuitBuilder, a: QubitLike, b: QubitLike, carry: QubitLike
) -> CircuitBuilder:
    """Append the Cuccaro ``MAJ(a, b, carry)`` gate.

    The sequence is ``CX(a,b); CX(a,carry); CCX(b,carry,a)``.  In a ripple,
    the target ``a`` holds the next carry while the third wire eventually
    becomes the sum bit during the matching UMA pass.
    """

    return builder.cx(a, b).cx(a, carry).ccx(b, carry, a)


def unmajority_add(
    builder: CircuitBuilder,
    a: QubitLike,
    b: QubitLike,
    carry: QubitLike,
) -> CircuitBuilder:
    """Append the three-CNOT Cuccaro ``UMA`` inverse/addition block."""

    # This is the paper's 3-CNOT UMA form.  It is paired with MAJ in reverse
    # ripple order; it restores ``a`` and the incoming carry and writes sum to
    # ``b``.
    return builder.ccx(b, carry, a).cx(a, carry).cx(carry, b)


maj = majority
uma = unmajority_add


def _ripple_forward(
    builder: CircuitBuilder,
    a: tuple[QubitLike, ...],
    b: tuple[QubitLike, ...],
    carry_in: QubitLike,
) -> None:
    majority(builder, a[0], b[0], carry_in)
    for bit in range(1, len(a)):
        majority(builder, a[bit], b[bit], a[bit - 1])


def _ripple_backward(
    builder: CircuitBuilder,
    a: tuple[QubitLike, ...],
    b: tuple[QubitLike, ...],
    carry_in: QubitLike,
) -> None:
    for bit in range(len(a) - 1, 0, -1):
        unmajority_add(builder, a[bit], b[bit], a[bit - 1])
    unmajority_add(builder, a[0], b[0], carry_in)


def add_with_carry(
    builder: CircuitBuilder,
    a: Register,
    b: Register,
    carry_in: QubitLike | Sequence[QubitLike],
    carry_out: QubitLike | Sequence[QubitLike],
) -> CircuitBuilder:
    """Append in-place ``b += a + carry_in`` with an XORed carry-out.

    ``a`` and ``b`` must have the same nonzero width and all four operands
    must be disjoint.  ``carry_in`` and ``carry_out`` are preserved as input
    bits except that ``carry_out`` is toggled by the final carry.  Thus the
    operation is reversible even when the output bit starts at one.
    """

    aa = _register(a, "a")
    bb = _register(b, "b")
    cin = _one_qubit(carry_in, "carry_in")
    cout = _one_qubit(carry_out, "carry_out")
    _same_width(aa, bb)
    _disjoint(aa, bb, (cin,), (cout,))
    _ripple_forward(builder, aa, bb, cin)
    builder.cx(aa[-1], cout)
    _ripple_backward(builder, aa, bb, cin)
    return builder


full_adder = add_with_carry


def add_modular(
    builder: CircuitBuilder,
    a: Register,
    b: Register,
    carry: QubitLike | Sequence[QubitLike] | None = None,
    *,
    carry_in: QubitLike | Sequence[QubitLike] | None = None,
) -> CircuitBuilder:
    """Append ``b <- a + b (mod 2**n)`` using one clean carry workspace.

    The carry workspace is restored exactly, so it can be safely reused by a
    surrounding Bennett schedule.  ``carry`` and ``carry_in`` are aliases;
    if both are omitted, the first declared layout ancilla is selected.  An
    explicit qubit is preferable for lowering because it makes ownership and
    clean-state requirements visible at the call site.
    """

    if carry is not None and carry_in is not None:
        raise ValueError("carry and carry_in are aliases; supply only one")
    aa = _register(a, "a")
    bb = _register(b, "b")
    _same_width(aa, bb)
    selected = carry if carry is not None else carry_in
    if selected is None:
        selected = _select_clean_carry(builder, aa + bb)
    cin = _one_qubit(selected, "carry")
    _disjoint(aa, bb, (cin,))
    _ripple_forward(builder, aa, bb, cin)
    _ripple_backward(builder, aa, bb, cin)
    return builder


add_modular_into = add_modular
modular_adder_into = add_modular
ripple_add = add_modular


def cuccaro_adder(
    builder: CircuitBuilder,
    a: Register,
    b: Register,
    *,
    carry: QubitLike | Sequence[QubitLike] | None = None,
    carry_out: QubitLike | Sequence[QubitLike] | None = None,
) -> CircuitBuilder:
    """Dispatch to the modular or carry-out Cuccaro variant."""

    if carry_out is None:
        return add_modular(builder, a, b, carry=carry)
    if carry is None:
        raise ValueError("carry is required when carry_out is requested")
    return add_with_carry(builder, a, b, carry, carry_out)


def copy_register(
    builder: CircuitBuilder,
    source: Register,
    target: Register,
) -> CircuitBuilder:
    """XOR ``source`` into ``target`` bitwise with CNOTs.

    If ``target`` starts clean this is a classical register copy; in general
    it is the reversible operation ``target ^= source``.
    """

    src = _register(source, "source")
    dst = _register(target, "target")
    _same_width(src, dst)
    _disjoint(src, dst)
    for source_bit, target_bit in zip(src, dst):
        builder.cx(source_bit, target_bit)
    return builder


cnot_copy = copy_register
xor_register = copy_register


def controlled_xor(
    builder: CircuitBuilder,
    control: QubitLike,
    source: Register,
    target: Register,
) -> CircuitBuilder:
    """Append ``target ^= source`` controlled by one additional qubit."""

    src = _register(source, "source")
    dst = _register(target, "target")
    control_bit = _one_qubit(control, "control")
    _same_width(src, dst)
    _disjoint((control_bit,), src, dst)
    for source_bit, target_bit in zip(src, dst):
        builder.ccx(control_bit, source_bit, target_bit)
    return builder


xor_controlled = controlled_xor


def controlled_xor_value(
    builder: CircuitBuilder,
    control: QubitLike,
    value: int,
    target: Register,
) -> CircuitBuilder:
    """XOR a compile-time constant into ``target`` when ``control`` is 1."""

    target_bits = _register(target, "target")
    value = int(_index(value))
    if value < 0 or value >= (1 << len(target_bits)):
        raise ValueError("controlled value does not fit in target width")
    control_bit = _one_qubit(control, "control")
    _disjoint((control_bit,), target_bits)
    for bit, target_bit in enumerate(target_bits):
        if (value >> bit) & 1:
            builder.cx(control_bit, target_bit)
    return builder


xor_controlled_value = controlled_xor_value


def _fragment_layout(
    a: tuple[QubitLike, ...],
    b: tuple[QubitLike, ...],
    carry: QubitLike,
    *,
    num_qubits: int,
    layout: CircuitLayout | None,
) -> CircuitLayout | None:
    if layout is not None:
        if layout.num_qubits != num_qubits:
            raise ValueError("fragment layout width does not match its operands")
        return layout
    return CircuitLayout({"a": a, "b": b}, ancillas=(carry,), num_qubits=num_qubits)


def modular_adder(
    a: Register,
    b: Register,
    carry: QubitLike | None = None,
    *,
    num_qubits: int | None = None,
    layout: CircuitLayout | None = None,
) -> Circuit:
    """Return an immutable modular-adder fragment.

    If ``carry`` is omitted, the next wire after the operands is reserved as a
    clean carry workspace.  Use :func:`add_modular` when appending to an
    existing builder and explicit workspace ownership matters.
    """

    aa = _register(a, "a")
    bb = _register(b, "b")
    _same_width(aa, bb)
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
    add_modular(builder, aa, bb, cin)
    return builder.build()


def add_fragment_with_carry(
    a: Register,
    b: Register,
    carry_in: QubitLike,
    carry_out: QubitLike,
    *,
    num_qubits: int | None = None,
    layout: CircuitLayout | None = None,
) -> Circuit:
    """Return an immutable carry-in/carry-out Cuccaro fragment."""

    aa = _register(a, "a")
    bb = _register(b, "b")
    cin = _one_qubit(carry_in, "carry_in")
    cout = _one_qubit(carry_out, "carry_out")
    _same_width(aa, bb)
    _disjoint(aa, bb, (cin,), (cout,))
    width = max((int(value) for value in aa + bb + (cin, cout)), default=-1) + 1
    if num_qubits is not None:
        width = int(_index(num_qubits))
        if width <= max(int(value) for value in aa + bb + (cin, cout)):
            raise ValueError("fragment width is smaller than an operand")
    fragment_layout = layout
    if fragment_layout is None:
        fragment_layout = CircuitLayout(
            {"a": aa, "b": bb, "carry_in": (cin,), "carry_out": (cout,)},
            num_qubits=width,
        )
    builder = CircuitBuilder(width, layout=fragment_layout, auto_grow=False)
    add_with_carry(builder, aa, bb, cin, cout)
    return builder.build()


def resource_formula(width: int, variant: str = "modular") -> dict[str, int]:
    """Return the simple construction's primitive-gate formula."""

    return _resource_formula(width, variant)


def _resource_formula(width: int, variant: str = "modular") -> dict[str, int]:
    width = int(_index(width))
    if width <= 0:
        raise ValueError("width must be positive")
    normalized = variant.lower().replace("_", "-")
    if normalized in {"modular", "mod"}:
        cx = 4 * width
    elif normalized in {"carry", "carry-out", "full", "incoming-carry"}:
        cx = 4 * width + 1
    else:
        raise ValueError(f"unknown adder variant {variant!r}")
    ccx = 2 * width
    return {"x": 0, "cx": cx, "ccx": ccx, "mcx": 0, "gates": cx + ccx}


__all__ = [
    "add_fragment_with_carry",
    "add_modular",
    "add_modular_into",
    "add_with_carry",
    "cnot_copy",
    "controlled_xor",
    "controlled_xor_value",
    "copy_register",
    "cuccaro_adder",
    "full_adder",
    "maj",
    "majority",
    "modular_adder",
    "modular_adder_into",
    "resource_formula",
    "ripple_add",
    "uma",
    "unmajority_add",
    "xor_controlled",
    "xor_controlled_value",
    "xor_register",
]
