"""v0.2 textual LLVM frontend coverage."""

from __future__ import annotations

import shutil

import pytest

from qcc.errors import UnsupportedConstructError
from qcc.frontend.clang import compile_c
from qcc.frontend.llvm_parser import parse_module

pytestmark = pytest.mark.skipif(
    shutil.which("clang") is None, reason="clang is required"
)


def test_integer_icmp_predicates_and_select_keep_typed_locations() -> None:
    predicates = (
        "eq",
        "ne",
        "ult",
        "ule",
        "ugt",
        "uge",
        "slt",
        "sle",
        "sgt",
        "sge",
    )
    comparisons = "\n".join(
        f"  %{predicate} = icmp {predicate} i8 %x, %y" for predicate in predicates
    )
    ir = (
        "define i8 @compare(i8 %x, i8 %y) {\n"
        "entry:\n"
        f"{comparisons}\n"
        "  %picked = select i1 %ult, i8 %x, i8 %y\n"
        "  ret i8 %picked\n"
        "}\n"
    )

    function = parse_module(ir, source_name="comparisons.ll")["compare"]

    assert [
        instruction.predicate for instruction in function.instructions[:10]
    ] == list(predicates)
    assert all(
        instruction.comparison_type.width == 8
        for instruction in function.instructions[:10]
    )
    selected = function.instructions[10]
    assert selected.opcode == "select"
    assert selected.type.width == 8  # type: ignore[union-attr]
    assert selected.condition is not None
    assert selected.true_value == function["x"]
    assert selected.false_value == function["y"]
    assert selected.location is not None
    assert selected.location.source_name == "comparisons.ll"
    assert selected.location.line == 13


def test_nested_diamonds_parse_phi_edges_and_expose_cfg_helpers() -> None:
    ir = """
 define i8 @nested(i8 %x, i8 %y, i8 %z) {
 entry:
   %outer = icmp ult i8 %x, %y
   br i1 %outer, label %left, label %right
 left:
   %inner = icmp ult i8 %y, %z
   br i1 %inner, label %inner.true, label %inner.false
 inner.true:
   %a = add i8 %x, 1
   br label %inner.merge
 inner.false:
   %b = sub i8 %y, 1
   br label %inner.merge
 inner.merge:
   %left.result = phi i8 [ %a, %inner.true ], [ %b, %inner.false ]
   br label %merge
 right:
   br label %merge
 merge:
   %result = phi i8 [ %left.result, %inner.merge ], [ %z, %right ]
   ret i8 %result
 }
 """

    function = parse_module(ir, source_name="nested.ll")["nested"]

    assert function.cfg_successors == {
        "entry": ("left", "right"),
        "left": ("inner.true", "inner.false"),
        "inner.true": ("inner.merge",),
        "inner.false": ("inner.merge",),
        "inner.merge": ("merge",),
        "right": ("merge",),
        "merge": (),
    }
    assert function.cfg_predecessors["merge"] == ("inner.merge", "right")
    assert [block.name for block in function.successors("entry")] == [
        "left",
        "right",
    ]
    assert [block.name for block in function.predecessors("merge")] == [
        "inner.merge",
        "right",
    ]
    assert function.block("%merge").successors == ()

    phi = next(
        instruction
        for instruction in function.instructions
        if instruction.result is not None and instruction.result.name == "result"
    )
    assert phi.type.width == 8  # type: ignore[union-attr]
    assert phi.incoming_blocks == ("inner.merge", "right")
    assert phi.location is not None
    assert phi.location.line == 21
    assert function.block("inner.merge").location is not None
    assert function.block("inner.merge").location.line == 15


def test_real_clang_if_else_output_parses_select_and_icmp() -> None:
    ir = compile_c(
        "#include <stdint.h>\n"
        "uint8_t choose(uint8_t x, uint8_t y) { "
        "if (x < y) return x; return y; }\n",
        source_name="choose.c",
    )

    function = parse_module(ir, source_name="choose.ll")["choose"]

    assert [instruction.opcode for instruction in function.instructions] == [
        "icmp",
        "select",
        "ret",
    ]
    assert function.instructions[0].predicate == "ult"
    assert function.instructions[1].location is not None
    assert function.instructions[1].location.source_name == "choose.ll"


def test_surviving_branch_has_labels_and_source_location() -> None:
    ir = """
 define i8 @branch(i1 %condition, i8 %left.value, i8 %right.value) {
 entry:
   br i1 %condition, label %left, label %right
 left:
   ret i8 %left.value
 right:
   ret i8 %right.value
 }
 """

    function = parse_module(ir, source_name="branch.ll")["branch"]
    branch = function.entry.terminator

    assert branch is not None
    assert branch.opcode == "br"
    assert branch.targets == ("left", "right")
    assert branch.location is not None
    assert branch.location.source_name == "branch.ll"
    assert branch.location.line == 4
    assert function.entry.location is not None
    assert function.entry.location.line == 3


def test_calls_and_memory_from_real_clang_have_clear_diagnostics() -> None:
    call_ir = compile_c(
        "#include <stdint.h>\n"
        "extern uint8_t helper(uint8_t);\n"
        "uint8_t caller(uint8_t x) { return helper(x); }\n",
        source_name="call.c",
    )
    with pytest.raises(UnsupportedConstructError) as call_error:
        parse_module(call_ir, source_name="call.ll")
    assert "function calls" in call_error.value.message
    assert call_error.value.location is not None
    assert call_error.value.location.source_name == "call.ll"

    memory_ir = compile_c(
        "#include <stdint.h>\nuint8_t memory(uint8_t x) { uint8_t y = x; return y; }\n",
        source_name="memory.c",
        extra_flags=("-O0",),
    )
    with pytest.raises(UnsupportedConstructError) as memory_error:
        parse_module(memory_ir, source_name="memory.ll")
    assert "memory operations" in memory_error.value.message
    assert memory_error.value.location is not None
    assert memory_error.value.location.source_name == "memory.ll"


def test_cyclic_cfg_is_rejected_at_the_back_edge() -> None:
    ir = """
 define i8 @loop(i8 %limit) {
 entry:
   br label %loop
 loop:
   %i = phi i8 [ 0, %entry ], [ %next, %loop ]
   %next = add i8 %i, 1
   %condition = icmp ult i8 %next, %limit
   br i1 %condition, label %loop, label %exit
 exit:
   ret i8 %next
 }
 """

    with pytest.raises(UnsupportedConstructError) as error:
        parse_module(ir, source_name="loop.ll")

    assert error.value.construct == "cyclic control flow"
    assert "data-dependent loop" in error.value.message
    assert error.value.location is not None
    assert error.value.location.source_name == "loop.ll"
    assert error.value.location.line == 9
