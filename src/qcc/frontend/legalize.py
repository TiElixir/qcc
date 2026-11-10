"""Rewrite parsed SSA into the primitive integer operations qcc lowers.

The LLVM parser accepts three things no lowering stage implements directly:

* calls to other functions defined in the same file,
* the integer intrinsics Clang emits for ordinary C (``llvm.umin`` for
  ``a < b ? a : b``, ``llvm.fshl`` for a rotate, and so on), and
* shifts whose amount is a runtime value.

This pass removes all three.  Calls are inlined, which is always possible
because the supported subset has no recursion and no side effects; intrinsics
and variable shifts are expanded into ``icmp``/``select``/shift/arithmetic
sequences.  Every consumer after this point sees one self-contained function
made of primitive operations only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace

from qcc.errors import CompilerError, SourceLocation, UnsupportedConstructError
from qcc.ir.ssa import (
    BasicBlock,
    Function,
    FunctionCollection,
    Instruction,
    IntConstant,
    IntType,
    Module,
    Operand,
    PhiIncoming,
    SSAValue,
)

_SHIFT_OPCODES = frozenset({"shl", "lshr", "ashr"})
_I1 = IntType(1)


class _Names:
    """Hand out value and block names that are unused in one function."""

    def __init__(self, function: Function) -> None:
        self._values = {parameter.name for parameter in function.parameters}
        self._blocks = {block.name for block in function.blocks}
        for instruction in function.instructions:
            if instruction.result is not None:
                self._values.add(instruction.result.name)

    @staticmethod
    def _fresh(base: str, used: set[str]) -> str:
        candidate = base
        suffix = 1
        while candidate in used:
            candidate = f"{base}.{suffix}"
            suffix += 1
        used.add(candidate)
        return candidate

    def value(self, base: str) -> str:
        return self._fresh(base, self._values)

    def block(self, base: str) -> str:
        return self._fresh(base, self._blocks)


class _Expansion:
    """Collect the primitive instructions that replace one instruction."""

    def __init__(self, source: Instruction, names: _Names) -> None:
        if source.result is None:
            raise AssertionError("only value-producing instructions are expanded")
        self.source = source
        self.result = source.result
        self.names = names
        self.instructions: list[Instruction] = []

    def emit(
        self,
        opcode: str,
        value_type: IntType,
        operands: tuple[Operand, ...],
        *,
        final: bool = False,
        predicate: str | None = None,
        operand_type: IntType | None = None,
    ) -> SSAValue:
        """Append one instruction; ``final`` reuses the replaced result name."""

        if final:
            result = self.result
        else:
            result = SSAValue(
                self.names.value(f"{self.result.name}.{opcode}"), value_type
            )
        self.instructions.append(
            Instruction(
                opcode,
                result=result,
                type=value_type,
                operands=operands,
                predicate=predicate,
                operand_type=operand_type,
                source_location=self.source.source_location,
            )
        )
        return result

    def bit(self, value: Operand, index: int) -> Operand:
        """Return bit ``index`` of ``value`` as an ``i1`` operand."""

        value_type = value.type
        if value_type.width == 1:
            return value
        shifted: Operand = value
        if index:
            shifted = self.emit(
                "lshr", value_type, (value, IntConstant(value_type, index))
            )
        return self.emit("trunc", _I1, (shifted,), operand_type=value_type)


def _unsupported(
    construct: str, message: str, location: SourceLocation | None
) -> UnsupportedConstructError:
    return UnsupportedConstructError(construct, message, location)


def _expand_min_max(expansion: _Expansion, predicate: str) -> None:
    first, second = expansion.source.operands
    value_type = first.type
    condition = expansion.emit(
        "icmp", _I1, (first, second), predicate=predicate, operand_type=value_type
    )
    expansion.emit("select", value_type, (condition, first, second), final=True)


def _expand_abs(expansion: _Expansion) -> None:
    value = expansion.source.operands[0]
    value_type = value.type
    if value_type.width == 1:
        expansion.emit("copy", value_type, (value,), final=True)
        return
    # abs(x) = (x ^ m) - m, where m is the sign bit smeared across the word.
    sign = expansion.emit(
        "ashr", value_type, (value, IntConstant(value_type, value_type.width - 1))
    )
    flipped = expansion.emit("xor", value_type, (value, sign))
    expansion.emit("sub", value_type, (flipped, sign), final=True)


def _expand_ctpop(expansion: _Expansion) -> None:
    value = expansion.source.operands[0]
    value_type = value.type
    width = value_type.width
    if width == 1:
        expansion.emit("copy", value_type, (value,), final=True)
        return
    one = IntConstant(value_type, 1)
    total: Operand = expansion.emit("and", value_type, (value, one))
    for index in range(1, width):
        shifted = expansion.emit(
            "lshr", value_type, (value, IntConstant(value_type, index))
        )
        bit = expansion.emit("and", value_type, (shifted, one))
        total = expansion.emit(
            "add", value_type, (total, bit), final=index == width - 1
        )


def _expand_permutation(
    expansion: _Expansion, chunk: int, moves: Iterable[tuple[int, int]]
) -> None:
    """OR together ``chunk``-bit fields moved from source to target offsets."""

    value = expansion.source.operands[0]
    value_type = value.type
    mask = IntConstant(value_type, (1 << chunk) - 1)
    pending = list(moves)
    combined: Operand | None = None
    for position, (source_offset, target_offset) in enumerate(pending):
        piece: Operand = value
        if source_offset:
            piece = expansion.emit(
                "lshr", value_type, (piece, IntConstant(value_type, source_offset))
            )
        piece = expansion.emit("and", value_type, (piece, mask))
        if target_offset:
            piece = expansion.emit(
                "shl", value_type, (piece, IntConstant(value_type, target_offset))
            )
        if combined is None:
            combined = piece
        else:
            combined = expansion.emit(
                "or",
                value_type,
                (combined, piece),
                final=position == len(pending) - 1,
            )
    if len(pending) == 1:
        assert combined is not None
        expansion.emit("copy", value_type, (combined,), final=True)


def _expand_bswap(expansion: _Expansion) -> None:
    width = expansion.source.operands[0].type.width
    if width % 16:
        raise _unsupported(
            "bswap width",
            f"llvm.bswap needs a whole number of byte pairs, not i{width}",
            expansion.source.source_location,
        )
    _expand_permutation(
        expansion,
        8,
        ((offset, width - 8 - offset) for offset in range(0, width, 8)),
    )


def _expand_bitreverse(expansion: _Expansion) -> None:
    width = expansion.source.operands[0].type.width
    _expand_permutation(
        expansion, 1, ((index, width - 1 - index) for index in range(width))
    )


def _expand_count_zeros(expansion: _Expansion, *, leading: bool) -> None:
    value = expansion.source.operands[0]
    value_type = value.type
    width = value_type.width
    # Start from the all-zero answer and let each set bit overwrite it; the
    # last write wins, so visit bits toward the end the count starts from.
    order = range(width) if leading else range(width - 1, -1, -1)
    count: Operand = IntConstant(value_type, width)
    for position, index in enumerate(order):
        zeros = width - 1 - index if leading else index
        count = expansion.emit(
            "select",
            value_type,
            (expansion.bit(value, index), IntConstant(value_type, zeros), count),
            final=position == width - 1,
        )


def _expand_funnel_shift(expansion: _Expansion, *, left: bool) -> None:
    high, low, amount = expansion.source.operands
    value_type = high.type
    width = value_type.width
    if isinstance(amount, IntConstant):
        count = amount.value % width
        if count == 0:
            expansion.emit("copy", value_type, (high if left else low,), final=True)
            return
        if left:
            kept = expansion.emit(
                "shl", value_type, (high, IntConstant(value_type, count))
            )
            filled = expansion.emit(
                "lshr", value_type, (low, IntConstant(value_type, width - count))
            )
        else:
            kept = expansion.emit(
                "lshr", value_type, (low, IntConstant(value_type, count))
            )
            filled = expansion.emit(
                "shl", value_type, (high, IntConstant(value_type, width - count))
            )
        expansion.emit("or", value_type, (kept, filled), final=True)
        return

    if width & (width - 1):
        raise _unsupported(
            "variable funnel shift",
            f"a variable rotate or funnel shift of i{width} needs the amount "
            "reduced modulo a width that is not a power of two; use a constant "
            "amount or a power-of-two width",
            expansion.source.source_location,
        )
    if width == 1:
        expansion.emit("copy", value_type, (high if left else low,), final=True)
        return
    # Split the complementary shift in two so neither amount reaches the
    # width: (b >> 1) >> (width - 1 - s) is b >> (width - s) for every s.
    reduced = expansion.emit(
        "and", value_type, (amount, IntConstant(value_type, width - 1))
    )
    complement = expansion.emit(
        "xor", value_type, (reduced, IntConstant(value_type, width - 1))
    )
    one = IntConstant(value_type, 1)
    if left:
        kept = expansion.emit("shl", value_type, (high, reduced))
        nudged = expansion.emit("lshr", value_type, (low, one))
        filled = expansion.emit("lshr", value_type, (nudged, complement))
    else:
        kept = expansion.emit("lshr", value_type, (low, reduced))
        nudged = expansion.emit("shl", value_type, (high, one))
        filled = expansion.emit("shl", value_type, (nudged, complement))
    expansion.emit("or", value_type, (kept, filled), final=True)


def _expand_saturating(expansion: _Expansion, *, add: bool) -> None:
    first, second = expansion.source.operands
    value_type = first.type
    if add:
        total = expansion.emit("add", value_type, (first, second))
        wrapped = expansion.emit(
            "icmp", _I1, (total, first), predicate="ult", operand_type=value_type
        )
        expansion.emit(
            "select",
            value_type,
            (wrapped, IntConstant(value_type, value_type.modulus - 1), total),
            final=True,
        )
        return
    difference = expansion.emit("sub", value_type, (first, second))
    borrowed = expansion.emit(
        "icmp", _I1, (first, second), predicate="ult", operand_type=value_type
    )
    expansion.emit(
        "select",
        value_type,
        (borrowed, IntConstant(value_type, 0), difference),
        final=True,
    )


_INTRINSIC_EXPANDERS: dict[str, Callable[[_Expansion], None]] = {
    "llvm.umin": lambda expansion: _expand_min_max(expansion, "ult"),
    "llvm.umax": lambda expansion: _expand_min_max(expansion, "ugt"),
    "llvm.smin": lambda expansion: _expand_min_max(expansion, "slt"),
    "llvm.smax": lambda expansion: _expand_min_max(expansion, "sgt"),
    "llvm.abs": _expand_abs,
    "llvm.ctpop": _expand_ctpop,
    "llvm.ctlz": lambda expansion: _expand_count_zeros(expansion, leading=True),
    "llvm.cttz": lambda expansion: _expand_count_zeros(expansion, leading=False),
    "llvm.bswap": _expand_bswap,
    "llvm.bitreverse": _expand_bitreverse,
    "llvm.fshl": lambda expansion: _expand_funnel_shift(expansion, left=True),
    "llvm.fshr": lambda expansion: _expand_funnel_shift(expansion, left=False),
    "llvm.uadd.sat": lambda expansion: _expand_saturating(expansion, add=True),
    "llvm.usub.sat": lambda expansion: _expand_saturating(expansion, add=False),
}


def _expand_variable_shift(expansion: _Expansion) -> None:
    """Build a barrel shifter: one mux stage per bit of the shift amount.

    C leaves a shift by the operand width or more undefined, so only the
    amount bits that can select a defined shift are examined.
    """

    source = expansion.source
    value, amount = source.operands
    value_type = value.type
    width = value_type.width
    stages = (width - 1).bit_length()
    if stages == 0:
        expansion.emit("copy", value_type, (value,), final=True)
        return
    current: Operand = value
    for stage in range(stages):
        shifted = expansion.emit(
            source.opcode, value_type, (current, IntConstant(value_type, 1 << stage))
        )
        current = expansion.emit(
            "select",
            value_type,
            (expansion.bit(amount, stage), shifted, current),
            final=stage == stages - 1,
        )


def _is_user_call(instruction: Instruction) -> bool:
    return (
        instruction.opcode == "call"
        and instruction.callee is not None
        and not instruction.callee.startswith("llvm.")
    )


def _needs_expansion(instruction: Instruction) -> bool:
    if instruction.opcode == "call":
        return instruction.callee in _INTRINSIC_EXPANDERS
    return (
        instruction.opcode in _SHIFT_OPCODES
        and len(instruction.operands) == 2
        and not isinstance(instruction.operands[1], IntConstant)
    )


def _expand_instructions(function: Function) -> Function:
    """Replace intrinsic calls and variable shifts with primitives."""

    names = _Names(function)
    blocks: list[BasicBlock] = []
    changed = False
    for block in function.blocks:
        pending = list(block.instructions)
        rewritten: list[Instruction] = []
        while pending:
            instruction = pending.pop(0)
            if not _needs_expansion(instruction):
                rewritten.append(instruction)
                continue
            changed = True
            expansion = _Expansion(instruction, names)
            if instruction.opcode == "call":
                assert instruction.callee is not None
                _INTRINSIC_EXPANDERS[instruction.callee](expansion)
            else:
                _expand_variable_shift(expansion)
            # An expansion may itself contain a variable shift (a variable
            # rotate does), so its output is scanned again.
            pending[:0] = expansion.instructions
        blocks.append(replace(block, instructions=tuple(rewritten)))
    return replace(function, blocks=tuple(blocks)) if changed else function


def _substitute(
    blocks: list[BasicBlock], name: str, replacement: Operand
) -> list[BasicBlock]:
    """Replace every use of SSA value ``name`` with ``replacement``."""

    def swap(operand: Operand) -> Operand:
        if isinstance(operand, SSAValue) and operand.name == name:
            return replacement
        return operand

    result: list[BasicBlock] = []
    for block in blocks:
        instructions = tuple(
            replace(
                instruction,
                operands=tuple(swap(operand) for operand in instruction.operands),
                incoming=tuple(
                    replace(edge, value=swap(edge.value))
                    for edge in instruction.incoming
                ),
            )
            for instruction in block.instructions
        )
        result.append(replace(block, instructions=instructions))
    return result


def _retarget_phis(
    blocks: list[BasicBlock], successors: Iterable[str], old: str, new: str
) -> list[BasicBlock]:
    """Rename predecessor ``old`` to ``new`` in the phis of ``successors``."""

    affected = set(successors)
    result: list[BasicBlock] = []
    for block in blocks:
        if block.name not in affected:
            result.append(block)
            continue
        instructions = tuple(
            replace(
                instruction,
                incoming=tuple(
                    replace(edge, block=new) if edge.block == old else edge
                    for edge in instruction.incoming
                ),
            )
            if instruction.opcode == "phi"
            else instruction
            for instruction in block.instructions
        )
        result.append(replace(block, instructions=instructions))
    return result


def _clone_callee(
    callee: Function,
    call: Instruction,
    names: _Names,
) -> tuple[list[BasicBlock], list[tuple[Operand, str]]]:
    """Copy ``callee`` with fresh names and its parameters bound to arguments.

    Returns the cloned blocks, still ending in their original ``ret``
    terminators, and the ``(returned value, block)`` pair of every return.
    """

    assert call.result is not None
    prefix = f"{call.result.name}.{callee.name}"
    values: dict[str, Operand] = {
        parameter.name: argument
        for parameter, argument in zip(callee.parameters, call.operands, strict=True)
    }
    for instruction in callee.instructions:
        if instruction.result is not None:
            values[instruction.result.name] = SSAValue(
                names.value(f"{prefix}.{instruction.result.name}"),
                instruction.result.type,
            )
    block_names = {
        block.name: names.block(f"{prefix}.{block.name}") for block in callee.blocks
    }

    def bind(operand: Operand) -> Operand:
        if isinstance(operand, SSAValue):
            return values[operand.name]
        return operand

    cloned: list[BasicBlock] = []
    returns: list[tuple[Operand, str]] = []
    for block in callee.blocks:
        instructions: list[Instruction] = []
        for instruction in block.instructions:
            result = instruction.result
            bound = replace(
                instruction,
                result=None if result is None else bind(result),  # type: ignore[arg-type]
                operands=tuple(bind(operand) for operand in instruction.operands),
                incoming=tuple(
                    PhiIncoming(
                        bind(edge.value), block_names[edge.block], edge.source_location
                    )
                    for edge in instruction.incoming
                ),
                targets=tuple(block_names[target] for target in instruction.targets),
            )
            if bound.opcode == "ret":
                returns.append((bound.operands[0], block_names[block.name]))
            instructions.append(bound)
        cloned.append(
            BasicBlock(
                block_names[block.name], tuple(instructions), block.source_location
            )
        )
    return cloned, returns


def _check_call_signature(call: Instruction, callee: Function) -> None:
    expected = tuple(parameter.type for parameter in callee.parameters)
    actual = tuple(operand.type for operand in call.operands)
    if expected != actual or callee.return_type != call.type:
        raise _unsupported(
            "mismatched call",
            f"call to @{callee.name} does not match its definition: passes "
            f"({', '.join(map(str, actual))}) -> {call.type}, defined as "
            f"({', '.join(map(str, expected))}) -> {callee.return_type}",
            call.source_location,
        )


def _inline_calls(
    function: Function, resolve: Callable[[Instruction], Function]
) -> Function:
    """Splice every direct callee into ``function``.

    ``resolve`` returns the already-legalized callee, so a spliced body never
    introduces another call.
    """

    if not any(_is_user_call(instruction) for instruction in function.instructions):
        return function
    names = _Names(function)
    blocks = list(function.blocks)
    index = 0
    while index < len(blocks):
        block = blocks[index]
        position = next(
            (
                offset
                for offset, instruction in enumerate(block.instructions)
                if _is_user_call(instruction)
            ),
            None,
        )
        if position is None:
            index += 1
            continue
        call = block.instructions[position]
        assert call.result is not None
        callee = resolve(call)
        _check_call_signature(call, callee)
        cloned, returns = _clone_callee(callee, call, names)
        before = block.instructions[:position]
        after = block.instructions[position + 1 :]

        if len(cloned) == 1:
            # Straight-line callee: splice its body in place, keeping the CFG.
            body = cloned[0].instructions[:-1]
            blocks[index] = replace(block, instructions=(*before, *body, *after))
            blocks = _substitute(blocks, call.result.name, returns[0][0])
            continue

        continuation = names.block(f"{block.name}.after.{callee.name}")
        entry = replace(
            block,
            instructions=(
                *before,
                Instruction(
                    "br",
                    source_location=call.source_location,
                    targets=(cloned[0].name,),
                ),
            ),
        )
        body_blocks = [
            replace(
                clone,
                instructions=(
                    *clone.instructions[:-1],
                    Instruction(
                        "br",
                        source_location=clone.instructions[-1].source_location,
                        targets=(continuation,),
                    ),
                )
                if clone.instructions[-1].opcode == "ret"
                else clone.instructions,
            )
            for clone in cloned
        ]
        merged: tuple[Instruction, ...] = ()
        if len(returns) > 1:
            merged = (
                Instruction(
                    "phi",
                    result=call.result,
                    type=call.result.type,
                    operands=tuple(value for value, _ in returns),
                    source_location=call.source_location,
                    incoming=tuple(
                        PhiIncoming(value, source, call.source_location)
                        for value, source in returns
                    ),
                ),
            )
        tail = BasicBlock(continuation, (*merged, *after), block.source_location)
        successors = block.successors
        blocks[index : index + 1] = [entry, *body_blocks, tail]
        blocks = _retarget_phis(blocks, successors, block.name, continuation)
        if len(returns) == 1:
            blocks = _substitute(blocks, call.result.name, returns[0][0])
        # The cloned body is call-free; resume at the continuation block.
        index += 1 + len(body_blocks)
    return replace(function, blocks=tuple(blocks))


def legalize_module(module: Module, *, strict: bool = True) -> Module:
    """Return ``module`` with calls inlined and non-primitive ops expanded.

    With ``strict=False`` a function that cannot be legalized is moved to
    ``module.rejected`` with its diagnostic instead of aborting the module.
    """

    originals: dict[str, Function] = {
        function.name: function for function in module.functions
    }
    rejected: dict[str, Exception] = dict(module.rejected)
    finished: dict[str, Function] = {}
    in_progress: list[str] = []

    def legalize(name: str) -> Function:
        done = finished.get(name)
        if done is not None:
            return done
        failure = rejected.get(name)
        if failure is not None:
            raise failure
        in_progress.append(name)
        try:

            def resolve(call: Instruction) -> Function:
                callee = call.callee
                assert callee is not None
                if callee in in_progress:
                    cycle = " -> ".join(
                        f"@{item}"
                        for item in (*in_progress[in_progress.index(callee) :], callee)
                    )
                    raise _unsupported(
                        "recursion",
                        f"recursive call {cycle}; recursion is not supported "
                        "because every function call is inlined into one "
                        "fixed-size circuit",
                        call.source_location,
                    )
                if callee not in originals and callee not in rejected:
                    raise _unsupported(
                        f"call to external function {callee}",
                        f"call to external function @{callee}; function calls "
                        "are only supported when the callee is defined in the "
                        "same file",
                        call.source_location,
                    )
                return legalize(callee)

            result = _expand_instructions(_inline_calls(originals[name], resolve))
        except CompilerError as error:
            rejected[name] = error
            raise
        finally:
            in_progress.pop()
        finished[name] = result
        return result

    accepted: list[Function] = []
    for name in originals:
        try:
            accepted.append(legalize(name))
        except CompilerError:
            if strict:
                raise
    return replace(
        module,
        functions=FunctionCollection(accepted),
        rejected=rejected,
    )


def legalize_function(
    function: Function, functions: Mapping[str, Function] | None = None
) -> Function:
    """Legalize one function, resolving calls against ``functions``."""

    available = dict(functions or {})
    available[function.name] = function
    module = legalize_module(Module(FunctionCollection(available.values())))
    return module.get_function(function.name)


__all__ = ["legalize_function", "legalize_module"]
