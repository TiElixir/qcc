"""Conservative constant folding and dead-value elimination for qcc SSA.

The typed SSA model is immutable, so these helpers return structurally equivalent
functions/modules with rewritten instruction tuples.  They only reason about
pure integer operations.  Entry-block substitutions are globally safe; values
created in conditional blocks are folded to local ``const``/``copy``
definitions but are not substituted across CFG paths.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

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

from .gates import normalize_level, optimizer_enabled

_PURE_OPCODES = {
    "add",
    "sub",
    "mul",
    "udiv",
    "urem",
    "and",
    "or",
    "xor",
    "not",
    "zext",
    "sext",
    "trunc",
    "const",
    "constant",
    "literal",
    "copy",
    "mov",
    "identity",
    "icmp",
    "select",
    "phi",
    "shl",
    "lshr",
    "ashr",
    "rol",
    "ror",
    "rotl",
    "rotr",
    "rotate",
    "rotate_left",
    "rotate_right",
    "fshl",
    "fshr",
}
_TERMINATORS = {"ret", "return", "br", "switch", "unreachable"}
_ICMP_PREDICATES = {
    "eq",
    "ne",
    "ugt",
    "uge",
    "ult",
    "ule",
    "sgt",
    "sge",
    "slt",
    "sle",
}
_SHIFT_OPCODES = {"shl", "lshr", "ashr"}
_ROTATE_OPCODES = {
    "rol",
    "ror",
    "rotl",
    "rotr",
    "rotate",
    "rotate_left",
    "rotate_right",
    "fshl",
    "fshr",
}


def _result_type(instruction: Instruction) -> IntType | None:
    if isinstance(instruction.type, IntType):
        return instruction.type
    if instruction.result is not None:
        return instruction.result.type
    return None


def _operand_type(operand: Operand) -> IntType | None:
    value_type = getattr(operand, "type", None)
    return value_type if isinstance(value_type, IntType) else None


def _same_operand(first: Operand, second: Operand) -> bool:
    return first == second


def _constant(value: int, type_: IntType) -> IntConstant:
    return IntConstant(type_, type_.normalize(value))


def _zero(type_: IntType) -> IntConstant:
    return _constant(0, type_)


def _all_ones(type_: IntType) -> IntConstant:
    return _constant(type_.modulus - 1, type_)


def _as_signed(value: int, width: int) -> int:
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value


def _resolve(operand: Operand, environment: dict[str, Operand]) -> Operand:
    """Resolve an SSA alias/constant chain without mutating the environment."""

    current = operand
    seen: set[str] = set()
    while isinstance(current, SSAValue) and current.name in environment:
        if current.name in seen:
            break
        seen.add(current.name)
        replacement = environment[current.name]
        if replacement == current:
            break
        current = replacement
    return current


def _binary_constant(
    opcode: str, first: int, second: int, type_: IntType
) -> int | None:
    mask = type_.modulus - 1
    if opcode == "add":
        return (first + second) & mask
    if opcode == "sub":
        return (first - second) & mask
    if opcode == "mul":
        return (first * second) & mask
    if opcode == "and":
        return first & second
    if opcode == "or":
        return first | second
    if opcode == "xor":
        return first ^ second
    if opcode == "udiv":
        return None if second == 0 else first // second
    if opcode == "urem":
        return None if second == 0 else first % second
    return None


def _comparison_constant(
    predicate: str, first: int, second: int, type_: IntType
) -> int | None:
    if predicate not in _ICMP_PREDICATES:
        return None
    signed = predicate.startswith("s")
    left = _as_signed(first, type_.width) if signed else first
    right = _as_signed(second, type_.width) if signed else second
    relation = predicate[1:] if signed else predicate
    result = {
        "eq": left == right,
        "ne": left != right,
        "ugt": left > right,
        "uge": left >= right,
        "ult": left < right,
        "ule": left <= right,
        "sgt": left > right,
        "sge": left >= right,
        "slt": left < right,
        "sle": left <= right,
    }.get(predicate if not signed else f"s{relation}")
    if result is None:
        # The dictionary above intentionally keeps the LLVM spelling visible;
        # this fallback handles the stripped signed relation explicitly.
        result = {
            "eq": left == right,
            "ne": left != right,
            "gt": left > right,
            "ge": left >= right,
            "lt": left < right,
            "le": left <= right,
        }.get(relation)
    return int(bool(result)) if result is not None else None


def _shift_constant(
    opcode: str, first: int, amount: int, type_: IntType, direction: str | None
) -> int | None:
    width = type_.width
    mask = type_.modulus - 1
    if opcode in _SHIFT_OPCODES and amount >= width:
        return None
    if opcode == "shl":
        return (first << amount) & mask
    if opcode == "lshr":
        return first >> amount
    if opcode == "ashr":
        return (_as_signed(first, width) >> amount) & mask
    if opcode in _ROTATE_OPCODES:
        count = amount % width
        right = opcode in {"ror", "rotr", "rotate_right", "fshr"}
        if opcode == "rotate":
            right = direction in {"right", "ror", "rotr"}
        if opcode in {"rol", "rotl", "rotate_left", "fshl"}:
            right = False
        if count == 0:
            return first
        return (
            ((first >> count) | (first << (width - count))) & mask
            if right
            else ((first << count) | (first >> (width - count))) & mask
        )
    return None


def _algebraic_simplification(
    opcode: str, operands: tuple[Operand, ...], type_: IntType
) -> Operand | None:
    if opcode in {"copy", "mov", "identity"} and len(operands) == 1:
        return operands[0]
    if opcode == "not" and len(operands) == 1:
        operand = operands[0]
        if isinstance(operand, IntConstant):
            return _constant(~operand.value, type_)
        return None
    if opcode in {"zext", "sext", "trunc"} and len(operands) == 1:
        operand = operands[0]
        source_type = _operand_type(operand)
        if source_type is not None and source_type.width == type_.width:
            return operand
        if isinstance(operand, IntConstant):
            if opcode == "sext" and source_type is not None:
                value = _as_signed(operand.value, source_type.width)
            else:
                value = operand.value
            return _constant(value, type_)
        return None
    if opcode in _SHIFT_OPCODES | _ROTATE_OPCODES and len(operands) >= 2:
        amount = operands[-1]
        if isinstance(amount, IntConstant) and amount.value == 0:
            return operands[0]
    if opcode == "select" and len(operands) == 3:
        condition, true_value, false_value = operands
        if _same_operand(true_value, false_value):
            return true_value
        if isinstance(condition, IntConstant):
            return true_value if condition.value & 1 else false_value
        return None
    if (
        opcode == "phi"
        and operands
        and all(_same_operand(operands[0], operand) for operand in operands[1:])
    ):
        return operands[0]
    if len(operands) != 2:
        return None

    first, second = operands
    first_constant = first.value if isinstance(first, IntConstant) else None
    second_constant = second.value if isinstance(second, IntConstant) else None
    if first_constant is not None and second_constant is not None:
        result = _binary_constant(opcode, first_constant, second_constant, type_)
        if result is not None:
            return _constant(result, type_)

    if _same_operand(first, second):
        if opcode in {"xor", "sub"}:
            return _zero(type_)
        if opcode in {"and", "or"}:
            return first

    if opcode in {"add", "xor", "or"} and (first_constant == 0 or second_constant == 0):
        return second if first_constant == 0 else first
    if opcode == "sub" and second_constant == 0:
        return first
    if opcode == "and":
        if first_constant == 0 or second_constant == 0:
            return _zero(type_)
        if first_constant == type_.modulus - 1:
            return second
        if second_constant == type_.modulus - 1:
            return first
    if opcode == "or" and (
        first_constant == type_.modulus - 1 or second_constant == type_.modulus - 1
    ):
        return _all_ones(type_)
    if opcode == "mul":
        if first_constant == 0 or second_constant == 0:
            return _zero(type_)
        if first_constant == 1:
            return second
        if second_constant == 1:
            return first
    if opcode in {"udiv", "urem"} and second_constant == 1:
        return first if opcode == "udiv" else _zero(type_)
    return None


def _fold_instruction(
    instruction: Instruction,
    operands: tuple[Operand, ...],
    incoming: tuple[PhiIncoming, ...],
) -> Operand | None:
    if instruction.result is None:
        return None
    type_ = _result_type(instruction)
    if type_ is None:
        return None
    opcode = instruction.opcode
    if (
        opcode in {"fshl", "fshr"}
        and len(operands) == 3
        and not _same_operand(operands[0], operands[1])
    ):
        return None
    if opcode == "phi":
        operands = tuple(edge.value for edge in incoming)
    simplified = _algebraic_simplification(opcode, operands, type_)
    if simplified is not None:
        return simplified

    if opcode == "icmp" and len(operands) == 2:
        first, second = operands
        if isinstance(first, IntConstant) and isinstance(second, IntConstant):
            operand_type = instruction.operand_type or _operand_type(first)
            if operand_type is not None:
                value = _comparison_constant(
                    (instruction.predicate or "").lower(),
                    first.value,
                    second.value,
                    operand_type,
                )
                if value is not None:
                    return _constant(value, type_)
    if opcode in {"const", "constant", "literal"} and len(operands) == 1:
        return operands[0] if isinstance(operands[0], IntConstant) else None
    if (
        opcode in {"add", "sub", "mul", "and", "or", "xor", "udiv", "urem"}
        and len(operands) == 2
        and all(isinstance(operand, IntConstant) for operand in operands)
    ):
        first = operands[0]
        second = operands[1]
        assert isinstance(first, IntConstant)
        assert isinstance(second, IntConstant)
        value = _binary_constant(opcode, first.value, second.value, type_)
        if value is not None:
            return _constant(value, type_)
    if opcode in _SHIFT_OPCODES | _ROTATE_OPCODES and len(operands) >= 2:
        first = operands[0]
        amount = operands[-1]
        if isinstance(first, IntConstant) and isinstance(amount, IntConstant):
            direction = getattr(instruction, "direction", None)
            if not isinstance(direction, str):
                direction = None
            value = _shift_constant(opcode, first.value, amount.value, type_, direction)
            if value is not None:
                return _constant(value, type_)
    return None


def _rewrite_instruction(
    instruction: Instruction, environment: dict[str, Operand]
) -> Instruction:
    operands = tuple(_resolve(operand, environment) for operand in instruction.operands)
    incoming = tuple(
        replace(edge, value=_resolve(edge.value, environment))
        for edge in instruction.incoming
    )
    return replace(instruction, operands=operands, incoming=incoming)


def _replacement_instruction(
    instruction: Instruction, replacement: Operand
) -> Instruction:
    type_ = _result_type(instruction)
    if type_ is None:
        return instruction
    if isinstance(replacement, IntConstant):
        return replace(
            instruction,
            opcode="const",
            type=type_,
            operands=(replacement,),
            flags=(),
            predicate=None,
            operand_type=None,
            incoming=(),
        )
    return replace(
        instruction,
        opcode="copy",
        type=type_,
        operands=(replacement,),
        flags=(),
        predicate=None,
        operand_type=None,
        incoming=(),
    )


def _transform_function(function: Function, *, remove_folded: bool) -> Function:
    safe_environment: dict[str, Operand] = {
        parameter.name: parameter for parameter in function.parameters
    }
    blocks: list[BasicBlock] = []
    for block_index, block in enumerate(function.blocks):
        environment = dict(safe_environment)
        transformed: list[Instruction] = []
        for instruction in block.instructions:
            resolved_operands = tuple(
                _resolve(operand, environment) for operand in instruction.operands
            )
            resolved_incoming = tuple(
                replace(edge, value=_resolve(edge.value, environment))
                for edge in instruction.incoming
            )
            rewritten = _rewrite_instruction(
                instruction, safe_environment if block_index else environment
            )
            replacement = _fold_instruction(
                rewritten, resolved_operands, resolved_incoming
            )
            result = instruction.result
            if replacement is not None and result is not None:
                environment[result.name] = replacement
                if block_index == 0:
                    safe_environment[result.name] = replacement
                if remove_folded and block_index == 0:
                    continue
                transformed.append(_replacement_instruction(rewritten, replacement))
                continue
            transformed.append(rewritten)
            if result is not None:
                environment[result.name] = result
                if block_index == 0:
                    safe_environment[result.name] = result
        blocks.append(replace(block, instructions=tuple(transformed)))
    return replace(function, blocks=tuple(blocks))


def _used_names(instructions: Iterable[Instruction]) -> dict[str, int]:
    uses: dict[str, int] = {}
    for instruction in instructions:
        operands = list(instruction.operands)
        operands.extend(edge.value for edge in instruction.incoming)
        for operand in operands:
            if isinstance(operand, SSAValue):
                uses[operand.name] = uses.get(operand.name, 0) + 1
    return uses


def _dce_function(function: Function) -> Function:
    current = function
    while True:
        uses = _used_names(current.instructions)
        changed = False
        blocks: list[BasicBlock] = []
        for block in current.blocks:
            kept: list[Instruction] = []
            for instruction in block.instructions:
                result = instruction.result
                removable = (
                    result is not None
                    and result.name not in uses
                    and instruction.opcode in _PURE_OPCODES
                    and instruction.opcode not in _TERMINATORS
                )
                if removable:
                    changed = True
                else:
                    kept.append(instruction)
            blocks.append(replace(block, instructions=tuple(kept)))
        if not changed:
            return current
        current = replace(current, blocks=tuple(blocks))


def _map_program(
    value: Function | Module, transform: Callable[[Function], Function]
) -> Function | Module:
    if isinstance(value, Function):
        return transform(value)
    functions = tuple(transform(function) for function in value.functions)
    return replace(value, functions=FunctionCollection(functions))


def constant_fold(value: Function | Module) -> Function | Module:
    """Fold pure integer constants and local algebraic identities in SSA.

    Folded definitions remain as explicit ``const``/``copy`` instructions so
    this helper can be used independently of dead-value elimination.
    """

    return _map_program(
        value, lambda function: _transform_function(function, remove_folded=False)
    )


def dead_value_eliminate(value: Function | Module) -> Function | Module:
    """Remove unused pure SSA definitions to a fixed point."""

    return _map_program(value, _dce_function)


def optimize_ssa(value: Any, level: int = 2, *, opt: int | None = None) -> Any:
    """Apply the v0.4 SSA simplification pipeline at an enabled level."""

    normalized = normalize_level(level if opt is None else opt)
    if not optimizer_enabled(normalized) or not isinstance(value, (Function, Module)):
        return value

    def transform(function: Function) -> Function:
        folded = _transform_function(function, remove_folded=True)
        return _dce_function(folded)

    return _map_program(value, transform)


# Public descriptive aliases.
fold_constants = constant_fold
fold_constant_expressions = constant_fold
eliminate_dead_values = dead_value_eliminate
dce = dead_value_eliminate
optimize_function = optimize_ssa
optimize_module = optimize_ssa


__all__ = [
    "constant_fold",
    "dce",
    "dead_value_eliminate",
    "eliminate_dead_values",
    "fold_constant_expressions",
    "fold_constants",
    "optimize_function",
    "optimize_module",
    "optimize_ssa",
]
