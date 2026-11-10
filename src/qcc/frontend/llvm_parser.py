"""Small, strict parser for the bounded textual LLVM subset.

LLVM's textual format is intentionally broad and changes slightly between
Clang releases. This parser therefore ignores harmless module attributes and
metadata declarations, but validates every instruction it sees. The accepted
program shape is an integer-typed function with an acyclic control-flow graph.

Direct calls and the integer intrinsics that newer Clang releases emit for
ordinary C (``llvm.umin`` for ``a < b ? a : b`` and friends) are parsed as
``call`` instructions and then expanded by :mod:`qcc.frontend.legalize`, so
every later stage only ever sees primitive integer operations.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from qcc.errors import (
    CompilerError,
    LLVMParseError,
    SourceLocation,
    UnsupportedConstructError,
)
from qcc.ir.ssa import (
    BasicBlock,
    Function,
    FunctionCollection,
    Instruction,
    IntConstant,
    IntType,
    MetadataAttachment,
    Module,
    Operand,
    Parameter,
    PhiIncoming,
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
_BLOCK_NAME_PATTERN = r'(?:[A-Za-z$._-][A-Za-z0-9$._-]*|[0-9]+|"(?:[^"\\]|\\.)*")'
_BLOCK_LABEL_RE = re.compile(rf"^(?P<name>{_BLOCK_NAME_PATTERN}):$")
_FLOAT_WORD_RE = re.compile(
    r"(?:^|\s)(?:half|bfloat|float|double|fp128|x86_fp80)(?:\s|$)"
)
_VECTOR_RE = re.compile(r"<\s*[0-9]+\s*x\s*[^>]+>")

_BINARY_OPS = frozenset(
    {"add", "sub", "mul", "and", "or", "xor", "shl", "lshr", "ashr", "udiv", "urem"}
)
_CAST_OPS = frozenset({"zext", "sext", "trunc"})
_ICMP_PREDICATES = frozenset(
    {"eq", "ne", "ugt", "uge", "ult", "ule", "sgt", "sge", "slt", "sle"}
)
_CONTROL_FLOW_OPS = frozenset(
    {
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
        "sdiv",
        "srem",
        "extractvalue",
        "insertvalue",
        "extractelement",
        "insertelement",
        "shufflevector",
        "va_arg",
        "landingpad",
    }
)

# Integer intrinsics with a primitive expansion in qcc.frontend.legalize,
# mapped to their argument count.  The overload suffix (``.i8``) is dropped
# when the call is parsed.
SUPPORTED_INTRINSICS: dict[str, int] = {
    "llvm.umin": 2,
    "llvm.umax": 2,
    "llvm.smin": 2,
    "llvm.smax": 2,
    "llvm.abs": 2,
    "llvm.ctpop": 1,
    "llvm.ctlz": 2,
    "llvm.cttz": 2,
    "llvm.bswap": 1,
    "llvm.bitreverse": 1,
    "llvm.fshl": 3,
    "llvm.fshr": 3,
    "llvm.uadd.sat": 2,
    "llvm.usub.sat": 2,
}
# Intrinsics that carry no value semantics and can be dropped.
_IGNORED_INTRINSIC_PREFIXES = (
    "llvm.lifetime.",
    "llvm.dbg.",
    "llvm.assume",
    "llvm.donothing",
    "llvm.experimental.noalias.scope.decl",
    "llvm.pseudoprobe",
)
_CALL_RE = re.compile(r"^(?:(?:tail|musttail|notail)\s+)?call\b\s*(?P<rest>.*)$")
_CALLEE_RE = re.compile(
    r"(?P<callee>[@%](?:\"(?:[^\"\\]|\\.)*\"|[A-Za-z0-9$._-]+))\s*\("
)
_INLINE_ASM_RE = re.compile(r"(?:^|\s)asm\s")
_ATTRIBUTE_GROUP_RE = re.compile(r"\b[A-Za-z_]+\([^()]*\)")


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
                "floating-point values are not supported; qcc accepts "
                "integer types only",
                location,
            )
        if _VECTOR_RE.search(value):
            raise UnsupportedConstructError(
                "vector type",
                "vector types are not supported; qcc accepts scalar integer types only",
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


def _llvm_string(value: str) -> str:
    """Decode the small quoted-string subset used by LLVM debug metadata."""

    try:
        return bytes(value, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return value


def _debug_locations(text: str, default_source: str) -> dict[int, SourceLocation]:
    """Extract ``!dbg`` locations from Clang's line-tables-only metadata.

    Debug metadata is intentionally parsed as a best-effort side table.  It is
    not part of qcc's semantic LLVM subset, but it lets an unsupported IR
    instruction point back to the original C file and line instead of an
    implementation-detail line in the generated ``.ll`` text.
    """

    files: dict[int, str] = {}
    scopes: dict[int, tuple[int, int | None]] = {}
    for raw in text.splitlines():
        file_match = re.match(
            r"^!(\d+)\s*=\s*!DIFile\(.*?filename:\s*\"((?:\\.|[^\"])*)\""
            r"(?:,\s*directory:\s*\"((?:\\.|[^\"])*)\")?",
            raw,
        )
        if file_match is not None:
            identifier = int(file_match.group(1))
            filename = _llvm_string(file_match.group(2))
            directory = _llvm_string(file_match.group(3) or "")
            if not Path(filename).is_absolute() and directory:
                filename = str(Path(directory) / filename)
            files[identifier] = filename or default_source
            continue
        scope_match = re.match(
            r"^!(\d+)\s*=\s*(?:distinct\s+)?!DISubprogram\(.*?"
            r"file:\s*!(\d+)(?:.*?line:\s*(\d+))?",
            raw,
        )
        if scope_match is not None:
            scopes[int(scope_match.group(1))] = (
                int(scope_match.group(2)),
                int(scope_match.group(3)) if scope_match.group(3) else None,
            )

    locations: dict[int, SourceLocation] = {}
    for scope_identifier, (_file_identifier, scope_line) in scopes.items():
        if scope_line is not None:
            locations[scope_identifier] = SourceLocation(default_source, scope_line, 1)
    for raw in text.splitlines():
        location_match = re.match(
            r"^!(\d+)\s*=\s*(?:distinct\s+)?!DILocation\("
            r".*?line:\s*(\d+),\s*column:\s*(\d+),\s*scope:\s*!(\d+)",
            raw,
        )
        if location_match is None:
            continue
        identifier = int(location_match.group(1))
        line = int(location_match.group(2))
        column = int(location_match.group(3))
        locations[identifier] = SourceLocation(default_source, line, column)
    return locations


def _location_from_dbg(
    attachments: tuple[MetadataAttachment, ...],
    fallback: SourceLocation,
    locations: Mapping[int, SourceLocation] | None,
) -> SourceLocation:
    """Resolve the first ``!dbg`` attachment, if the side table has it."""

    if locations is None:
        return fallback
    for attachment in attachments:
        if attachment.name != "dbg":
            continue
        match = re.fullmatch(r"!(\d+)", attachment.node)
        if match is not None and int(match.group(1)) in locations:
            return locations[int(match.group(1))]
    return fallback


def _source_keyword_location(
    source_name: str,
    keywords: tuple[str, ...],
    fallback: SourceLocation,
    *,
    after_line: int | None = None,
) -> SourceLocation:
    """Use a source declaration line when LLVM has no attachment for an op."""

    if source_name in {"<llvm>", "<stdin>"}:
        return fallback
    try:
        source_lines = Path(source_name).read_text().splitlines()
    except OSError:
        return fallback
    for line_number, source_line in enumerate(source_lines, 1):
        if after_line is not None and line_number <= after_line:
            continue
        for keyword in keywords:
            pattern = (
                rf"\b{re.escape(keyword)}\b"
                if keyword.isidentifier()
                else re.escape(keyword)
            )
            match = re.search(pattern, source_line)
            if match is not None:
                return SourceLocation(source_name, line_number, match.start() + 1)
    return fallback


def _operand(
    text: str,
    expected_type: IntType,
    values: dict[str, SSAValue],
    location: SourceLocation,
    *,
    forward_names: set[str] | None = None,
) -> SSAValue | IntConstant:
    value = text.strip()
    if value.startswith("%"):
        name = _ssa_name(value, location)
        result = values.get(name)
        if result is None:
            if forward_names is None:
                raise LLVMParseError(f"use of undefined SSA value {value}", location)
            result = SSAValue(name, expected_type)
            values[name] = result
            forward_names.add(name)
        if result.type != expected_type:
            raise LLVMParseError(
                f"SSA value {value} has type {result.type}, expected {expected_type}",
                location,
            )
        return result
    if value.startswith("@"):
        raise UnsupportedConstructError(
            "global or function operand",
            "global variables and function references are not supported",
            location,
        )
    if value in {"undef", "poison", "zeroinitializer"}:
        if value == "poison":
            zero_location = _source_keyword_location(
                location.source_name,
                ("/ 0", "% 0", "/0", "%0"),
                location,
            )
            if zero_location != location:
                raise UnsupportedConstructError(
                    "division by zero",
                    "division or remainder by zero is undefined and cannot be "
                    "compiled; use a nonzero compile-time divisor",
                    zero_location,
                )
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
            "floating-point values are not supported; qcc accepts "
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
    *,
    forward_names: set[str] | None = None,
) -> SSAValue:
    if lhs is None:
        raise LLVMParseError(
            "value-producing instruction is missing an SSA result", location
        )
    name = _ssa_name(lhs, location)
    existing = values.get(name)
    if existing is not None:
        if forward_names is None or name not in forward_names:
            raise LLVMParseError(
                f"SSA value %{name} is defined more than once", location
            )
        if existing.type != result_type:
            raise LLVMParseError(
                f"forward SSA value %{name} has type {existing.type}, "
                f"defined as {result_type}",
                location,
            )
        forward_names.remove(name)
    value = SSAValue(name, result_type)
    values[name] = value
    return value


def _unsupported_opcode(opcode: str, location: SourceLocation) -> None:
    if opcode in {"sdiv", "srem"}:
        message = (
            f"unsupported LLVM instruction {opcode!r}; signed division and "
            "remainder are not supported; use unsigned division or remainder "
            "by a nonzero constant"
        )
    elif opcode == "call" or opcode.endswith("call") or opcode in {"invoke", "callbr"}:
        message = (
            f"unsupported LLVM instruction {opcode!r}; only direct calls to "
            "functions defined in the same file are supported"
        )
    elif opcode in _CONTROL_FLOW_OPS:
        message = (
            f"unsupported LLVM instruction {opcode!r}; only br/ret control flow "
            "with an acyclic CFG is supported"
        )
    elif opcode in _MEMORY_OPS:
        message = (
            f"unsupported LLVM instruction {opcode!r}; loads, stores, pointers, "
            "and memory operations are not supported"
        )
    elif opcode in _UNSUPPORTED_OPS:
        message = (
            f"unsupported LLVM instruction {opcode!r}; it is outside the integer subset"
        )
    else:
        raise UnsupportedConstructError(
            f"unknown LLVM instruction {opcode!r}",
            f"unknown LLVM instruction {opcode!r}; the parser will not skip "
            "unsupported instructions",
            location,
        )
    raise UnsupportedConstructError(f"LLVM instruction {opcode!r}", message, location)


def _block_name(value: str, location: SourceLocation) -> str:
    """Parse an LLVM block reference such as ``%then``."""

    value = value.strip()
    if not value.startswith("%"):
        raise LLVMParseError(f"expected a basic-block label, got {value!r}", location)
    raw = value[1:]
    if re.fullmatch(_BLOCK_NAME_PATTERN, raw) is None:
        raise LLVMParseError(f"invalid basic-block label {value!r}", location)
    return _normalize_name(value)


def _label_target(value: str, location: SourceLocation) -> str:
    match = re.fullmatch(r"label\s+(.+)", value.strip())
    if match is None:
        raise LLVMParseError(f"expected a label target, got {value!r}", location)
    return _block_name(match.group(1), location)


def _typed_operand(
    text: str,
    values: dict[str, SSAValue],
    location: SourceLocation,
) -> tuple[IntType, SSAValue | IntConstant]:
    match = re.match(r"^(?P<type>i[1-9][0-9]*)\s+(?P<operand>.+)$", text.strip())
    if match is None:
        raise LLVMParseError(
            f"expected a typed integer operand, got {text!r}", location
        )
    value_type = _parse_type(match.group("type"), location)
    return value_type, _operand(match.group("operand"), value_type, values, location)


def _call_return_type(prefix: str) -> str:
    """Classify the return type named before a call's callee."""

    cleaned = _ATTRIBUTE_GROUP_RE.sub(" ", prefix)
    for token in cleaned.split():
        if _TYPE_RE.fullmatch(token):
            return token
        if token == "void":
            return "void"
        if token == "ptr" or token.endswith("*"):
            return "ptr"
        if token in {"half", "bfloat", "float", "double", "fp128", "x86_fp80"}:
            return "float"
        if token[0] in "<{[%":
            return "aggregate"
    return "unknown"


def _parse_call(
    rest: str,
    result_name: str | None,
    values: dict[str, SSAValue],
    location: SourceLocation,
    metadata: tuple[MetadataAttachment, ...],
    *,
    forward_names: set[str] | None,
    defined_functions: frozenset[str],
) -> Instruction | None:
    """Parse a direct call to a supported intrinsic or a defined function."""

    if _INLINE_ASM_RE.search(rest.split('"', 1)[0]):
        raise UnsupportedConstructError(
            "inline asm",
            "inline asm is not supported; qcc compiles pure integer functions "
            "and cannot give a machine instruction a reversible meaning",
            location,
        )
    callee_match = _CALLEE_RE.search(rest)
    if callee_match is None:
        raise UnsupportedConstructError(
            "LLVM instruction 'call'",
            "unsupported LLVM instruction 'call'; function calls are only "
            "supported when they name a function defined in the same file",
            location,
        )
    callee_text = callee_match.group("callee")
    if callee_text.startswith("%"):
        raise UnsupportedConstructError(
            "function pointer",
            f"indirect call through {callee_text}; function pointers are not "
            "supported because the callee must be known at compile time",
            location,
        )
    callee = _normalize_name(callee_text, "@")
    if callee.startswith(_IGNORED_INTRINSIC_PREFIXES):
        return None

    intrinsic: str | None = None
    if callee.startswith("llvm."):
        intrinsic = next(
            (
                name
                for name in SUPPORTED_INTRINSICS
                if callee == name or callee.startswith(f"{name}.")
            ),
            None,
        )
        if intrinsic is None:
            raise UnsupportedConstructError(
                f"intrinsic {callee}",
                f"unsupported LLVM intrinsic call @{callee}; supported integer "
                "intrinsics are "
                + ", ".join(sorted(name[5:] for name in SUPPORTED_INTRINSICS)),
                location,
            )
    elif callee not in defined_functions:
        raise UnsupportedConstructError(
            f"call to external function {callee}",
            f"call to external function @{callee}; function calls are only "
            "supported when the callee is defined in the same file, because "
            "qcc inlines the callee and an external function may have side "
            "effects",
            location,
        )

    return_kind = _call_return_type(rest[: callee_match.start()])
    if _TYPE_RE.fullmatch(return_kind) is None:
        raise UnsupportedConstructError(
            f"call returning {return_kind}",
            f"call to @{callee} returns {return_kind}; only calls that return "
            "one fixed-width integer are supported",
            location,
        )
    return_type = _parse_type(return_kind, location)
    open_index = callee_match.end() - 1
    close_index = _find_matching(rest, open_index)
    if close_index < 0:
        raise LLVMParseError("unterminated call argument list", location)
    argument_text = rest[open_index + 1 : close_index].strip()
    operands: list[SSAValue | IntConstant] = []
    for item in _split_top_level(argument_text) if argument_text else []:
        tokens = _ATTRIBUTE_GROUP_RE.sub(" ", item).split()
        if len(tokens) < 2 or _TYPE_RE.fullmatch(tokens[0]) is None:
            raise UnsupportedConstructError(
                "non-integer call argument",
                f"call to @{callee} passes {item!r}; only fixed-width integer "
                "arguments are supported",
                location,
            )
        argument_type = _parse_type(tokens[0], location)
        operands.append(_operand(tokens[-1], argument_type, values, location))
    if intrinsic is not None and len(operands) != SUPPORTED_INTRINSICS[intrinsic]:
        raise LLVMParseError(
            f"@{callee} expects {SUPPORTED_INTRINSICS[intrinsic]} argument(s), "
            f"got {len(operands)}",
            location,
        )
    result = _result(
        result_name, return_type, values, location, forward_names=forward_names
    )
    return Instruction(
        "call",
        result=result,
        type=return_type,
        operands=tuple(operands),
        source_location=location,
        metadata=metadata,
        callee=intrinsic or callee,
    )


def _parse_instruction(
    line: str,
    *,
    source_name: str,
    line_number: int,
    values: dict[str, SSAValue],
    function_return_type: IntType | VoidType,
    forward_names: set[str] | None = None,
    debug_locations: Mapping[int, SourceLocation] | None = None,
    defined_functions: frozenset[str] = frozenset(),
    fallback_location: SourceLocation | None = None,
) -> Instruction | None:
    """Parse one instruction line; ``None`` means a value-free no-op.

    ``fallback_location`` is the position reported for an instruction that has
    no usable ``!dbg`` attachment.  Without it the position is the line of the
    LLVM text itself, which is only right when that text is the user's input.
    """

    text_location = _location(source_name, line_number, line)
    base, metadata = _extract_metadata(line.strip(), text_location)
    location = _location_from_dbg(
        metadata,
        fallback_location or text_location,
        debug_locations,
    )
    result_name: str | None = None
    result_match = re.match(r"^(%[^\s=]+)\s*=\s*(.+)$", base)
    if result_match is not None:
        result_name = result_match.group(1)
        body = result_match.group(2).strip()
    else:
        body = base

    call_match = _CALL_RE.match(body)
    if call_match is not None:
        return _parse_call(
            call_match.group("rest"),
            result_name,
            values,
            location,
            metadata,
            forward_names=forward_names,
            defined_functions=defined_functions,
        )

    opcode_match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*)(?:\s+(.*))?$", body)
    if opcode_match is None:
        raise LLVMParseError(f"malformed LLVM instruction {line.strip()!r}", location)
    opcode = opcode_match.group(1).lower()
    rest = opcode_match.group(2) or ""

    if _FLOAT_WORD_RE.search(rest) or (opcode.startswith("f") and opcode != "freeze"):
        raise UnsupportedConstructError(
            "floating-point instruction",
            f"floating-point instruction {opcode!r} is not supported; qcc "
            "accepts integer operations only",
            location,
        )
    if _VECTOR_RE.search(rest):
        raise UnsupportedConstructError(
            "vector instruction",
            f"vector operands or results in {opcode!r} are not supported; "
            "qcc accepts scalar integer operations only",
            location,
        )

    if opcode == "unreachable":
        # Kept only until the function's blocks are finished: a block that
        # nothing branches to is dropped there, and any other is rejected.
        return Instruction("unreachable", source_location=location, metadata=metadata)

    if opcode in _BINARY_OPS:
        match = re.match(
            r"^(?:(?P<flags>(?:(?:nuw|nsw|exact|disjoint)\s+)+))?"
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
        result = _result(
            result_name, value_type, values, location, forward_names=forward_names
        )
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
        result = _result(
            result_name, target_type, values, location, forward_names=forward_names
        )
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
                f"icmp predicate {predicate!r} is not supported; qcc accepts "
                "the ten integer predicates only",
                location,
            )
        value_type = _parse_type(match.group("type"), location)
        operands_text = _split_top_level(match.group("operands"))
        if len(operands_text) != 2 or any(not item for item in operands_text):
            raise LLVMParseError("icmp expects two operands", location)
        icmp_operands = tuple(
            _operand(item, value_type, values, location) for item in operands_text
        )
        result = _result(
            result_name, IntType(1), values, location, forward_names=forward_names
        )
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

    if opcode == "select":
        if result_name is None:
            raise LLVMParseError("select cannot omit its SSA result", location)
        parts = _split_top_level(rest)
        if len(parts) != 3 or any(not item for item in parts):
            raise LLVMParseError("select expects a condition and two values", location)
        condition_type, condition = _typed_operand(parts[0], values, location)
        if condition_type != IntType(1):
            raise LLVMParseError(
                f"select condition must have type i1, got {condition_type}", location
            )
        true_type, true_value = _typed_operand(parts[1], values, location)
        false_type, false_value = _typed_operand(parts[2], values, location)
        if true_type != false_type:
            raise LLVMParseError(
                f"select values have different types {true_type} and {false_type}",
                location,
            )
        result = _result(
            result_name, true_type, values, location, forward_names=forward_names
        )
        return Instruction(
            "select",
            result=result,
            type=true_type,
            operands=(condition, true_value, false_value),
            source_location=location,
            metadata=metadata,
        )

    if opcode == "phi":
        if result_name is None:
            raise LLVMParseError("phi cannot omit its SSA result", location)
        match = re.match(
            r"^(?P<type>i[1-9][0-9]*)\s+(?P<incoming>.+)$",
            rest,
        )
        if match is None:
            raise LLVMParseError("malformed phi instruction", location)
        value_type = _parse_type(match.group("type"), location)
        incoming: list[PhiIncoming] = []
        phi_operands: list[SSAValue | IntConstant] = []
        for item in _split_top_level(match.group("incoming")):
            if len(item) < 2 or not item.startswith("[") or not item.endswith("]"):
                raise LLVMParseError(f"malformed phi incoming edge {item!r}", location)
            pair = _split_top_level(item[1:-1])
            if len(pair) != 2 or any(not value.strip() for value in pair):
                raise LLVMParseError(f"malformed phi incoming edge {item!r}", location)
            value = _operand(
                pair[0],
                value_type,
                values,
                location,
                forward_names=forward_names,
            )
            block = _block_name(pair[1], location)
            phi_operands.append(value)
            incoming.append(PhiIncoming(value, block, location))
        if not incoming:
            raise LLVMParseError("phi expects at least one incoming edge", location)
        result = _result(
            result_name, value_type, values, location, forward_names=forward_names
        )
        return Instruction(
            "phi",
            result=result,
            type=value_type,
            operands=tuple(phi_operands),
            source_location=location,
            metadata=metadata,
            incoming=tuple(incoming),
        )

    if opcode == "br":
        if result_name is not None:
            raise LLVMParseError("br cannot define an SSA result", location)
        parts = _split_top_level(rest)
        if len(parts) == 1:
            target = _label_target(parts[0], location)
            return Instruction(
                "br",
                source_location=location,
                metadata=metadata,
                targets=(target,),
            )
        if len(parts) != 3:
            raise LLVMParseError(
                "br expects one label or an i1 condition and two labels", location
            )
        condition_type, condition = _typed_operand(parts[0], values, location)
        if condition_type != IntType(1):
            raise LLVMParseError(
                f"br condition must have type i1, got {condition_type}", location
            )
        targets = (_label_target(parts[1], location), _label_target(parts[2], location))
        if targets[0] == targets[1]:
            raise LLVMParseError(
                "conditional br must name two distinct destination blocks", location
            )
        return Instruction(
            "br",
            type=IntType(1),
            operands=(condition,),
            source_location=location,
            metadata=metadata,
            targets=targets,
        )

    if opcode == "ret":
        if result_name is not None:
            raise LLVMParseError("ret cannot define an SSA result", location)
        if rest.strip() == "void":
            return_type: IntType | VoidType = VoidType()
            return_operands: tuple[SSAValue | IntConstant, ...] = ()
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
            return_operands = (
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
            operands=return_operands,
            source_location=location,
            metadata=metadata,
        )

    if opcode == "freeze":
        # ``freeze`` pins an undef/poison operand to one arbitrary value.  A
        # reversible circuit computes a definite bit pattern on every wire, so
        # the identity is a valid refinement.
        value_type, frozen = _typed_operand(rest, values, location)
        result = _result(
            result_name, value_type, values, location, forward_names=forward_names
        )
        return Instruction(
            "copy",
            result=result,
            type=value_type,
            operands=(frozen,),
            source_location=location,
            metadata=metadata,
        )

    if opcode == "alloca":
        location = _source_keyword_location(
            source_name,
            ("volatile", "_Atomic", "atomic_"),
            location,
        )
    _unsupported_opcode(opcode, location)
    raise AssertionError("unreachable")


def _parse_parameters(
    text: str,
    *,
    source_name: str,
    line_number: int,
    location: SourceLocation,
) -> tuple[tuple[Parameter, ...], tuple[str, ...]]:
    """Parse a parameter list into integer parameters and pointer names.

    A pointer parameter is not an error by itself: the caller reports it
    against the instruction that first uses it, which is where the user's
    source actually touches memory.
    """

    if not text.strip():
        return (), ()
    parameters: list[Parameter] = []
    pointers: list[str] = []
    used_names: set[str] = set()
    next_implicit = 0
    name_pattern = r"%(?:[A-Za-z$._-][A-Za-z0-9$._-]*|[0-9]+|\"(?:[^\"\\]|\\.)*\")"
    for item in _split_top_level(text):
        if item == "...":
            raise UnsupportedConstructError(
                "variadic function parameter",
                "variadic functions are not supported",
                location,
            )
        name_matches = re.findall(name_pattern, item)
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
        if "*" in item or re.search(r"\bptr\b", item):
            pointers.append(name)
            continue
        type_match = re.search(
            r"(?<![A-Za-z0-9_])i[1-9][0-9]*(?![A-Za-z0-9_])",
            item,
        )
        if type_match is None:
            if _FLOAT_WORD_RE.search(item) or _VECTOR_RE.search(item):
                raise UnsupportedConstructError(
                    "non-scalar function parameter",
                    "function parameters must be scalar integer values",
                    location,
                )
            raise LLVMParseError(
                f"could not find an integer parameter type in {item!r}",
                location,
            )
        value_type = _parse_type(type_match.group(0), location)
        attributes = tuple(
            token
            for token in item.replace(type_match.group(0), " ").split()
            if not token.startswith("%")
        )
        parameters.append(Parameter(name, value_type, attributes))
    return tuple(parameters), tuple(pointers)


def _parse_function_header(
    header: str,
    *,
    source_name: str,
    line_number: int,
    debug_locations: Mapping[int, SourceLocation] | None = None,
) -> tuple[
    str,
    IntType | VoidType,
    tuple[Parameter, ...],
    tuple[str, ...],
    SourceLocation,
    tuple[str, ...],
]:
    header_location = _location(source_name, line_number, header)
    _, header_metadata = _extract_metadata(header, header_location)
    location = _location_from_dbg(header_metadata, header_location, debug_locations)
    if location == header_location and debug_locations is not None:
        debug_match = re.search(r"(?:,|\s)!dbg\s+!(\d+)", header)
        if debug_match is not None:
            location = debug_locations.get(int(debug_match.group(1)), location)
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
                "floating-point function signatures are not supported",
                location,
            )
        if _VECTOR_RE.search(prefix):
            raise UnsupportedConstructError(
                "vector function return type",
                "vector function signatures are not supported",
                location,
            )
        if "ptr" in prefix or "*" in prefix:
            raise UnsupportedConstructError(
                "pointer function return type",
                "pointer-typed functions are not supported",
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
    parameters, pointers = _parse_parameters(
        after_name[1:close],
        source_name=source_name,
        line_number=line_number,
        location=location,
    )
    attributes_text = after_name[close + 1 :].strip()
    attributes = tuple(attributes_text.split()) if attributes_text else ()
    return name, return_type, parameters, attributes, location, pointers


def _reject_pointer_parameters(
    lines: list[str],
    body_start: int,
    pointers: tuple[str, ...],
    *,
    source_name: str,
    function_location: SourceLocation,
    debug_locations: Mapping[int, SourceLocation] | None,
) -> None:
    """Raise the diagnostic for a function that takes pointer parameters.

    The error points at the first instruction that uses a pointer, so an
    indexed load is reported on the source line that dereferences it rather
    than on the function signature.
    """

    for index in range(body_start, len(lines)):
        body_line = _strip_comment(lines[index])
        if body_line.strip() == "}":
            break
        for name in pointers:
            use = re.search(
                rf"%(?:{re.escape(name)}|\"{re.escape(name)}\")(?![A-Za-z0-9$._-])",
                body_line,
            )
            if use is None:
                continue
            if _CALL_RE.match(body_line.split("=", 1)[-1].strip()) and re.match(
                r"\s*\(", body_line[use.end() :]
            ):
                raise UnsupportedConstructError(
                    "function pointer",
                    f"indirect call through %{name}; function pointers are not "
                    "supported because the callee must be known at compile time",
                    function_location,
                )
            fallback = _location(source_name, index + 1, body_line)
            _, attachments = _extract_metadata(body_line.strip(), fallback)
            raise UnsupportedConstructError(
                "unresolved pointer",
                f"memory access through pointer parameter %{name}; unresolved "
                "pointers and variable-index memory access are not supported",
                _location_from_dbg(attachments, fallback, debug_locations),
            )
    raise UnsupportedConstructError(
        "pointer parameter",
        f"pointer parameter %{pointers[0]}; pointers and memory are not "
        "supported, pass fixed-width integers by value",
        function_location,
    )


def _validate_function(function: Function) -> None:
    """Validate CFG edges, phi predecessor sets, and acyclicity."""

    block_names = {block.name for block in function.blocks}
    for block in function.blocks:
        terminator = block.terminator
        if terminator is None:
            location = block.location or function.location
            raise LLVMParseError(
                f"basic block %{block.name} does not end in a terminator",
                location,
            )
        if terminator.opcode == "br":
            for target in terminator.targets:
                if target not in block_names:
                    raise LLVMParseError(
                        f"branch from %{block.name} targets undefined basic "
                        f"block %{target}",
                        terminator.location or block.location,
                    )

    state: dict[str, int] = {}

    def visit(block: BasicBlock) -> None:
        state[block.name] = 1
        for target in block.successors:
            target_state = state.get(target, 0)
            if target_state == 1:
                terminator = block.terminator
                location = (
                    terminator.location
                    if terminator is not None
                    else block.location or function.location
                )
                raise UnsupportedConstructError(
                    "cyclic control flow",
                    "cyclic control flow (data-dependent loop) is not supported; "
                    f"branch from %{block.name} to %{target} creates a cycle",
                    location,
                )
            if target_state == 0:
                visit(function.get_block(target))
        state[block.name] = 2

    for block in function.blocks:
        if state.get(block.name, 0) == 0:
            visit(block)

    for block in function.blocks:
        predecessors = set(function.predecessor_names(block))
        for instruction in block.instructions:
            if instruction.opcode != "phi":
                continue
            seen_incoming: set[str] = set()
            for edge in instruction.incoming:
                if edge.block in seen_incoming:
                    raise LLVMParseError(
                        f"phi in %{block.name} lists predecessor %{edge.block} "
                        "more than once",
                        instruction.location or block.location,
                    )
                seen_incoming.add(edge.block)
                if edge.block not in block_names:
                    raise LLVMParseError(
                        f"phi in %{block.name} references undefined basic "
                        f"block %{edge.block}",
                        instruction.location or block.location,
                    )
            if seen_incoming != predecessors:
                missing = sorted(predecessors - seen_incoming)
                extra = sorted(seen_incoming - predecessors)
                details: list[str] = []
                if missing:
                    details.append(
                        "missing " + ", ".join(f"%{name}" for name in missing)
                    )
                if extra:
                    details.append(
                        "not-predecessors " + ", ".join(f"%{name}" for name in extra)
                    )
                detail = "; ".join(details)
                raise LLVMParseError(
                    f"phi in %{block.name} does not match CFG predecessors"
                    + (f" ({detail})" if detail else ""),
                    instruction.location or block.location,
                )


def _parse_function(
    lines: list[str],
    start: int,
    *,
    source_name: str,
    debug_locations: Mapping[int, SourceLocation] | None = None,
    defined_functions: frozenset[str] = frozenset(),
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
    name, return_type, parameters, attributes, function_location, pointers = (
        _parse_function_header(
            header,
            source_name=source_name,
            line_number=line_number,
            debug_locations=debug_locations,
        )
    )
    if pointers:
        _reject_pointer_parameters(
            lines,
            cursor + 1,
            pointers,
            source_name=source_name,
            function_location=function_location,
            debug_locations=debug_locations,
        )
    # A function location that came from debug metadata is a position in the
    # C source.  It stands in for any instruction without a location of its
    # own, so a diagnostic never reports a line of the generated LLVM text as
    # if it were a line of the user's file.
    source_fallback = (
        function_location
        if function_location != _location(source_name, line_number, header)
        else None
    )
    for body_line_index in range(cursor + 1, len(lines)):
        body_line = _strip_comment(lines[body_line_index])
        if body_line.strip() == "}":
            break
        if "!llvm.loop" in body_line:
            loop_location = _location(source_name, body_line_index + 1, body_line)
            _, loop_metadata = _extract_metadata(body_line, loop_location)
            loop_location = _location_from_dbg(
                loop_metadata, source_fallback or loop_location, debug_locations
            )
            raise UnsupportedConstructError(
                "cyclic control flow",
                "a surviving LLVM loop is data-dependent or was not fully "
                "unrolled; use a statically bounded loop and enable full unrolling",
                loop_location,
            )
    values: dict[str, SSAValue] = {
        parameter.name: parameter for parameter in parameters
    }
    forward_names: set[str] = set()
    block_name = "entry"
    block_location = function_location
    instructions: list[Instruction] = []
    blocks: list[BasicBlock] = []
    seen_labels: set[str] = set()
    has_explicit_label = False

    def finish_current(location: SourceLocation) -> None:
        if not instructions:
            if has_explicit_label:
                raise LLVMParseError(f"basic block %{block_name} is empty", location)
            return
        terminator = instructions[-1]
        if not terminator.is_terminator:
            raise LLVMParseError(
                f"basic block %{block_name} does not end in a terminator",
                terminator.location or location,
            )
        blocks.append(BasicBlock(block_name, tuple(instructions), block_location))

    cursor += 1
    while cursor < len(lines):
        current = _strip_comment(lines[cursor])
        stripped = current.strip()
        current_line = cursor + 1
        if not stripped:
            cursor += 1
            continue
        if stripped == "}":
            finish_current(_location(source_name, current_line, current))
            if not blocks:
                raise LLVMParseError(
                    f"function @{name} has no basic blocks",
                    _location(source_name, current_line, current),
                )
            if forward_names:
                missing = ", ".join(f"%{name}" for name in sorted(forward_names))
                raise LLVMParseError(
                    f"use of undefined forward SSA value(s) {missing}",
                    _location(source_name, current_line, current),
                )
            function = Function(
                name,
                return_type,
                parameters,
                tuple(_finish_blocks(blocks, set(values))),
                attributes,
                function_location,
            )
            _validate_function(function)
            return function, cursor + 1

        label_match = _BLOCK_LABEL_RE.fullmatch(stripped)
        if label_match is not None:
            label = _normalize_name(label_match.group("name"), "")
            label_location = _location(source_name, current_line, current)
            if label in seen_labels or (
                not has_explicit_label and instructions and label == block_name
            ):
                raise LLVMParseError(
                    f"basic block %{label} is defined more than once",
                    label_location,
                )
            if instructions or has_explicit_label:
                finish_current(label_location)
            seen_labels.add(label)
            block_name = label
            block_location = label_location
            instructions = []
            has_explicit_label = True
            cursor += 1
            continue

        if instructions and instructions[-1].is_terminator:
            raise LLVMParseError(
                "instructions are not allowed after a terminator without a label",
                _location(source_name, current_line, current),
            )
        switch = _joined_switch(lines, cursor)
        if switch is not None:
            switch_text, consumed = switch
            instructions.append(
                _parse_switch(
                    switch_text,
                    values,
                    _location(source_name, current_line, current),
                    debug_locations=debug_locations,
                    fallback_location=source_fallback,
                )
            )
            cursor += consumed
            continue
        instruction = _parse_instruction(
            current,
            source_name=source_name,
            line_number=current_line,
            values=values,
            function_return_type=return_type,
            forward_names=forward_names,
            debug_locations=debug_locations,
            defined_functions=defined_functions,
            fallback_location=source_fallback,
        )
        if instruction is None:
            cursor += 1
            continue
        if instruction.opcode == "phi" and any(
            existing.opcode != "phi" for existing in instructions
        ):
            raise LLVMParseError(
                "phi instructions must appear before other instructions in a block",
                instruction.location,
            )
        instructions.append(instruction)
        cursor += 1

    raise LLVMParseError(
        f"function @{name} is missing its closing '}}'",
        function_location,
    )


_SWITCH_RE = re.compile(
    r"^switch\s+(?P<type>i[1-9][0-9]*)\s+(?P<value>[^,\s]+)\s*,\s*"
    r"(?P<default>label\s+[^\s\[]+)\s*\[(?P<cases>.*)\]$"
)
_SWITCH_CASE_RE = re.compile(
    r"(?P<type>i[1-9][0-9]*)\s+(?P<value>[^,\s]+)\s*,\s*(?P<target>label\s+\S+)"
)


def _joined_switch(lines: list[str], start: int) -> tuple[str, int] | None:
    """Return a ``switch`` as one line plus the number of lines it spans.

    LLVM prints one case per line and puts the ``!dbg`` attachment after the
    closing bracket, so the first line alone is neither a whole instruction
    nor a source position.  Any other text yields ``None``.
    """

    first = _strip_comment(lines[start]).strip()
    if not first.startswith("switch "):
        return None
    if "]" in first:
        return first, 1
    parts = [first]
    for index in range(start + 1, len(lines)):
        part = _strip_comment(lines[index]).strip()
        parts.append(part)
        if part.startswith("]"):
            return " ".join(parts), len(parts)
        if part == "}":
            break
    return None


def _parse_switch(
    text: str,
    values: dict[str, SSAValue],
    text_location: SourceLocation,
    *,
    debug_locations: Mapping[int, SourceLocation] | None,
    fallback_location: SourceLocation | None,
) -> Instruction:
    """Parse a ``switch`` into its selector, case constants, and targets.

    The operands are the selector followed by one constant per case; the
    targets are the default label followed by one label per case.  The
    instruction only lives until :func:`_lower_switches` rewrites it.
    """

    base, metadata = _extract_metadata(text, text_location)
    location = _location_from_dbg(
        metadata, fallback_location or text_location, debug_locations
    )
    match = _SWITCH_RE.match(base)
    if match is None:
        raise LLVMParseError("malformed switch instruction", location)
    value_type = _parse_type(match.group("type"), location)
    selector = _operand(match.group("value"), value_type, values, location)
    operands: list[SSAValue | IntConstant] = [selector]
    targets = [_label_target(match.group("default"), location)]
    cases = match.group("cases")
    position = 0
    for case in _SWITCH_CASE_RE.finditer(cases):
        if cases[position : case.start()].strip():
            raise LLVMParseError("malformed switch case list", location)
        position = case.end()
        if _parse_type(case.group("type"), location) != value_type:
            raise LLVMParseError(
                "switch case type does not match the selector type", location
            )
        constant = _operand(case.group("value"), value_type, values, location)
        if not isinstance(constant, IntConstant):
            raise LLVMParseError("switch case value must be a constant", location)
        operands.append(constant)
        targets.append(_label_target(case.group("target"), location))
    if cases[position:].strip():
        raise LLVMParseError("malformed switch case list", location)
    return Instruction(
        "switch",
        type=value_type,
        operands=tuple(operands),
        source_location=location,
        metadata=metadata,
        targets=tuple(targets),
    )


def _finish_blocks(blocks: list[BasicBlock], value_names: set[str]) -> list[BasicBlock]:
    """Reduce a parsed function body to ``br``/``ret`` control flow.

    Switches become branch chains, and an ``unreachable`` block that no
    branch names any more is dropped.  One that is still a branch target
    marks undefined behavior the compiler cannot rule out, and is rejected.
    """

    if not any(
        block.instructions[-1].opcode in {"switch", "unreachable"} for block in blocks
    ):
        return blocks
    lowered = _lower_switches(blocks, value_names)
    targeted = {
        target for block in lowered for target in block.instructions[-1].targets
    }
    finished: list[BasicBlock] = []
    for index, block in enumerate(lowered):
        terminator = block.instructions[-1]
        if terminator.opcode != "unreachable":
            finished.append(block)
        elif index == 0 or block.name in targeted:
            raise UnsupportedConstructError(
                "LLVM instruction 'unreachable'",
                "this function can reach code that has undefined behavior "
                "(an LLVM 'unreachable' instruction); only functions that are "
                "defined for every input are supported",
                terminator.source_location,
            )
    return finished


def _fresh_name(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 1
    while candidate in used:
        candidate = f"{base}.{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _lower_switches(
    blocks: list[BasicBlock], value_names: set[str]
) -> list[BasicBlock]:
    """Rewrite every ``switch`` as a chain of equality tests and branches.

    Clang emits ``switch`` not only for a C ``switch`` statement but for any
    run of ``==`` tests on one value, such as an ``if``/``else if`` chain.
    Each distinct case target gets one test (the OR of its case values) and
    one two-way branch; the last branch falls through to the default label.
    Later stages therefore see ordinary ``icmp``/``br`` control flow.

    A phi in a target block named the switch block as its predecessor.  Its
    edge now arrives from whichever chain block performs that target's test,
    so the phi edges are renamed to match.
    """

    bit = IntType(1)
    # Clang points the default label at an ``unreachable`` block when the
    # cases cover every possible value.  That edge is never taken, so the
    # last case target takes its place as the fall-through.
    dead = {
        block.name for block in blocks if block.instructions[0].opcode == "unreachable"
    }
    block_names = {block.name for block in blocks}
    lowered: list[BasicBlock] = []
    # (target block, original predecessor) -> the blocks that now branch there
    arrivals: dict[tuple[str, str], list[str]] = {}
    for block in blocks:
        switch = block.instructions[-1]
        if switch.opcode != "switch":
            lowered.append(block)
            continue
        location = switch.source_location
        selector = switch.operands[0]
        default = switch.targets[0]
        groups: dict[str, list[Operand]] = {}
        for constant, target in zip(
            switch.operands[1:], switch.targets[1:], strict=True
        ):
            if target != default:
                groups.setdefault(target, []).append(constant)
        fallthrough = default
        if default in dead and groups:
            fallthrough = list(groups)[-1]
            del groups[fallthrough]
        body = list(block.instructions[:-1])
        current = block.name
        current_location = block.source_location
        if not groups:
            body.append(
                Instruction("br", source_location=location, targets=(fallthrough,))
            )
            lowered.append(BasicBlock(current, tuple(body), current_location))
            arrivals.setdefault((fallthrough, block.name), []).append(current)
            continue
        remaining = len(groups)
        for target, constants in groups.items():
            remaining -= 1
            condition: SSAValue | None = None
            for constant in constants:
                test = SSAValue(_fresh_name(f"{block.name}.case", value_names), bit)
                body.append(
                    Instruction(
                        "icmp",
                        result=test,
                        type=bit,
                        operands=(selector, constant),
                        predicate="eq",
                        operand_type=selector.type,
                        source_location=location,
                    )
                )
                if condition is None:
                    condition = test
                    continue
                either = SSAValue(_fresh_name(f"{block.name}.case", value_names), bit)
                body.append(
                    Instruction(
                        "or",
                        result=either,
                        type=bit,
                        operands=(condition, test),
                        source_location=location,
                    )
                )
                condition = either
            assert condition is not None
            following = (
                fallthrough
                if remaining == 0
                else _fresh_name(f"{block.name}.switch", block_names)
            )
            body.append(
                Instruction(
                    "br",
                    type=bit,
                    operands=(condition,),
                    source_location=location,
                    targets=(target, following),
                )
            )
            lowered.append(BasicBlock(current, tuple(body), current_location))
            arrivals.setdefault((target, block.name), []).append(current)
            if remaining == 0:
                arrivals.setdefault((fallthrough, block.name), []).append(current)
            current, body, current_location = following, [], location

    renamed: list[BasicBlock] = []
    for block in lowered:
        instructions: list[Instruction] = []
        for instruction in block.instructions:
            if instruction.opcode != "phi":
                instructions.append(instruction)
                continue
            edges: list[PhiIncoming] = []
            moved: set[str] = set()
            for edge in instruction.incoming:
                sources = arrivals.get((block.name, edge.block))
                if sources is None:
                    edges.append(edge)
                elif edge.block not in moved:
                    # LLVM lists a switch predecessor once per case; every
                    # such entry carries the same value.
                    moved.add(edge.block)
                    edges.extend(
                        PhiIncoming(edge.value, source, edge.source_location)
                        for source in sources
                    )
            instructions.append(
                replace(
                    instruction,
                    operands=tuple(edge.value for edge in edges),
                    incoming=tuple(edges),
                )
            )
        renamed.append(
            BasicBlock(block.name, tuple(instructions), block.source_location)
        )
    return renamed


def _global_location(
    name: str,
    source_name: str,
    text_location: SourceLocation,
    *,
    from_c: bool,
) -> SourceLocation:
    """Return where a rejected global variable is declared.

    Clang's line tables carry no position for a global, so the declaration is
    found by name in the C file: an identifier is declared before it is used,
    and its first mention is therefore the declaration.  When that file cannot
    be read the diagnostic names the file only; the line of the LLVM text is
    reported only for LLVM text the caller supplied directly.
    """

    if source_name not in {"<llvm>", "<stdin>"}:
        try:
            source_lines = Path(source_name).read_text().splitlines()
        except OSError:
            source_lines = []
        mention = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_$])")
        for source_line_number, source_line in enumerate(source_lines, 1):
            match = mention.search(source_line)
            if match is not None:
                return SourceLocation(
                    source_name, source_line_number, match.start() + 1
                )
    return SourceLocation(source_name) if from_c else text_location


_DEFINE_NAME_RE = re.compile(
    r"^define\b[^@]*@(?P<name>\"(?:[^\"\\]|\\.)*\"|[A-Za-z$._-][A-Za-z0-9$._-]*)"
)
_GLOBAL_REFERENCE_RE = re.compile(
    r"@(\"(?:[^\"\\]|\\.)*\"|[A-Za-z$._-][A-Za-z0-9$._-]*)"
)


def _defined_name(line: str) -> str | None:
    match = _DEFINE_NAME_RE.match(line)
    return None if match is None else _normalize_name(match.group("name"), "@")


def _function_end(lines: list[str], start: int) -> int:
    """Return the index just past the ``}`` that closes a function body."""

    for index in range(start, len(lines)):
        if _strip_comment(lines[index]).strip() == "}":
            return index + 1
    return len(lines)


def parse_module(
    text: str,
    source_name: str = "<llvm>",
    *,
    strict: bool = True,
    legalize: bool = True,
) -> Module:
    """Parse bounded integer LLVM IR into a :class:`Module`.

    ``strict`` (the default) raises on the first construct outside the
    supported subset anywhere in the module.  With ``strict=False`` a
    function that cannot be accepted is recorded in ``Module.rejected`` and
    parsing continues, so one unsupported helper does not make the rest of a
    file uncompilable; :meth:`Module.get_function` raises the stored
    diagnostic if that function is requested.

    ``legalize`` runs :func:`qcc.frontend.legalize.legalize_module`, which
    inlines direct calls and expands intrinsics and variable shifts, so the
    returned functions contain primitive operations only.
    """

    if not isinstance(text, str):
        raise TypeError("LLVM input must be text")
    # Run the lexical pass first so malformed quoting and locations are reported
    # consistently even for module-level constructs the line parser ignores.
    tokenize(text, source_name)
    debug_locations = _debug_locations(text, source_name)
    lines = text.splitlines()
    functions: list[Function] = []
    rejected: dict[str, Exception] = {}
    rejected_globals: dict[str, CompilerError] = {}
    module_metadata: list[str] = []
    target_triple: str | None = None
    defined_functions = frozenset(
        name
        for name in (_defined_name(_strip_comment(line).strip()) for line in lines)
        if name is not None
    )
    cursor = 0
    while cursor < len(lines):
        raw = _strip_comment(lines[cursor])
        stripped = raw.strip()
        line_number = cursor + 1
        if not stripped:
            cursor += 1
            continue
        if stripped.startswith("define "):
            end = _function_end(lines, cursor)
            try:
                for body_line in lines[cursor:end]:
                    for reference in _GLOBAL_REFERENCE_RE.findall(body_line):
                        failure = rejected_globals.get(_normalize_name(reference, ""))
                        if failure is not None:
                            raise failure
                function, _ = _parse_function(
                    lines,
                    cursor,
                    source_name=source_name,
                    debug_locations=debug_locations,
                    defined_functions=defined_functions,
                )
            except CompilerError as error:
                if strict:
                    raise
                name = _defined_name(stripped) or f"<function at line {line_number}>"
                rejected.setdefault(name, error)
                cursor = end
                continue
            cursor = end
            if any(existing.name == function.name for existing in functions):
                raise LLVMParseError(
                    f"function @{function.name} is defined more than once",
                    function.source_location
                    or SourceLocation(source_name, line_number),
                )
            functions.append(function)
            continue
        if stripped.startswith("declare "):
            # External declarations are harmless until a supported function
            # actually calls one.  The instruction-level diagnostic then points
            # at the call site rather than at Clang's module preamble.
            module_metadata.append(stripped)
            cursor += 1
            continue
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
            if " constant " in f" {stripped} ":
                module_metadata.append(stripped)
                cursor += 1
                continue
            name_match = re.match(r"^@?([A-Za-z_.$][A-Za-z0-9_.$-]*)\s*=", stripped)
            fallback = _location(source_name, line_number, raw)
            subject = "a global variable"
            if name_match is not None:
                subject = f"global variable @{name_match.group(1)}"
                fallback = _global_location(
                    name_match.group(1),
                    source_name,
                    fallback,
                    from_c=bool(debug_locations),
                )
            global_error = UnsupportedConstructError(
                "global definition",
                f"{subject}: global mutable state, global variables, and global "
                "initializers are not supported",
                fallback,
            )
            if strict:
                raise global_error
            if name_match is not None:
                rejected_globals[name_match.group(1)] = global_error
            module_metadata.append(stripped)
            cursor += 1
            continue
        raise LLVMParseError(
            f"unrecognized top-level LLVM construct {stripped.split()[0]!r}",
            _location(source_name, line_number, raw),
        )

    if not functions and not rejected:
        raise LLVMParseError(
            "LLVM module does not contain a function definition",
            SourceLocation(source_name),
        )
    module = Module(
        FunctionCollection(functions),
        source_name=source_name,
        target_triple=target_triple,
        metadata=tuple(module_metadata),
        rejected=rejected,
    )
    if legalize:
        from qcc.frontend.legalize import legalize_module

        module = legalize_module(module, strict=strict)
    return module


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
