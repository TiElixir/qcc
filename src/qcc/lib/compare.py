"""Reversible equality and unsigned comparison blocks.

The public helpers append gates to an existing :class:`CircuitBuilder` or
``GateBuffer``.  Registers are sequences in LSB-first order.  Every predicate
is an XOR-style flag operation: a clean output bit becomes the predicate, and
an arbitrary output bit is toggled by the predicate.

Comparison blocks use two clean ancillas.  The first is a clean borrow-in bit
and the second is a clean borrow-out workspace.  The borrow computation is
followed by a CNOT into the requested flag and then exact gate reversal, so
the two source registers and both ancillas have their input values at the end.
When ``ancillas`` is omitted, the first two non-overlapping ancillas declared
by a ``CircuitLayout`` are used.  A ``GateBuffer`` has no layout, so it
requires an explicit two-qubit workspace.

For unsigned ``n``-bit values ``a`` and ``b``, the implemented formulas are::

    lt = [a < b]
    gt = [a > b]
    eq = 1 xor lt xor gt
    le = 1 xor gt
    ge = 1 xor lt

The formulas are applied as reversible XOR updates to each requested flag.
"""

from __future__ import annotations

from collections.abc import Sequence

from qcc.ir.netlist import CircuitBuilder, QubitLike

from ._common import (
    Emitter,
    Register,
    disjoint,
    emit_ccx,
    emit_cx,
    emit_x,
    one_qubit,
    register,
    reverse_block,
    same_width,
    workspace_register,
)

Ancillas = Sequence[QubitLike]
Output = QubitLike | Sequence[QubitLike]


def _output(
    positional: Output | None,
    *,
    result: Output | None,
    target: Output | None,
    name: str,
) -> QubitLike:
    supplied = [value for value in (positional, result, target) if value is not None]
    if len(supplied) != 1:
        raise TypeError(f"supply exactly one {name} using flag, result, or target")
    return one_qubit(supplied[0], name)


def _workspace(
    emitter: Emitter,
    used: tuple[QubitLike, ...],
    ancillas: Ancillas | QubitLike | None,
    workspace: Ancillas | QubitLike | None,
) -> tuple[QubitLike, QubitLike]:
    if ancillas is not None and workspace is not None:
        raise ValueError("ancillas and workspace are aliases; supply only one")
    selected: Ancillas | QubitLike | None = (
        ancillas if ancillas is not None else workspace
    )
    if selected is None:
        if not isinstance(emitter, CircuitBuilder) or emitter.layout is None:
            raise ValueError(
                "comparison needs two clean ancillas; pass ancillas=(q0, q1)"
            )
        selected = emitter.layout.ancillas

    values = workspace_register(selected, "ancillas")
    if len(values) < 2:
        raise ValueError("comparison needs at least two clean ancillas")
    disjoint(used, values, message="comparison operands and ancillas")
    return values[0], values[1]


def _majority(
    emitter: Emitter,
    a: QubitLike,
    b: QubitLike,
    carry: QubitLike,
) -> None:
    emit_cx(emitter, a, b)
    emit_cx(emitter, a, carry)
    emit_ccx(emitter, b, carry, a)


def _unmajority_add(
    emitter: Emitter,
    a: QubitLike,
    b: QubitLike,
    carry: QubitLike,
) -> None:
    emit_ccx(emitter, b, carry, a)
    emit_cx(emitter, a, carry)
    emit_cx(emitter, carry, b)


def _borrow_block(
    emitter: Emitter,
    subtrahend: tuple[QubitLike, ...],
    target: tuple[QubitLike, ...],
    borrow_in: QubitLike,
    borrow_out: QubitLike,
) -> None:
    """Append target subtraction with an XORed borrow-out workspace."""

    for target_bit in target:
        emit_x(emitter, target_bit)

    _majority(emitter, subtrahend[0], target[0], borrow_in)
    for bit in range(1, len(subtrahend)):
        _majority(emitter, subtrahend[bit], target[bit], subtrahend[bit - 1])
    emit_cx(emitter, subtrahend[-1], borrow_out)
    for bit in range(len(subtrahend) - 1, 0, -1):
        _unmajority_add(emitter, subtrahend[bit], target[bit], subtrahend[bit - 1])
    _unmajority_add(emitter, subtrahend[0], target[0], borrow_in)

    for target_bit in target:
        emit_x(emitter, target_bit)


def _append_borrow_flag(
    emitter: Emitter,
    subtrahend: tuple[QubitLike, ...],
    target: tuple[QubitLike, ...],
    flag: QubitLike,
    borrow_in: QubitLike,
    borrow_out: QubitLike,
) -> None:
    """Toggle ``flag`` iff ``target < subtrahend`` and clean the block."""

    start = len(emitter)
    _borrow_block(emitter, subtrahend, target, borrow_in, borrow_out)
    stop = len(emitter)
    emit_cx(emitter, borrow_out, flag)
    reverse_block(emitter, start, stop)


def _register_pair(
    left: Register, right: Register
) -> tuple[tuple[QubitLike, ...], tuple[QubitLike, ...]]:
    aa = register(left, "left")
    bb = register(right, "right")
    same_width(aa, bb)
    disjoint(aa, bb, message="comparison operands")
    return aa, bb


def _strict_less(
    emitter: Emitter,
    left: tuple[QubitLike, ...],
    right: tuple[QubitLike, ...],
    flag: QubitLike,
    workspace: tuple[QubitLike, QubitLike],
) -> None:
    disjoint(left, right, (flag,), workspace, message="comparison operands")
    _append_borrow_flag(emitter, right, left, flag, *workspace)


def _strict_greater(
    emitter: Emitter,
    left: tuple[QubitLike, ...],
    right: tuple[QubitLike, ...],
    flag: QubitLike,
    workspace: tuple[QubitLike, QubitLike],
) -> None:
    disjoint(left, right, (flag,), workspace, message="comparison operands")
    _append_borrow_flag(emitter, left, right, flag, *workspace)


def bitwise_equal(
    emitter: Emitter,
    left: Register,
    right: Register,
    flag: Output | None = None,
    ancillas: Ancillas | QubitLike | None = None,
    *,
    result: Output | None = None,
    target: Output | None = None,
    workspace: Ancillas | QubitLike | None = None,
) -> Emitter:
    """Toggle ``flag`` iff two same-width LSB-first registers are equal.

    ``flag`` is XOR-updated, so it must be clean only when a plain predicate
    output is wanted.  ``ancillas`` must contain at least two clean,
    non-overlapping qubits; those qubits are restored to their entry values.
    """

    aa, bb = _register_pair(left, right)
    output = _output(flag, result=result, target=target, name="equality flag")
    borrow_in, borrow_out = _workspace(
        emitter, aa + bb + (output,), ancillas, workspace
    )
    emit_x(emitter, output)
    _strict_less(emitter, aa, bb, output, (borrow_in, borrow_out))
    _strict_greater(emitter, aa, bb, output, (borrow_in, borrow_out))
    return emitter


def unsigned_less_than(
    emitter: Emitter,
    left: Register,
    right: Register,
    flag: Output | None = None,
    ancillas: Ancillas | QubitLike | None = None,
    *,
    result: Output | None = None,
    target: Output | None = None,
    workspace: Ancillas | QubitLike | None = None,
) -> Emitter:
    """Toggle ``flag`` iff unsigned ``left < right``."""

    aa, bb = _register_pair(left, right)
    output = _output(flag, result=result, target=target, name="less-than flag")
    borrow_in, borrow_out = _workspace(
        emitter, aa + bb + (output,), ancillas, workspace
    )
    _strict_less(emitter, aa, bb, output, (borrow_in, borrow_out))
    return emitter


def unsigned_greater_than(
    emitter: Emitter,
    left: Register,
    right: Register,
    flag: Output | None = None,
    ancillas: Ancillas | QubitLike | None = None,
    *,
    result: Output | None = None,
    target: Output | None = None,
    workspace: Ancillas | QubitLike | None = None,
) -> Emitter:
    """Toggle ``flag`` iff unsigned ``left > right``."""

    aa, bb = _register_pair(left, right)
    output = _output(flag, result=result, target=target, name="greater-than flag")
    borrow_in, borrow_out = _workspace(
        emitter, aa + bb + (output,), ancillas, workspace
    )
    _strict_greater(emitter, aa, bb, output, (borrow_in, borrow_out))
    return emitter


def unsigned_less_equal(
    emitter: Emitter,
    left: Register,
    right: Register,
    flag: Output | None = None,
    ancillas: Ancillas | QubitLike | None = None,
    *,
    result: Output | None = None,
    target: Output | None = None,
    workspace: Ancillas | QubitLike | None = None,
) -> Emitter:
    """Toggle ``flag`` iff unsigned ``left <= right``."""

    aa, bb = _register_pair(left, right)
    output = _output(flag, result=result, target=target, name="less-equal flag")
    borrow_in, borrow_out = _workspace(
        emitter, aa + bb + (output,), ancillas, workspace
    )
    emit_x(emitter, output)
    _strict_greater(emitter, aa, bb, output, (borrow_in, borrow_out))
    return emitter


def unsigned_greater_equal(
    emitter: Emitter,
    left: Register,
    right: Register,
    flag: Output | None = None,
    ancillas: Ancillas | QubitLike | None = None,
    *,
    result: Output | None = None,
    target: Output | None = None,
    workspace: Ancillas | QubitLike | None = None,
) -> Emitter:
    """Toggle ``flag`` iff unsigned ``left >= right``."""

    aa, bb = _register_pair(left, right)
    output = _output(flag, result=result, target=target, name="greater-equal flag")
    borrow_in, borrow_out = _workspace(
        emitter, aa + bb + (output,), ancillas, workspace
    )
    emit_x(emitter, output)
    _strict_less(emitter, aa, bb, output, (borrow_in, borrow_out))
    return emitter


def unsigned_not_equal(
    emitter: Emitter,
    left: Register,
    right: Register,
    flag: Output | None = None,
    ancillas: Ancillas | QubitLike | None = None,
    *,
    result: Output | None = None,
    target: Output | None = None,
    workspace: Ancillas | QubitLike | None = None,
) -> Emitter:
    """Toggle ``flag`` iff unsigned ``left != right``."""

    aa, bb = _register_pair(left, right)
    output = _output(flag, result=result, target=target, name="not-equal flag")
    borrow_in, borrow_out = _workspace(
        emitter, aa + bb + (output,), ancillas, workspace
    )
    _strict_less(emitter, aa, bb, output, (borrow_in, borrow_out))
    _strict_greater(emitter, aa, bb, output, (borrow_in, borrow_out))
    return emitter


def compare_flags(
    emitter: Emitter,
    left: Register,
    right: Register,
    less: Output | None = None,
    equal: Output | None = None,
    greater: Output | None = None,
    ancillas: Ancillas | QubitLike | None = None,
    *,
    lt: Output | None = None,
    eq: Output | None = None,
    gt: Output | None = None,
    workspace: Ancillas | QubitLike | None = None,
) -> Emitter:
    """Append any requested ``less``, ``equal``, and ``greater`` flags.

    The three output arguments are XOR targets and must be distinct from one
    another, the inputs, and the two clean workspace qubits.  One workspace
    pair is reused and restored between the requested predicates.
    """

    if less is not None and lt is not None:
        raise ValueError("less and lt are aliases; supply only one")
    if equal is not None and eq is not None:
        raise ValueError("equal and eq are aliases; supply only one")
    if greater is not None and gt is not None:
        raise ValueError("greater and gt are aliases; supply only one")
    less_output = less if less is not None else lt
    equal_output = equal if equal is not None else eq
    greater_output = greater if greater is not None else gt
    if less_output is None and equal_output is None and greater_output is None:
        raise ValueError("request at least one comparison flag")

    aa, bb = _register_pair(left, right)
    output_values = [
        _output(value, result=None, target=None, name=name)
        for value, name in (
            (less_output, "less-than flag"),
            (equal_output, "equality flag"),
            (greater_output, "greater-than flag"),
        )
        if value is not None
    ]
    disjoint(
        aa,
        bb,
        *((value,) for value in output_values),
        message="comparison operands and flags",
    )
    outputs = tuple(output_values)
    borrow_in, borrow_out = _workspace(emitter, aa + bb + outputs, ancillas, workspace)

    if less_output is not None:
        less_flag = _output(
            less_output, result=None, target=None, name="less-than flag"
        )
        _strict_less(emitter, aa, bb, less_flag, (borrow_in, borrow_out))
    if greater_output is not None:
        greater_flag = _output(
            greater_output, result=None, target=None, name="greater-than flag"
        )
        _strict_greater(emitter, aa, bb, greater_flag, (borrow_in, borrow_out))
    if equal_output is not None:
        equal_flag = _output(
            equal_output, result=None, target=None, name="equality flag"
        )
        emit_x(emitter, equal_flag)
        _strict_less(emitter, aa, bb, equal_flag, (borrow_in, borrow_out))
        _strict_greater(emitter, aa, bb, equal_flag, (borrow_in, borrow_out))
    return emitter


# Readable aliases used by callers that prefer predicate names without the
# ``unsigned_`` prefix.  The explicit names above remain the canonical API.
equal = bitwise_equal
bitwise_equality = bitwise_equal
compare_equal = bitwise_equal
less_than = unsigned_less_than
less_equal = unsigned_less_equal
greater_than = unsigned_greater_than
greater_equal = unsigned_greater_equal
unsigned_lt = unsigned_less_than
unsigned_le = unsigned_less_equal
unsigned_gt = unsigned_greater_than
unsigned_ge = unsigned_greater_equal
unsigned_compare = compare_flags
compare_unsigned = compare_flags


__all__ = [
    "bitwise_equal",
    "bitwise_equality",
    "compare_equal",
    "compare_flags",
    "compare_unsigned",
    "equal",
    "greater_equal",
    "greater_than",
    "less_equal",
    "less_than",
    "unsigned_compare",
    "unsigned_ge",
    "unsigned_greater_equal",
    "unsigned_greater_than",
    "unsigned_gt",
    "unsigned_le",
    "unsigned_less_equal",
    "unsigned_less_than",
    "unsigned_lt",
    "unsigned_not_equal",
]
