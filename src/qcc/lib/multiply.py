"""Reversible shift-and-add multiplication blocks.

Registers are little-endian sequences of qubits.  The public multiplication
helpers preserve both operands and add the requested product into an accumulator
target:

``multiply_modular``
    ``target <- target + (a * b) mod 2**n`` for three ``n``-bit registers.

``multiply_widening``
    ``target <- target + a * b mod 2**(2*n)`` for ``n``-bit operands and a
    ``2*n``-bit target.

The implementation is the ordinary shift-and-add algorithm.  Each partial
product is copied into a clean temporary addend register and accumulated with
a Cuccaro-style ripple adder.  The addend and carry are clean after every
partial product, so the whole block is safe to place inside a Bennett
compute/copy/uncompute schedule.

Controlled addends use the existing netlist ``MCX`` operation as an
intermediate gate.  ``GateBuffer`` and :class:`~qcc.ir.netlist.CircuitBuilder`
both preserve that operation; a backend may decompose it later.  The library
contract does not require an MCX decomposition or a particular primitive-gate
count.

The explicit workspace contract is deliberately visible: a multiplication
needs a clean addend register with the target width and one clean carry bit.
Pass them as ``workspace=(addend_bits..., carry_bit)`` (or ``ancillas=``), or
declare enough clean ancillas in a ``CircuitBuilder`` layout.  The workspace
is restored exactly, while the target is updated modulo its width and may
start dirty.  A clean target therefore receives the product.
Operand registers and the target/control must be pairwise disjoint.  The
lowering pass handles an SSA expression such as ``x * x`` by making a clean
copy before calling this block.
"""

from __future__ import annotations

from collections.abc import Sequence
from operator import index as _index

from qcc.ir.netlist import CircuitBuilder, QubitLike

from ._common import (
    Emitter,
    Register,
    disjoint,
    emit_ccx,
    emit_cx,
    emit_x,
    register,
    reverse_block,
    same_width,
    workspace_register,
)


def _controls(
    value: QubitLike | Sequence[QubitLike] | None,
    name: str = "control",
) -> tuple[QubitLike, ...]:
    if value is None:
        return ()
    return workspace_register(value, name)


def _emit_multi_controlled_x(
    emitter: Emitter,
    controls: Sequence[QubitLike],
    target: QubitLike,
) -> None:
    """Emit an X with zero or more controls using the common emitter API."""

    control_bits = tuple(controls)
    if not control_bits:
        emit_x(emitter, target)
    elif len(control_bits) == 1:
        emit_cx(emitter, control_bits[0], target)
    elif len(control_bits) == 2:
        emit_ccx(emitter, control_bits[0], control_bits[1], target)
    else:
        emitter.mcx(control_bits, target)


def _emit_controlled_cx(
    emitter: Emitter,
    controls: Sequence[QubitLike],
    control: QubitLike,
    target: QubitLike,
) -> None:
    """Emit ``controls & control -> target`` as one reversible toggle."""

    _emit_multi_controlled_x(emitter, (*controls, control), target)


def _emit_controlled_ccx(
    emitter: Emitter,
    controls: Sequence[QubitLike],
    control_a: QubitLike,
    control_b: QubitLike,
    target: QubitLike,
) -> None:
    """Emit a controlled Toffoli, retaining MCX when it has many controls."""

    _emit_multi_controlled_x(emitter, (*controls, control_a, control_b), target)


def _append_controlled_addition(
    emitter: Emitter,
    source: Register,
    target: Register,
    carry: QubitLike,
    controls: Sequence[QubitLike] = (),
) -> Emitter:
    """Append ``target += source`` modulo the width under ``controls``.

        ``source`` and ``target`` are preserved/updated respectively, and the
        clean carry is restored.  The operation is a direct controlled version of
    the Cuccaro MAJ/UMA ripple.  It is shared with constant-divisor restoring
        division, where the quotient bit controls a modular subtraction.
    """

    source_bits = register(source, "source")
    target_bits = register(target, "target")
    same_width(source_bits, target_bits)
    carry_bit = workspace_register(carry, "carry")
    if len(carry_bit) != 1:
        raise ValueError("carry must be one qubit")
    control_bits = register(controls, "controls") if controls else ()
    disjoint(
        source_bits,
        target_bits,
        carry_bit,
        control_bits,
        message="controlled addition operands",
    )
    incoming = carry_bit[0]

    def majority(first: QubitLike, second: QubitLike, carry_wire: QubitLike) -> None:
        _emit_controlled_cx(emitter, control_bits, first, second)
        _emit_controlled_cx(emitter, control_bits, first, carry_wire)
        _emit_controlled_ccx(emitter, control_bits, second, carry_wire, first)

    def unmajority_add(
        first: QubitLike, second: QubitLike, carry_wire: QubitLike
    ) -> None:
        _emit_controlled_ccx(emitter, control_bits, second, carry_wire, first)
        _emit_controlled_cx(emitter, control_bits, first, carry_wire)
        _emit_controlled_cx(emitter, control_bits, carry_wire, second)

    majority(source_bits[0], target_bits[0], incoming)
    for bit in range(1, len(source_bits)):
        majority(source_bits[bit], target_bits[bit], source_bits[bit - 1])
    for bit in range(len(source_bits) - 1, 0, -1):
        unmajority_add(source_bits[bit], target_bits[bit], source_bits[bit - 1])
    unmajority_add(source_bits[0], target_bits[0], incoming)
    return emitter


def controlled_add_modular(
    emitter: Emitter,
    control: QubitLike | Sequence[QubitLike],
    source: Register,
    target: Register,
    carry: QubitLike,
) -> Emitter:
    """Append a controlled modular addition ``target += source``.

    This small public primitive is useful to callers composing arithmetic
    blocks.  Multiple controls are accepted as a sequence.
    """

    return _append_controlled_addition(
        emitter,
        source,
        target,
        carry,
        _controls(control, "control"),
    )


def _workspace(
    emitter: Emitter,
    used: tuple[QubitLike, ...],
    width: int,
    workspace: Sequence[QubitLike] | QubitLike | None,
    ancillas: Sequence[QubitLike] | QubitLike | None,
    carry: QubitLike | None,
) -> tuple[tuple[QubitLike, ...], QubitLike]:
    if workspace is not None and ancillas is not None:
        raise ValueError("workspace and ancillas are aliases; supply only one")
    selected = workspace if workspace is not None else ancillas
    if selected is None:
        if not isinstance(emitter, CircuitBuilder) or emitter.layout is None:
            raise ValueError(
                "multiplication needs a clean workspace; pass workspace=(...)"
            )
        selected = emitter.layout.ancillas

    values = workspace_register(selected, "workspace")
    if carry is None:
        if len(values) < width + 1:
            raise ValueError(f"multiplication needs {width + 1} clean workspace qubits")
        addend = values[:width]
        carry_bit = values[width]
    else:
        carry_values = workspace_register(carry, "carry")
        if len(carry_values) != 1:
            raise ValueError("carry must be one qubit")
        if len(values) < width:
            raise ValueError(
                f"multiplication needs {width} clean addend workspace qubits"
            )
        addend = values[:width]
        carry_bit = carry_values[0]
    disjoint(used, addend, (carry_bit,), message="multiplication operands/workspace")
    return addend, carry_bit


def _append_shifted_add(
    emitter: Emitter,
    source: tuple[QubitLike, ...],
    shift: int,
    addend: tuple[QubitLike, ...],
    target: tuple[QubitLike, ...],
    carry: QubitLike,
    controls: Sequence[QubitLike],
) -> None:
    """Copy one shifted partial product, add it, and clear the copy."""

    copied = min(len(source), len(addend) - shift)
    if copied <= 0:
        return
    for source_index in range(copied):
        emit_cx(emitter, source[source_index], addend[shift + source_index])
    _append_controlled_addition(emitter, addend, target, carry, controls)
    for source_index in reversed(range(copied)):
        emit_cx(emitter, source[source_index], addend[shift + source_index])


def _append_shifted_controlled_addition(
    emitter: Emitter,
    source: tuple[QubitLike, ...],
    target: tuple[QubitLike, ...],
    shift: int,
    carry: QubitLike,
    controls: Sequence[QubitLike],
) -> None:
    """Add ``source << shift`` into a wider target with one clean carry bit."""

    if shift < 0 or shift + len(source) >= len(target) + 1:
        raise ValueError("shifted add does not fit in the target register")
    target_slice = target[shift : shift + len(source)]
    control_bits = tuple(controls)

    def majority(first: QubitLike, second: QubitLike, incoming: QubitLike) -> None:
        _emit_controlled_cx(emitter, control_bits, first, second)
        _emit_controlled_cx(emitter, control_bits, first, incoming)
        _emit_controlled_ccx(emitter, control_bits, second, incoming, first)

    def unmajority_add(
        first: QubitLike, second: QubitLike, incoming: QubitLike
    ) -> None:
        _emit_controlled_ccx(emitter, control_bits, second, incoming, first)
        _emit_controlled_cx(emitter, control_bits, first, incoming)
        _emit_controlled_cx(emitter, control_bits, incoming, second)

    majority(source[0], target_slice[0], carry)
    for index in range(1, len(source)):
        majority(source[index], target_slice[index], source[index - 1])
    cout = target[shift + len(source)]
    _emit_controlled_cx(emitter, control_bits, source[-1], cout)
    for index in range(len(source) - 1, 0, -1):
        unmajority_add(source[index], target_slice[index], source[index - 1])
    unmajority_add(source[0], target_slice[0], carry)


def _multiply(
    emitter: Emitter,
    multiplicand: Register,
    multiplier: Register,
    target: Register,
    *,
    target_width: int,
    control: QubitLike | Sequence[QubitLike] | None,
    workspace: Sequence[QubitLike] | QubitLike | None,
    ancillas: Sequence[QubitLike] | QubitLike | None,
    carry: QubitLike | None,
) -> Emitter:
    first = register(multiplicand, "multiplicand")
    second = register(multiplier, "multiplier")
    result = register(target, "target")
    same_width(first, second)
    if len(result) != target_width:
        raise ValueError(f"target width must be {target_width}, got {len(result)}")
    control_bits = _controls(control, "control")
    disjoint(
        first,
        second,
        result,
        control_bits,
        message="multiplication operands and controls",
    )
    addend, carry_bit = _workspace(
        emitter,
        first + second + result + control_bits,
        target_width,
        workspace,
        ancillas,
        carry,
    )
    for shift in range(len(second)):
        _append_shifted_add(
            emitter,
            first,
            shift,
            addend,
            result,
            carry_bit,
            (*control_bits, second[shift]),
        )
    return emitter


def multiply_modular(
    emitter: Emitter,
    multiplicand: Register,
    multiplier: Register,
    target: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
    carry: QubitLike | None = None,
) -> Emitter:
    """Append ``target += multiplicand * multiplier (mod 2**n)``."""

    first = register(multiplicand, "multiplicand")
    second = register(multiplier, "multiplier")
    result = register(target, "target")
    same_width(first, second, result)
    return _multiply(
        emitter,
        first,
        second,
        result,
        target_width=len(first),
        control=None,
        workspace=workspace,
        ancillas=ancillas,
        carry=carry,
    )


def multiply_into(
    emitter: Emitter,
    multiplicand: Register,
    multiplier: Register,
    target: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
    carry: QubitLike | None = None,
) -> Emitter:
    """XOR the full widening product into a ``2*n`` target.

    This oracle-oriented spelling computes the product into a private clean
    history register, XOR-copies it into the caller's target, and uncomputes
    the history.  The target therefore has XOR semantics even when it starts
    dirty.
    """

    del workspace, ancillas, carry
    first = register(multiplicand, "multiplicand")
    second = register(multiplier, "multiplier")
    result = register(target, "target")
    same_width(first, second)
    if len(result) != 2 * len(first):
        raise ValueError(
            "widening multiplication target must be twice the operand width"
        )
    disjoint(first, second, result, message="multiplication operands")
    if not isinstance(emitter, CircuitBuilder):
        raise ValueError("multiply_into requires a CircuitBuilder for clean history")
    temporary = emitter.allocate_clean_ancillas(2 * len(first))
    carry_bit = emitter.allocate_clean_ancillas(1)[0]
    start = len(emitter)
    multiply_widening(
        emitter,
        first,
        second,
        temporary,
        workspace=(carry_bit,),
    )
    computation_stop = len(emitter)
    for source_bit, target_bit in zip(temporary, result, strict=True):
        emit_cx(emitter, source_bit, target_bit)
    reverse_block(emitter, start, computation_stop)
    return emitter


def controlled_multiply_modular(
    emitter: Emitter,
    control: QubitLike | Sequence[QubitLike],
    multiplicand: Register,
    multiplier: Register,
    target: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
    carry: QubitLike | None = None,
) -> Emitter:
    """Append modular multiplication controlled by all supplied controls.

    When the controls are one, the product is added into ``target`` modulo its
    width; when they are zero, the target is unchanged.
    """

    first = register(multiplicand, "multiplicand")
    second = register(multiplier, "multiplier")
    result = register(target, "target")
    same_width(first, second, result)
    controls = _controls(control, "control")
    return _multiply(
        emitter,
        first,
        second,
        result,
        target_width=len(first),
        control=controls,
        workspace=workspace,
        ancillas=ancillas,
        carry=carry,
    )


def multiply_widening(
    emitter: Emitter,
    multiplicand: Register,
    multiplier: Register,
    target: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
    carry: QubitLike | None = None,
) -> Emitter:
    """Append the full product into ``target`` modulo its width.

    With a clean ``2*n``-bit target this is the full widening product.
    """

    first = register(multiplicand, "multiplicand")
    second = register(multiplier, "multiplier")
    result = register(target, "target")
    same_width(first, second)
    if len(result) != 2 * len(first):
        raise ValueError(
            "widening multiplication target must be twice the operand width"
        )
    control_bits: tuple[QubitLike, ...] = ()
    _, carry_bit = _workspace(
        emitter,
        first + second + result,
        0,
        workspace,
        ancillas,
        carry,
    )
    for shift in range(len(second)):
        _append_shifted_controlled_addition(
            emitter,
            first,
            result,
            shift,
            carry_bit,
            (*control_bits, second[shift]),
        )
    return emitter


def controlled_multiply_widening(
    emitter: Emitter,
    control: QubitLike | Sequence[QubitLike],
    multiplicand: Register,
    multiplier: Register,
    target: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
    carry: QubitLike | None = None,
) -> Emitter:
    """Append controlled widening multiplication into a ``2*n`` target.

    With the control asserted, the product is added into the target; with the
    control clear, the target is unchanged.
    """

    first = register(multiplicand, "multiplicand")
    second = register(multiplier, "multiplier")
    result = register(target, "target")
    same_width(first, second)
    if len(result) != 2 * len(first):
        raise ValueError(
            "widening multiplication target must be twice the operand width"
        )
    controls = _controls(control, "control")
    _, carry_bit = _workspace(
        emitter,
        first + second + result + controls,
        0,
        workspace,
        ancillas,
        carry,
    )
    for shift in range(len(second)):
        _append_shifted_controlled_addition(
            emitter,
            first,
            result,
            shift,
            carry_bit,
            (*controls, second[shift]),
        )
    return emitter


def multiply_by_constant_modular(
    emitter: Emitter,
    source: Register,
    constant: int,
    target: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
    carry: QubitLike | None = None,
    control: QubitLike | Sequence[QubitLike] | None = None,
) -> Emitter:
    """Append ``target += source * constant (mod 2**n)``.

    A clean target receives the constant product.  Zero and one need no
    workspace; other constants use the clean
    addend/carry contract of :func:`multiply_modular`.
    """

    source_bits = register(source, "source")
    target_bits = register(target, "target")
    same_width(source_bits, target_bits)
    if isinstance(constant, bool):
        raise TypeError("constant must be an integer")
    value = int(_index(constant))
    if value < 0:
        raise ValueError("constant must be non-negative")
    value &= (1 << len(source_bits)) - 1
    controls = _controls(control, "control")
    disjoint(source_bits, target_bits, controls, message="multiplication operands")
    if value == 0:
        return emitter
    if value == 1:
        for source_bit, target_bit in zip(source_bits, target_bits, strict=True):
            _emit_controlled_cx(emitter, controls, source_bit, target_bit)
        return emitter

    addend, carry_bit = _workspace(
        emitter,
        source_bits + target_bits + controls,
        len(target_bits),
        workspace,
        ancillas,
        carry,
    )
    for shift in range(len(source_bits)):
        if (value >> shift) & 1:
            _append_shifted_add(
                emitter,
                source_bits,
                shift,
                addend,
                target_bits,
                carry_bit,
                controls,
            )
    return emitter


def controlled_multiply_by_constant_modular(
    emitter: Emitter,
    control: QubitLike | Sequence[QubitLike],
    source: Register,
    constant: int,
    target: Register,
    workspace: Sequence[QubitLike] | QubitLike | None = None,
    *,
    ancillas: Sequence[QubitLike] | QubitLike | None = None,
    carry: QubitLike | None = None,
) -> Emitter:
    """Controlled alias for :func:`multiply_by_constant_modular`."""

    return multiply_by_constant_modular(
        emitter,
        source,
        constant,
        target,
        workspace,
        ancillas=ancillas,
        carry=carry,
        control=control,
    )


# Readable aliases used by lowering clients and small integrations.
modular_multiply = multiply_modular
multiply_modular_into = multiply_modular
widening_multiply = multiply_widening
multiply_widening_into = multiply_widening
controlled_modular_multiply = controlled_multiply_modular
controlled_widening_multiply = controlled_multiply_widening
modular_multiplier = multiply_modular
widening_multiplier = multiply_widening
mul_modular = multiply_modular
mul_widening = multiply_widening
constant_multiply_modular = multiply_by_constant_modular
controlled_constant_multiply_modular = controlled_multiply_by_constant_modular


__all__ = [
    "constant_multiply_modular",
    "controlled_add_modular",
    "controlled_constant_multiply_modular",
    "controlled_modular_multiply",
    "controlled_multiply_by_constant_modular",
    "controlled_multiply_modular",
    "controlled_multiply_widening",
    "controlled_widening_multiply",
    "multiply_by_constant_modular",
    "multiply_modular",
    "multiply_into",
    "multiply_modular_into",
    "multiply_widening",
    "multiply_widening_into",
    "modular_multiply",
    "modular_multiplier",
    "mul_modular",
    "mul_widening",
    "widening_multiply",
    "widening_multiplier",
]
