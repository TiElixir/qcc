"""v0.5 frontend: calls, intrinsics, variable shifts, and per-function errors."""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any

import pytest

import qcc
from qcc.errors import CompilerError, UnsupportedConstructError
from qcc.frontend.clang import compile_c as emit_llvm
from qcc.frontend.llvm_parser import parse_module
from qcc.ir.ssa import IntConstant
from qcc.verify import assert_verified, evaluate
from v02_helpers import build_native_library, native_function

FIXTURES = Path(__file__).parent / "fixtures"
FRONTEND = FIXTURES / "v05_frontend.c"
MIXED = FIXTURES / "v05_mixed.c"

# name -> (argument widths, result width, signed C types)
SIGNATURES: dict[str, tuple[tuple[int, ...], int, bool]] = {
    "helper_call": ((8, 8), 8, False),
    "branching_call": ((8, 8), 8, False),
    "variable_shl": ((8, 8), 8, False),
    "variable_shr": ((8, 8), 8, False),
    "rotate_left": ((8, 8), 8, False),
    "popcount_builtin": ((8,), 8, False),
    "leading_zeros": ((8,), 8, False),
    "trailing_zeros": ((8,), 8, False),
    "byte_swap": ((16,), 16, False),
    "absolute": ((8,), 8, True),
    "signed_max": ((8, 8), 8, True),
    "saturating_sub": ((8, 8), 8, False),
}
_UNSIGNED = {8: ctypes.c_uint8, 16: ctypes.c_uint16}
_SIGNED = {8: ctypes.c_int8, 16: ctypes.c_int16}


@pytest.fixture(scope="module")
def native(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    return build_native_library(FRONTEND, tmp_path_factory)


def _native_call(library: ctypes.CDLL, name: str) -> Any:
    widths, result_width, signed = SIGNATURES[name]
    types = _SIGNED if signed else _UNSIGNED
    entry = native_function(
        library, name, [types[width] for width in widths], types[result_width]
    )

    def call(*values: int) -> int:
        converted = [
            value - (1 << width) if signed and value >> (width - 1) else value
            for value, width in zip(values, widths, strict=True)
        ]
        return int(entry(*converted)) & ((1 << result_width) - 1)

    return call


def _samples(widths: tuple[int, ...]) -> list[tuple[int, ...]]:
    if len(widths) == 1:
        step = 1 if widths[0] <= 8 else 37
        return [(value,) for value in range(0, 1 << widths[0], step)]
    return [(a, b) for a in range(0, 256, 3) for b in range(0, 256, 7)]


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_parsed_function_matches_native_c(native: ctypes.CDLL, name: str) -> None:
    """Inlining and intrinsic expansion preserve what Clang compiled."""

    function = qcc.parse_c(FRONTEND, name)
    reference = _native_call(native, name)

    assert all(instruction.opcode != "call" for instruction in function.instructions)
    for arguments in _samples(SIGNATURES[name][0]):
        assert evaluate(function, arguments) == reference(*arguments)


@pytest.mark.parametrize("opt", (0, 3))
@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_frontend_functions_compile_to_verified_circuits(name: str, opt: int) -> None:
    function = qcc.parse_c(FRONTEND, name)
    circuit = qcc.compile_c(function, function=name, opt=opt)

    result = assert_verified(circuit, function, exhaustive_limit=16)

    assert result.exhaustive


def test_variable_shift_becomes_a_barrel_shifter() -> None:
    function = qcc.parse_c(FRONTEND, "variable_shl")
    shifts = [item for item in function.instructions if item.opcode == "shl"]

    # One mux stage per amount bit; every remaining shift is by a constant.
    assert [item.operands[1].value for item in shifts] == [1, 2, 4]  # type: ignore[union-attr]
    assert all(isinstance(item.operands[1], IntConstant) for item in shifts)
    assert sum(item.opcode == "select" for item in function.instructions) == 3


_BRANCHING_CALLEE = """
define i8 @pick(i8 %a, i8 %b) {
entry:
  %greater = icmp ugt i8 %a, %b
  br i1 %greater, label %then, label %else
then:
  %difference = sub i8 %a, %b
  ret i8 %difference
else:
  %mixed = xor i8 %b, %a
  ret i8 %mixed
}

define i8 @caller(i8 %x, i8 %y) {
entry:
  %first = call i8 @pick(i8 %x, i8 %y)
  %second = call i8 @pick(i8 %y, i8 3)
  %sum = add i8 %first, %second
  ret i8 %sum
}
"""


def _pick(a: int, b: int) -> int:
    return (a - b) & 0xFF if a > b else b ^ a


def test_multi_block_callee_is_spliced_into_the_caller() -> None:
    function = parse_module(_BRANCHING_CALLEE)["caller"]

    # Two call sites: each adds three cloned blocks and one continuation.
    assert len(function.blocks) == 9
    assert all(instruction.opcode != "call" for instruction in function.instructions)
    assert (
        sum(instruction.opcode == "phi" for instruction in function.instructions) == 2
    )
    for x in range(0, 256, 5):
        for y in range(0, 256, 11):
            expected = (_pick(x, y) + _pick(y, 3)) & 0xFF
            assert evaluate(function, {"x": x, "y": y}) == expected
    for opt in (0, 3):
        circuit = qcc.compile_c(function, function="caller", opt=opt)
        assert_verified(circuit, function, exhaustive_limit=16)


def test_one_unsupported_function_does_not_poison_the_file() -> None:
    circuit = qcc.compile_c(MIXED, function="pure_add", opt=3)

    assert qcc.simulate(circuit, {"a": 200, "b": 100}) == 44


@pytest.mark.parametrize(
    ("function", "construct", "terms"),
    [
        ("logs_value", "call to external function printf", ("printf", "external")),
        ("factorial", "recursion", ("@factorial -> @factorial", "inlined")),
        ("reads_memory", "unresolved pointer", ("%values", "memory access")),
        ("calls_logger", "call to external function printf", ("printf",)),
    ],
)
def test_rejections_name_the_real_construct(
    function: str, construct: str, terms: tuple[str, ...]
) -> None:
    with pytest.raises(UnsupportedConstructError) as captured:
        qcc.compile_c(MIXED, function=function, opt=3)

    error = captured.value
    assert error.construct == construct
    for term in terms:
        assert term in error.message
    assert error.location is not None
    assert Path(error.location.source_name).name == MIXED.name


def test_pointer_rejection_points_at_the_dereference() -> None:
    with pytest.raises(UnsupportedConstructError) as captured:
        qcc.compile_c(MIXED, function="reads_memory")

    location = captured.value.location
    assert location is not None
    source_line = MIXED.read_text().splitlines()[location.line - 1]
    assert "values[index]" in source_line


def test_strict_parsing_still_rejects_the_whole_module() -> None:
    text = emit_llvm(MIXED)

    with pytest.raises(UnsupportedConstructError):
        parse_module(text, source_name=str(MIXED))

    module = parse_module(text, source_name=str(MIXED), strict=False)
    assert module.functions.keys() == ("pure_add",)
    assert set(module.rejected) == {
        "logs_value",
        "factorial",
        "reads_memory",
        "calls_logger",
    }


def test_inline_asm_and_indirect_calls_have_specific_messages() -> None:
    asm = """
define i8 @uses_asm(i8 %value) {
entry:
  %0 = call i8 asm sideeffect "movb $1, $0", "=q,q"(i8 %value)
  ret i8 %0
}
"""
    indirect = """
define i8 @indirect(i8 %value, ptr %callback) {
entry:
  %0 = call i8 %callback(i8 %value)
  ret i8 %0
}
"""
    with pytest.raises(UnsupportedConstructError, match="inline asm"):
        parse_module(asm)
    with pytest.raises(UnsupportedConstructError, match="function pointers"):
        parse_module(indirect)


def test_unknown_function_lists_the_available_ones() -> None:
    with pytest.raises(CompilerError, match="pure_add") as captured:
        qcc.compile_c(MIXED, function="missing")

    assert captured.value.diagnostic.code == "unknown-function"


def test_freeze_and_disjoint_or_parse_as_plain_operations() -> None:
    text = """
define i8 @modern(i8 %a, i8 %b) {
entry:
  %frozen = freeze i8 %a
  %low = and i8 %frozen, 15
  %high = shl nuw i8 %b, 4
  %joined = or disjoint i8 %high, %low
  ret i8 %joined
}
"""
    function = parse_module(text)["modern"]

    assert evaluate(function, {"a": 0xAB, "b": 0x0C}) == 0xCB
    assert_verified(qcc.compile_c(function, function="modern", opt=3), function)
