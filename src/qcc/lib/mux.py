"""Reversible register select and controlled-XOR blocks.

All register arguments use LSB-first order.  The helpers XOR into their
result register, which makes them reversible for arbitrary initial result
bits.  If the result begins clean, ``select``/``mux`` produce a plain selected
copy.  No ancillas are required; the condition, source registers, and result
must be pairwise disjoint and have matching positive widths.

``select(condition, when_false, when_true, result)`` implements::

    result <- result xor ((not condition and when_false)
                          xor (condition and when_true))

Equivalently, a clean result receives ``when_false`` for condition zero and
``when_true`` for condition one.  ``controlled_xor_result`` implements the
smaller ``result ^= condition & source`` primitive used by the mux.
"""

from __future__ import annotations

from collections.abc import Sequence

from qcc.ir.netlist import QubitLike

from ._common import (
    Emitter,
    Register,
    disjoint,
    emit_ccx,
    emit_cx,
    one_qubit,
    register,
    same_width,
)


def controlled_xor_result(
    emitter: Emitter,
    condition: QubitLike | Sequence[QubitLike],
    source: Register,
    result: Register,
) -> Emitter:
    """Append ``result[i] ^= condition & source[i]`` for every bit."""

    control = one_qubit(condition, "condition")
    source_bits = register(source, "source")
    result_bits = register(result, "result")
    same_width(source_bits, result_bits)
    disjoint((control,), source_bits, result_bits, message="mux operands")
    for source_bit, result_bit in zip(source_bits, result_bits, strict=True):
        emit_ccx(emitter, control, source_bit, result_bit)
    return emitter


def select(
    emitter: Emitter,
    condition: QubitLike | Sequence[QubitLike],
    when_false: Register,
    when_true: Register,
    result: Register,
) -> Emitter:
    """XOR the selected source register into ``result``.

    ``when_false`` is selected when the condition is zero and ``when_true``
    when it is one.  A clean result therefore becomes the selected value.
    """

    control = one_qubit(condition, "condition")
    false_bits = register(when_false, "when_false")
    true_bits = register(when_true, "when_true")
    result_bits = register(result, "result")
    same_width(false_bits, true_bits, result_bits)
    disjoint((control,), false_bits, true_bits, result_bits, message="mux operands")

    # result ^= false xor condition & (false xor true).  Keeping the two
    # controlled terms separate avoids a negative-control gate and works for
    # both CircuitBuilder and the v0.1 GateBuffer.
    for false_bit, true_bit, result_bit in zip(
        false_bits, true_bits, result_bits, strict=True
    ):
        emit_cx(emitter, false_bit, result_bit)
        emit_ccx(emitter, control, false_bit, result_bit)
        emit_ccx(emitter, control, true_bit, result_bit)
    return emitter


def mux(
    emitter: Emitter,
    condition: QubitLike | Sequence[QubitLike],
    when_false: Register,
    when_true: Register,
    result: Register,
) -> Emitter:
    """Alias for :func:`select`."""

    return select(emitter, condition, when_false, when_true, result)


select_into = select
mux_into = select
controlled_xor = controlled_xor_result
xor_if = controlled_xor_result


__all__ = [
    "controlled_xor",
    "controlled_xor_result",
    "mux",
    "mux_into",
    "select",
    "select_into",
    "xor_if",
]
