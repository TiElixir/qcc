"""Small, strict parser for the v0.1 textual LLVM subset.

LLVM's textual format is intentionally broad and changes slightly between
Clang releases. This parser therefore ignores harmless module attributes and
metadata declarations, but validates every instruction it sees. The accepted
program shape is an integer-typed function with one straight-line basic block.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from qcc.errors import LLVMParseError, SourceLocation, UnsupportedConstructError
from qcc.ir.ssa import (
    BasicBlock,
    Function,
    FunctionCollection,
    Instruction,
    IntConstant,
    IntType,
    MetadataAttachment,
    Module,
    Parameter,
    SSAValue,
    VoidType,
)


@dataclass(frozen=True, slots=True)
class Token:
    """A lexical token with the source position at which it began."""

    kind: str
    value: str
    location: SourceLocation

    @property
    def line(self) -> int:
        return self.location.line or 0

    @property
    def column(self) -> int:
        return self.location.column or 0

    def __repr__(self) -> str:
        return f"Token({self.kind!r}, {self.value!r}, {self.location!s})"


_IDENTIFIER_STOP = frozenset("\t\r\n ,=(){}[]<>*:")
_PUNCTUATION = frozenset("=,(){}[]<>*:")
_INTEGER_RE = re.compile(r"^[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)$")
_SSA_RE = re.compile(r"^%(?:[A-Za-z$._-][A-Za-z0-9$._-]*|[0-9]+|\"(?:[^\"\\]|\\.)*\")$")
_TYPE_RE = re.compile(r"^i([1-9][0-9]*)$")
_FLOAT_WORD_RE = re.compile(
    r"(?:^|\s)(?:half|bfloat|float|double|fp128|x86_fp80)(?:\s|$)"
)
_VECTOR_RE = re.compile(r"<\s*[0-9]+\s*x\s*[^>]+>")

_BINARY_OPS = frozenset({"add", "sub", "and", "or", "xor"})
_CAST_OPS = frozenset({"zext", "sext", "trunc"})
_ICMP_PREDICATES = frozenset(
    {"eq", "ne", "ugt", "uge", "ult", "ule", "sgt", "sge", "slt", "sle"}
)
_CONTROL_FLOW_OPS = frozenset(
    {
        "br",
        "switch",
        "indirectbr",
        "invoke",
        "callbr",
        "resume",
        "catchswitch",
        "catchret",
        "cleanupret",
        "unreachable",
    }
)
_MEMORY_OPS = frozenset(
    {"load", "store", "atomicrmw", "cmpxchg", "fence", "alloca", "getelementptr"}
)
_UNSUPPORTED_OPS = frozenset(
    {
        "mul",
        "udiv",
        "sdiv",
        "urem",
        "srem",
        "shl",
        "lshr",
        "ashr",
        "select",
        "phi",
        "extractvalue",
        "insertvalue",
        "extractelement",
        "insertelement",
        "shufflevector",
        "freeze",
        "va_arg",
        "landingpad",
    }
)


def _location(
    source_name: str,
    line_number: int,
    text: str,
    column: int | None = None,
) -> SourceLocation:
    if column is None:
        leading = len(text) - len(text.lstrip())
        column = leading + 1
    return SourceLocation(source_name, line_number, max(column, 1))


def _strip_comment(text: str) -> str:
    in_string = False
    escaped = False
    for char_index, char in enumerate(text):
        if char == '"' and not escaped:
            in_string = not in_string
        if char == ";" and not in_string:
            return text[:char_index]
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return text


def tokenize(text: str, source_name: str = "<llvm>") -> tuple[Token, ...]:
    """Tokenize LLVM text while retaining one-based source locations."""

    tokens: list[Token] = []
    index = 0
    line = 1
    column = 1
    length = len(text)

    def advance(value: str) -> None:
        nonlocal line, column
        newlines = value.count("\n")
        if newlines:
            line += newlines
            column = len(value.rsplit("\n", 1)[1]) + 1
        else:
            column += len(value)

    while index < length:
        char = text[index]
        if char in " \t\r":
            index += 1
            column += 1
            continue
        if char == "\n":
            index += 1
            line += 1
            column = 1
            continue
        if char == ";":
            newline = text.find("\n", index)
            if newline == -1:
                break
            advance(text[index:newline])
            index = newline
            continue

        token_location = SourceLocation(source_name, line, column)
        if char == '"':
            end = index + 1
            escaped = False
            while end < length:
                current = text[end]
                if current == '"' and not escaped:
                    end += 1
                    break
                escaped = current == "\\" and not escaped
                if current != "\\":
                    escaped = False
                end += 1
            else:
                raise LLVMParseError("unterminated quoted LLVM string", token_location)
            value = text[index:end]
            tokens.append(Token("string", value, token_location))
            advance(value)
            index = end
            continue

        if char in "%@!#":
            end = index + 1
            if end < length and text[end] == '"':
                end += 1
                escaped = False
                while end < length:
                    current = text[end]
                    if current == '"' and not escaped:
                        end += 1
                        break
                    escaped = current == "\\" and not escaped
                    if current != "\\":
                        escaped = False
                    end += 1
                else:
                    raise LLVMParseError(
                        "unterminated quoted identifier", token_location
                    )
            else:
                while end < length and text[end] not in _IDENTIFIER_STOP:
                    end += 1
            value = text[index:end]
            tokens.append(Token("identifier", value, token_location))
            advance(value)
            index = end
            continue

        if char in "+-" and index + 1 < length and text[index + 1].isdigit():
            match = re.match(r"[+-]?(?:0[xX][0-9a-fA-F]+|[0-9]+)", text[index:])
            assert match is not None
            value = match.group(0)
            tokens.append(Token("number", value, token_location))
            advance(value)
            index += len(value)
            continue
        if char.isdigit():
            match = re.match(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", text[index:])
            assert match is not None
            value = match.group(0)
            tokens.append(Token("number", value, token_location))
            advance(value)
            index += len(value)
            continue

        if text.startswith("...", index):
            tokens.append(Token("punctuation", "...", token_location))
            advance("...")
            index += 3
            continue
        if char in _PUNCTUATION:
            tokens.append(Token("punctuation", char, token_location))
            advance(char)
            index += 1
            continue

        end = index + 1
        while (
            end < length
            and text[end] not in _IDENTIFIER_STOP
            and text[end] not in _PUNCTUATION
        ):
            if text.startswith("...", end):
                break
            end += 1
        value = text[index:end]
        if not value:
            raise LLVMParseError(f"unexpected character {char!r}", token_location)
        tokens.append(Token("identifier", value, token_location))
        advance(value)
        index = end

    return tuple(tokens)


def _split_top_level(text: str, delimiter: str = ",") -> list[str]:
    parts: list[str] = []
    start = 0
    stack: list[str] = []
    in_string = False
    escaped = False
    matching = {")": "(", "]": "[", "}": "{", ">": "<"}
    opening = set(matching.values())
    for index, char in enumerate(text):
        if char == '"' and not escaped:
            in_string = not in_string
        if in_string:
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
            continue
        if char in opening:
            stack.append(char)
        elif char in matching:
            if stack and stack[-1] == matching[char]:
                stack.pop()
        elif char == delimiter and not stack:
            parts.append(text[start:index].strip())
            start = index + 1
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    parts.append(text[start:].strip())
    return parts


def _find_matching(
    text: str,
    opening_index: int,
    opening: str = "(",
    closing: str = ")",
) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(opening_index, len(text)):
        char = text[index]
        if char == '"' and not escaped:
            in_string = not in_string
        if not in_string:
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return index
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return -1


def _parse_type(value: str, location: SourceLocation) -> IntType:
    match = _TYPE_RE.fullmatch(value.strip())
    if match is None:
        if _FLOAT_WORD_RE.search(value) or value.strip() in {"float", "double"}:
            raise UnsupportedConstructError(
                "floating-point type",
                "floating-point values are not supported; qcc v0.1 accepts "
                "integer types only",
                location,
            )
        if _VECTOR_RE.search(value):
            raise UnsupportedConstructError(
                "vector type",
                "vector types are not supported; qcc v0.1 accepts scalar "
                "integer types only",
                location,
            )
        raise LLVMParseError(f"expected an integer type, got {value!r}", location)
    return IntType(int(match.group(1)))


def _normalize_name(value: str, sigil: str = "%") -> str:
    value = value.strip()
    value = value.removeprefix(sigil)
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value


def _ssa_name(value: str, location: SourceLocation) -> str:
    value = value.strip()
    if not _SSA_RE.fullmatch(value):
        raise LLVMParseError(f"invalid SSA identifier {value!r}", location)
    return _normalize_name(value)


def _parse_integer(value: str, location: SourceLocation) -> int:
    if not _INTEGER_RE.fullmatch(value.strip()):
        raise LLVMParseError(f"expected an integer constant, got {value!r}", location)
    try:
        return (
            int(value, 0)
            if value.lower().lstrip("+-").startswith("0x")
            else int(value, 10)
        )
    except ValueError as exc:
        raise LLVMParseError(f"invalid integer constant {value!r}", location) from exc


def _extract_metadata(
    text: str,
    location: SourceLocation,
) -> tuple[str, tuple[MetadataAttachment, ...]]:
    stack: list[str] = []
    in_string = False
    escaped = False
    start = -1
    for index, char in enumerate(text):
        if char == '"' and not escaped:
            in_string = not in_string
        if not in_string:
            if char in "([{<":
                stack.append(char)
            elif char in ")]}>" and stack:
                stack.pop()
            elif char == "," and not stack:
                remainder = text[index + 1 :].lstrip()
                if remainder.startswith("!"):
                    start = index
                    break
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    if start == -1:
        return text.strip(), ()

    instruction_text = text[:start].rstrip()
    metadata_text = text[start + 1 :].strip()
    attachments: list[MetadataAttachment] = []
    for item in _split_top_level(metadata_text):
        match = re.fullmatch(
            r"!([A-Za-z0-9$._-]+)\s+(![A-Za-z0-9$._-]+)",
            item,
        )
        if match is None:
            raise LLVMParseError(f"malformed metadata attachment {item!r}", location)
        attachments.append(MetadataAttachment(match.group(1), match.group(2)))
    return instruction_text, tuple(attachments)


def _operand(
    text: str,
    expected_type: IntType,
    values: Mapping[str, SSAValue],
    location: SourceLocation,
) -> SSAValue | IntConstant:
    value = text.strip()
    if value.startswith("%"):
        name = _ssa_name(value, location)
        result = values.get(name)
        if result is None:
            raise LLVMParseError(f"use of undefined SSA value {value}", location)
        if result.type != expected_type:
            raise LLVMParseError(
                f"SSA value {value} has type {result.type}, expected {expected_type}",
                location,
            )
        return result
    if value.startswith("@"):
        raise UnsupportedConstructError(
            "global or function operand",
            "global variables and function references are not supported in v0.1",
            location,
        )
    if value in {"undef", "poison", "zeroinitializer"}:
        raise UnsupportedConstructError(
            value,
            f"{value} operands are not supported; qcc requires defined integer values",
            location,
        )
    if value in {"true", "false"}:
        if expected_type != IntType(1):
            raise LLVMParseError(
                f"boolean constant has type i1, expected {expected_type}", location
            )
        return IntConstant(expected_type, 1 if value == "true" else 0)
    if _FLOAT_WORD_RE.search(value) or re.search(r"[0-9]\.[0-9]", value):
        raise UnsupportedConstructError(
            "floating-point constant",
            "floating-point values are not supported; qcc v0.1 accepts "
            "integer constants only",
            location,
        )
    if _INTEGER_RE.fullmatch(value):
        return IntConstant(expected_type, _parse_integer(value, location))
    raise LLVMParseError(
        f"expected an SSA value or integer constant, got {value!r}", location
    )


def _result(
    lhs: str | None,
    result_type: IntType,
    values: dict[str, SSAValue],
    location: SourceLocation,
) -> SSAValue:
    if lhs is None:
        raise LLVMParseError(
            "value-producing instruction is missing an SSA result", location
        )
    name = _ssa_name(lhs, location)
    if name in values:
        raise LLVMParseError(f"SSA value %{name} is defined more than once", location)
    value = SSAValue(name, result_type)
    values[name] = value
    return value


def _unsupported_opcode(opcode: str, location: SourceLocation) -> None:
    if opcode in _CONTROL_FLOW_OPS:
        message = (
            f"unsupported LLVM instruction {opcode!r}; branches, multiple "
            "basic blocks, "
            "and cyclic control flow are not supported in v0.1"
        )
    elif opcode in _MEMORY_OPS:
        message = (
            f"unsupported LLVM instruction {opcode!r}; loads, stores, pointers, and "
            "memory operations are not supported in v0.1"
        )
    elif opcode == "call" or opcode.endswith("call"):
        message = (
            f"unsupported LLVM instruction {opcode!r}; function calls are not "
            "supported "
            "in the straight-line v0.1 subset"
        )
    elif opcode in _UNSUPPORTED_OPS:
        message = f"unsupported LLVM instruction {opcode!r} in the v0.1 integer subset"
    else:
        raise UnsupportedConstructError(
            f"unknown LLVM instruction {opcode!r}",
            f"unknown LLVM instruction {opcode!r}; the parser will not skip "
            "unsupported instructions",
            location,
        )
    raise UnsupportedConstructError(f"LLVM instruction {opcode!r}", message, location)


def _parse_instruction(
    line: str,
    *,
    source_name: str,
    line_number: int,
    values: dict[str, SSAValue],
    function_return_type: IntType | VoidType,
) -> Instruction:
    base, metadata = _extract_metadata(
        line.strip(),
        _location(source_name, line_number, line),
    )
    location = _location(source_name, line_number, line)
    result_name: str | None = None
    result_match = re.match(r"^(%[^\s=]+)\s*=\s*(.+)$", base)
    if result_match is not None:
        result_name = result_match.group(1)
        body = result_match.group(2).strip()
    else:
        body = base

    opcode_match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*)(?:\s+(.*))?$", body)
    if opcode_match is None:
        raise LLVMParseError(f"malformed LLVM instruction {line.strip()!r}", location)
    opcode = opcode_match.group(1).lower()
    rest = opcode_match.group(2) or ""

    if _FLOAT_WORD_RE.search(rest) or (opcode.startswith("f") and opcode != "freeze"):
        raise UnsupportedConstructError(
            "floating-point instruction",
            f"floating-point instruction {opcode!r} is not supported in v0.1",
            location,
        )
    if _VECTOR_RE.search(rest):
        raise UnsupportedConstructError(
            "vector instruction",
            f"vector operands or results in {opcode!r} are not supported in v0.1",
            location,
        )

    if opcode in _BINARY_OPS:
        match = re.match(
            r"^(?:(?P<flags>(?:(?:nuw|nsw|exact)\s+)+))?"
            r"(?P<type>i[1-9][0-9]*)\s+(?P<operands>.+)$",
            rest,
        )
        if match is None:
            raise LLVMParseError(f"malformed {opcode} instruction", location)
        value_type = _parse_type(match.group("type"), location)
        operands_text = _split_top_level(match.group("operands"))
        if len(operands_text) != 2 or any(not item for item in operands_text):
            raise LLVMParseError(f"{opcode} expects two operands", location)
        binary_operands = tuple(
            _operand(item, value_type, values, location) for item in operands_text
        )
        result = _result(result_name, value_type, values, location)
        flags = tuple((match.group("flags") or "").split())
        return Instruction(
            opcode,
            result=result,
            type=value_type,
            operands=binary_operands,
            flags=flags,
            source_location=location,
            metadata=metadata,
        )

    if opcode in _CAST_OPS:
        match = re.match(
            r"^(?:(?P<flags>(?:(?:nneg|nuw|nsw)\s+)+))?"
            r"(?P<source>i[1-9][0-9]*)\s+(?P<operand>.+?)\s+to\s+"
            r"(?P<target>i[1-9][0-9]*)$",
            rest,
        )
        if match is None:
            raise LLVMParseError(f"malformed {opcode} instruction", location)
        source_type = _parse_type(match.group("source"), location)
        target_type = _parse_type(match.group("target"), location)
        if opcode in {"zext", "sext"} and target_type.width <= source_type.width:
            raise LLVMParseError(
                f"{opcode} must widen {source_type} to a larger integer type",
                location,
            )
        if opcode == "trunc" and target_type.width >= source_type.width:
            raise LLVMParseError(
                f"trunc must narrow {source_type} to a smaller integer type",
                location,
            )
        operand = _operand(match.group("operand"), source_type, values, location)
        result = _result(result_name, target_type, values, location)
        return Instruction(
            opcode,
            result=result,
            type=target_type,
            operands=(operand,),
            flags=tuple((match.group("flags") or "").split()),
            operand_type=source_type,
            source_location=location,
            metadata=metadata,
        )

    if opcode == "icmp":
        match = re.match(
            r"^(?:(?P<flags>samesign)\s+)?"
            r"(?P<predicate>[A-Za-z]+)\s+(?P<type>i[1-9][0-9]*)\s+"
            r"(?P<operands>.+)$",
            rest,
        )
        if match is None:
            raise LLVMParseError("malformed icmp instruction", location)
        predicate = match.group("predicate").lower()
        if predicate not in _ICMP_PREDICATES:
            raise UnsupportedConstructError(
                f"icmp predicate {predicate!r}",
                f"icmp predicate {predicate!r} is not supported in v0.1",
                location,
            )
        value_type = _parse_type(match.group("type"), location)
        operands_text = _split_top_level(match.group("operands"))
        if len(operands_text) != 2:
            raise LLVMParseError("icmp expects two operands", location)
        icmp_operands = tuple(
            _operand(item, value_type, values, location) for item in operands_text
        )
        result = _result(result_name, IntType(1), values, location)
        return Instruction(
            "icmp",
            result=result,
            type=IntType(1),
            operands=icmp_operands,
            flags=tuple((match.group("flags") or "").split()),
            predicate=predicate,
            operand_type=value_type,
            source_location=location,
            metadata=metadata,
        )

    if opcode == "ret":
        if result_name is not None:
            raise LLVMParseError("ret cannot define an SSA result", location)
        if rest.strip() == "void":
            return_type: IntType | VoidType = VoidType()
            operands: tuple[SSAValue | IntConstant, ...] = ()
        else:
            match = re.match(
                r"^(?P<type>i[1-9][0-9]*)\s+(?P<operand>.+)$",
                rest,
            )
            if match is None:
                raise LLVMParseError(
                    "ret must return an integer value or void", location
                )
            return_type = _parse_type(match.group("type"), location)
            operands = (
                _operand(match.group("operand"), return_type, values, location),
            )
        if return_type != function_return_type:
            raise LLVMParseError(
                f"return type {return_type} does not match function return type "
                f"{function_return_type}",
                location,
            )
        return Instruction(
            "ret",
            type=return_type,
            operands=operands,
            source_location=location,
            metadata=metadata,
        )

    _unsupported_opcode(opcode, location)
    raise AssertionError("unreachable")


def _parse_parameters(
    text: str,
    *,
    source_name: str,
    line_number: int,
    location: SourceLocation,
) -> tuple[Parameter, ...]:
    del source_name, line_number
    if not text.strip():
        return ()
    parameters: list[Parameter] = []
    used_names: set[str] = set()
    next_implicit = 0
    for item in _split_top_level(text):
        if item == "...":
            raise UnsupportedConstructError(
                "variadic function parameter",
                "variadic functions are not supported in the integer v0.1 subset",
                location,
            )
        if "*" in item or re.search(r"\bptr\b", item):
            raise UnsupportedConstructError(
                "pointer function parameter",
                "pointer-typed function parameters are not supported in the "
                "integer v0.1 subset",
                location,
            )
        type_match = re.search(
            r"(?<![A-Za-z0-9_])i[1-9][0-9]*(?![A-Za-z0-9_])",
            item,
        )
        if type_match is None:
            if _FLOAT_WORD_RE.search(item) or _VECTOR_RE.search(item):
                raise UnsupportedConstructError(
                    "non-scalar function parameter",
                    "function parameters must be scalar integer values in v0.1",
                    location,
                )
            raise LLVMParseError(
                f"could not find an integer parameter type in {item!r}",
                location,
            )
        value_type = _parse_type(type_match.group(0), location)
        name_matches = re.findall(
            r"%(?:[A-Za-z$._-][A-Za-z0-9$._-]*|[0-9]+|\"(?:[^\"\\]|\\.)*\")",
            item,
        )
        if name_matches:
            name = _ssa_name(name_matches[-1], location)
        else:
            while str(next_implicit) in used_names:
                next_implicit += 1
            name = str(next_implicit)
            next_implicit += 1
        if name in used_names:
            raise LLVMParseError(
                f"function parameter %{name} is defined more than once",
                location,
            )
        used_names.add(name)
        attributes = tuple(
            token
            for token in item.replace(type_match.group(0), " ").split()
            if not token.startswith("%")
        )
        parameters.append(Parameter(name, value_type, attributes))
    return tuple(parameters)


def _parse_function_header(
    header: str,
    *,
    source_name: str,
    line_number: int,
) -> tuple[
    str,
    IntType | VoidType,
    tuple[Parameter, ...],
    tuple[str, ...],
    SourceLocation,
]:
    location = _location(source_name, line_number, header)
    if not header.rstrip().endswith("{"):
        raise LLVMParseError("function definition must open with '{'", location)
    content = header.rstrip()[:-1].rstrip()
    define_match = re.match(r"^define\s+(.+)$", content)
    if define_match is None:
        raise LLVMParseError("malformed function definition", location)
    definition = define_match.group(1).strip()
    at_index = definition.find("@")
    if at_index < 0:
        raise LLVMParseError("function definition is missing its @name", location)
    prefix = definition[:at_index].strip()
    name_match = re.match(
        r"@(?P<name>\"(?:[^\"\\]|\\.)*\"|[A-Za-z$._-][A-Za-z0-9$._-]*)",
        definition[at_index:],
    )
    if name_match is None:
        raise LLVMParseError("malformed function name", location)
    name = _normalize_name(name_match.group("name"), "@")
    after_name = definition[at_index + name_match.end() :].lstrip()
    if not after_name.startswith("("):
        raise LLVMParseError(
            "function definition is missing its parameter list",
            location,
        )
    close = _find_matching(after_name, 0)
    if close < 0:
        raise LLVMParseError("unterminated function parameter list", location)

    return_match = list(
        re.finditer(
            r"(?<![A-Za-z0-9_])(i[1-9][0-9]*|void)(?![A-Za-z0-9_])",
            prefix,
        )
    )
    if not return_match:
        if _FLOAT_WORD_RE.search(prefix):
            raise UnsupportedConstructError(
                "floating-point function return type",
                "floating-point function signatures are not supported in v0.1",
                location,
            )
        if _VECTOR_RE.search(prefix):
            raise UnsupportedConstructError(
                "vector function return type",
                "vector function signatures are not supported in v0.1",
                location,
            )
        if "ptr" in prefix or "*" in prefix:
            raise UnsupportedConstructError(
                "pointer function return type",
                "pointer-typed functions are not supported in the integer v0.1 subset",
                location,
            )
        raise LLVMParseError(
            "function definition is missing an integer return type",
            location,
        )
    return_text = return_match[-1].group(1)
    return_type: IntType | VoidType = (
        VoidType() if return_text == "void" else _parse_type(return_text, location)
    )
    parameters = _parse_parameters(
        after_name[1:close],
        source_name=source_name,
        line_number=line_number,
        location=location,
    )
    attributes_text = after_name[close + 1 :].strip()
    attributes = tuple(attributes_text.split()) if attributes_text else ()
    return name, return_type, parameters, attributes, location


def _parse_function(
    lines: list[str],
    start: int,
    *,
    source_name: str,
) -> tuple[Function, int]:
    line_number = start + 1
    header = _strip_comment(lines[start]).strip()
    cursor = start
    while not header.rstrip().endswith("{"):
        cursor += 1
        if cursor >= len(lines):
            raise LLVMParseError(
                "unterminated function header",
                _location(source_name, line_number, lines[start]),
            )
        header += " " + _strip_comment(lines[cursor]).strip()
    name, return_type, parameters, attributes, function_location = (
        _parse_function_header(
            header,
            source_name=source_name,
            line_number=line_number,
        )
    )
    values: dict[str, SSAValue] = {
        parameter.name: parameter for parameter in parameters
    }
    block_name = "entry"
    block_location = function_location
    instructions: list[Instruction] = []
    saw_label = False
    saw_return = False
    cursor += 1
    while cursor < len(lines):
        current = _strip_comment(lines[cursor])
        stripped = current.strip()
        current_line = cursor + 1
        if not stripped:
            cursor += 1
            continue
        if stripped == "}":
            if not saw_return:
                raise LLVMParseError(
                    f"function @{name} ends without a return instruction",
                    _location(source_name, current_line, current),
                )
            block = BasicBlock(block_name, tuple(instructions), block_location)
            return (
                Function(
                    name,
                    return_type,
                    parameters,
                    (block,),
                    attributes,
                    function_location,
                ),
                cursor + 1,
            )
        label_match = re.fullmatch(
            r"([A-Za-z$._-][A-Za-z0-9$._-]*|[0-9]+):",
            stripped,
        )
        if label_match is not None:
            if saw_label:
                raise UnsupportedConstructError(
                    "multiple basic blocks",
                    "multiple basic blocks and branches/cycles are not supported "
                    "in v0.1",
                    _location(source_name, current_line, current),
                )
            saw_label = True
            block_name = label_match.group(1)
            block_location = _location(source_name, current_line, current)
            cursor += 1
            continue
        if saw_return:
            raise LLVMParseError(
                "instructions are not allowed after a return terminator",
                _location(source_name, current_line, current),
            )
        instruction = _parse_instruction(
            current,
            source_name=source_name,
            line_number=current_line,
            values=values,
            function_return_type=return_type,
        )
        instructions.append(instruction)
        saw_return = instruction.opcode == "ret"
        cursor += 1

    raise LLVMParseError(
        f"function @{name} is missing its closing '}}'",
        function_location,
    )


def parse_module(text: str, source_name: str = "<llvm>") -> Module:
    """Parse v0.1 integer straight-line LLVM IR into a :class:`Module`."""

    if not isinstance(text, str):
        raise TypeError("LLVM input must be text")
    # Run the lexical pass first so malformed quoting and locations are reported
    # consistently even for module-level constructs the line parser ignores.
    tokenize(text, source_name)
    lines = text.splitlines()
    functions: list[Function] = []
    module_metadata: list[str] = []
    target_triple: str | None = None
    cursor = 0
    while cursor < len(lines):
        raw = _strip_comment(lines[cursor])
        stripped = raw.strip()
        line_number = cursor + 1
        if not stripped:
            cursor += 1
            continue
        if stripped.startswith("define "):
            function, cursor = _parse_function(lines, cursor, source_name=source_name)
            if any(existing.name == function.name for existing in functions):
                raise LLVMParseError(
                    f"function @{function.name} is defined more than once",
                    function.source_location
                    or SourceLocation(source_name, line_number),
                )
            functions.append(function)
            continue
        if stripped.startswith("declare "):
            raise UnsupportedConstructError(
                "function declaration",
                "function declarations/calls are not supported in the "
                "straight-line v0.1 subset",
                _location(source_name, line_number, raw),
            )
        if stripped.startswith("target triple"):
            target_match = re.search(r'"(.*)"', stripped)
            if target_match:
                target_triple = target_match.group(1)
            module_metadata.append(stripped)
            cursor += 1
            continue
        if stripped.startswith(
            (
                "source_filename",
                "target datalayout",
                "module asm",
                "attributes ",
                "!",
                "uselistorder",
                "comdat ",
                "$",
            )
        ):
            module_metadata.append(stripped)
            cursor += 1
            continue
        if stripped.startswith("@") or re.match(
            r"^\$?[A-Za-z_.$][A-Za-z0-9_.$-]*\s*=",
            stripped,
        ):
            raise UnsupportedConstructError(
                "global definition",
                "global variables and global initializers are not supported in v0.1",
                _location(source_name, line_number, raw),
            )
        raise LLVMParseError(
            f"unrecognized top-level LLVM construct {stripped.split()[0]!r}",
            _location(source_name, line_number, raw),
        )

    if not functions:
        raise LLVMParseError(
            "LLVM module does not contain a function definition",
            SourceLocation(source_name),
        )
    return Module(
        FunctionCollection(functions),
        source_name=source_name,
        target_triple=target_triple,
        metadata=tuple(module_metadata),
    )


def lookup_function(module: Module, name: str) -> Function:
    """Return a named function, accepting either ``foo`` or ``@foo``."""

    return module.get_function(name)


get_function = lookup_function
parse = parse_module


__all__ = [
    "Token",
    "get_function",
    "lookup_function",
    "parse",
    "parse_module",
    "tokenize",
]
