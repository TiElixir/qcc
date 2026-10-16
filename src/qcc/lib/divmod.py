"""Constant-divisor restoring unsigned division and remainder.

The network in this module implements unsigned ``divmod`` for a compile-time
constant divisor.  It preserves an ``n``-bit dividend and XORs the quotient
and remainder into clean-or-dirty ``n``-bit targets:

``constant_divmod(emitter, dividend, divisor, quotient, remainder, workspace)``
    ``quotient ^= dividend // divisor`` and
    ``remainder ^= dividend % divisor``.

A zero divisor is rejected because LLVM/C unsigned division by zero has
undefined/poison semantics.  A general runtime divisor is intentionally not a
part of this v0.3 contract.

The implementation is a reversible restoring-division pipeline.  Each of the
``n`` high-to-low dividend steps writes a fresh ``n+1``-bit partial remainder,
compares it against a materialized constant, records the quotient decision,
and conditionally subtracts the divisor.  The final quotient/remainder are
copied to the requested targets and the complete pipeline is reversed.  This
uses clean history registers rather than overwriting a partial remainder, which
makes the cleanup contract explicit and makes the block straightforward to use
inside Bennett schedules.

The workspace layout is, in order:

1. ``n+1`` remainder registers for each of the ``n+1`` pipeline states;
2. ``n`` quotient-decision bits;
3. one ``n+1``-bit constant-divisor register;
4. two comparison work bits;
5. one subtraction carry bit.

Use :func:`constant_divmod_workspace_width` to size a flat workspace.  The
workspace must start clean and is restored exactly.  The comparator and
controlled subtraction use the existing ``GateBuffer``/netlist APIs.  MCX is
allowed as an intermediate gate when the quotient bit controls a Toffoli; no
backend decomposition is assumed here.
"""

from __future__ import annotations

from collections.abc import Sequence
from operator import index as _index

from qcc.ir.netlist import CircuitBuilder, QubitLike
from qcc.lib.compare import unsigned_greater_equal

from ._common import (
    Emitter,
    Register,
    disjoint,
    emit_cx,
    register,
    same_width,
    workspace_register,
)
from .multiply import _append_controlled_addition, _emit_multi_controlled_x

_BASE_WORKSPACE_EXTRA = 3  # two comparison bits and one subtraction carry


def constant_divmod_workspace_width(width: int) -> int:
    """Return the required clean flat-workspace width for ``n``-bit divmod."""

    if isinstance(width, bool):
        raise TypeError("width must be a positive integer")
    value = int(_index(width))
    if value <= 0:
        raise ValueError("width must be positive")
    stage_width = value + 1
    return stage_width * stage_width + value + stage_width + _BASE_WORKSPACE_EXTRA


def _workspace(
    emitter: Emitter,
    used: tuple[QubitLike, ...],
    width: int,
    workspace: Sequence[QubitLike] | QubitLike | None,
    ancillas: Sequence[QubitLike] | QubitLike | None,
) -> tuple[
    tuple[tuple[QubitLike, ...], ...],
    tuple[QubitLike, ...],
    tuple[QubitLike, ...],
    tuple[QubitLike, QubitLike],
    QubitLike,
]:
    if workspace is not None and ancillas is not None:
        raise ValueError("workspace and ancillas are aliases; supply only one")
    selected = workspace if workspace is not None else ancillas
    if selected is None:
        if not isinstance(emitter, CircuitBuilder) or emitter.layout is None:
            raise ValueError(
                "constant divmod needs clean workspace; pass workspace=(...)"
            )
        selected = emitter.layout.ancillas
    values = workspace_register(selected, "workspace")
    required = constant_divmod_workspace_width(width)
    if len(values) < required:
        raise ValueError(f"constant divmod needs {required} clean workspace qubits")
    stage_width = width + 1
    stage_count = width + 1
    stage_bits = stage_width * stage_count
    stages = tuple(
        tuple(values[offset : offset + stage_width])
        for offset in range(0, stage_bits, stage_width)
    )
    quotient_work = tuple(values[stage_bits : stage_bits + width])
    divisor_start = stage_bits + width
    divisor_bits = tuple(values[divisor_start : divisor_start + stage_width])
    compare_start = divisor_start + stage_width
    compare_bits = (values[compare_start], values[compare_start + 1])
    carry = values[compare_start + 2]
    workspace_bits = tuple(values[:required])
    disjoint(
        used,
        workspace_bits,
        message="constant divmod operands/workspace",
    )
    return stages, quotient_work, divisor_bits, compare_bits, carry


def _constant_value(divisor: int) -> int:
    if isinstance(divisor, bool):
        raise TypeError("divisor must be a nonzero integer constant")
    try:
        value = int(_index(divisor))
    except TypeError as exc:
        raise TypeError("divisor must be a nonzero integer constant") from exc
    if value == 0:
        raise ValueError("constant divisor must be nonzero")
    if value < 0:
        raise ValueError("constant divisor must be positive")
    return value


def _copy_register(
    emitter: Emitter, source: Sequence[QubitLike], target: Sequence[QubitLike]
) -> None:
    for source_bit, target_bit in zip(source, target, strict=True):
        emit_cx(emitter, source_bit, target_bit)


def _append_constant_divmod(
    emitter: Emitter,
    dividend: tuple[QubitLike, ...],
    divisor: int,
    quotient: tuple[QubitLike, ...],
    remainder: tuple[QubitLike, ...],
    workspace: Sequence[QubitLike] | QubitLike | None,
    ancillas: Sequence[QubitLike] | QubitLike | None,
) -> Emitter:
    width = len(dividend)
    modulus = 1 << width
    if divisor == 1:
        _copy_register(emitter, dividend, quotient)
        return emitter
    if divisor >= modulus:
        _copy_register(emitter, dividend, remainder)
        return emitter

    stages, quotient_work, divisor_bits, compare_bits, carry = _workspace(
        emitter,
        dividend + quotient + remainder,
        width,
        workspace,
        ancillas,
    )
    start = len(emitter)
    for bit, qubit in enumerate(divisor_bits):
        if (divisor >> bit) & 1:
            emitter.x(qubit)

    current = stages[0]
    for input_index in range(width - 1, -1, -1):
        next_remainder = stages[width - input_index]
        # The invariant after each subtraction is R < divisor < 2**n, so the
        # top bit of the current state is known to be zero.  Copying only the
        # low n bits performs the restoring shift into a fresh register.
        for bit in range(width):
            emit_cx(emitter, current[bit], next_remainder[bit + 1])
        emit_cx(emitter, dividend[input_index], next_remainder[0])
        quotient_bit = quotient_work[input_index]
        unsigned_greater_equal(
            emitter,
            next_remainder,
            divisor_bits,
            quotient_bit,
            ancillas=compare_bits,
        )
        for target_bit in next_remainder:
            _emit_multi_controlled_x(emitter, (quotient_bit,), target_bit)
        _append_controlled_addition(
            emitter,
            divisor_bits,
            next_remainder,
            carry,
            (quotient_bit,),
        )
        for target_bit in next_remainder:
            _emit_multi_controlled_x(emitter, (quotient_bit,), target_bit)
        current = next_remainder

    computation_stop = len(emitter)
    _copy_register(emitter, quotient_work, quotient)
    _copy_register(emitter, current[:width], remainder)
    # The copy targets are deliberately outside this block.  Reversing only
    # the computation therefore clears all history, the constant register,
    # comparisons, carries, and quotient-decision bits without touching the
    # outputs.
    from ._common import reverse_block

    reverse_block(emitter, start, computation_stop)
    return emitter


def constant_divmod(
    emitter: Emitter,
    dividend: Register,
    divisor: int,
    quotient: Register,
    remainder: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
) -> Emitter:
    """Append constant-divisor unsigned quotient and remainder computation.

    ``dividend``, ``quotient``, and ``remainder`` have the same positive width.
    The dividend is preserved and each output is XOR-updated, so dirty output
    targets are valid.  ``divisor`` must be a positive compile-time integer;
    runtime/general divisors are outside this API.
    """

    dividend_bits = register(dividend, "dividend")
    quotient_bits = register(quotient, "quotient")
    remainder_bits = register(remainder, "remainder")
    same_width(dividend_bits, quotient_bits, remainder_bits)
    value = _constant_value(divisor)
    disjoint(
        dividend_bits,
        quotient_bits,
        remainder_bits,
        message="constant divmod operands",
    )
    return _append_constant_divmod(
        emitter,
        dividend_bits,
        value,
        quotient_bits,
        remainder_bits,
        workspace,
        ancillas,
    )


def constant_division(
    emitter: Emitter,
    dividend: Register,
    divisor: int,
    quotient: Register,
    remainder: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
) -> Emitter:
    """Division-named wrapper that also supplies the clean remainder target."""

    return constant_divmod(
        emitter,
        dividend,
        divisor,
        quotient,
        remainder,
        workspace,
        ancillas=ancillas,
    )


def constant_remainder(
    emitter: Emitter,
    dividend: Register,
    divisor: int,
    quotient: Register,
    remainder: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
) -> Emitter:
    """Remainder-named wrapper that cleans a supplied quotient target too."""

    return constant_divmod(
        emitter,
        dividend,
        divisor,
        quotient,
        remainder,
        workspace,
        ancillas=ancillas,
    )


# Names used by callers that prefer the operation order in the name.
constant_unsigned_divmod = constant_divmod
constant_unsigned_division = constant_division
constant_unsigned_remainder = constant_remainder
restoring_divmod = constant_divmod
divmod_constant = constant_divmod
unsigned_divmod = constant_divmod
divide_by_constant = constant_division
remainder_by_constant = constant_remainder
udiv_constant = constant_division
urem_constant = constant_remainder
restore_division = constant_division
restore_remainder = constant_remainder
restoring_division = constant_division
restoring_remainder = constant_remainder
divmod_by_constant = constant_divmod

divmod_workspace_width = constant_divmod_workspace_width


__all__ = [
    "constant_division",
    "constant_divmod",
    "constant_divmod_workspace_width",
    "constant_remainder",
    "constant_unsigned_division",
    "constant_unsigned_divmod",
    "constant_unsigned_remainder",
    "divide_by_constant",
    "divmod_by_constant",
    "divmod_constant",
    "divmod_workspace_width",
    "remainder_by_constant",
    "restore_division",
    "restore_remainder",
    "restoring_division",
    "restoring_remainder",
    "restoring_divmod",
    "udiv_constant",
    "unsigned_divmod",
    "urem_constant",
]
