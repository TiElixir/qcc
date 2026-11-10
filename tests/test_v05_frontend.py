"""v0.5 frontend: calls, intrinsics, variable shifts, and per-function errors."""

from __future__ import annotations

import ctypes
import random
from collections.abc import Callable
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
SWITCH = FIXTURES / "v05_switch.c"

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


SWITCH_FUNCTIONS = {
    "if_chain": 1,
    "ternary_chain": 1,
    "two_cases": 2,
    "full_cover": 2,
    "shared_targets": 2,
    "nested": 2,
}


@pytest.fixture(scope="module")
def native_switch(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    return build_native_library(SWITCH, tmp_path_factory)


@pytest.mark.parametrize("name", sorted(SWITCH_FUNCTIONS))
def test_switch_is_lowered_to_branches_that_match_native_c(
    native_switch: ctypes.CDLL, name: str
) -> None:
    """An if/else-if chain reaches qcc as an LLVM switch; so does a switch."""

    text = emit_llvm(SWITCH)
    body = text[text.index(f"@{name}(") :]
    assert "switch i8" in body[: body.index("\n}\n")]

    arity = SWITCH_FUNCTIONS[name]
    function = qcc.parse_c(SWITCH, name)
    reference = native_function(
        native_switch, name, [ctypes.c_uint8] * arity, ctypes.c_uint8
    )

    assert all(
        instruction.opcode in {"br", "ret"}
        for block in function.blocks
        for instruction in block.instructions[-1:]
    )
    domain = (
        [(a,) for a in range(256)]
        if arity == 1
        else [(a, b) for a in range(256) for b in range(0, 256, 5)]
    )
    for arguments in domain:
        assert evaluate(function, arguments) == int(reference(*arguments)), arguments


@pytest.mark.parametrize("opt", (0, 3))
@pytest.mark.parametrize("name", sorted(SWITCH_FUNCTIONS))
def test_switch_functions_compile_to_verified_circuits(name: str, opt: int) -> None:
    function = qcc.parse_c(SWITCH, name)
    circuit = qcc.compile_c(function, function=name, opt=opt)

    assert assert_verified(circuit, function, exhaustive_limit=16).exhaustive


def test_switch_phi_edges_follow_the_new_predecessors() -> None:
    text = """
define i8 @pick(i8 %a, i8 %b) {
entry:
  switch i8 %a, label %join [
    i8 1, label %one
    i8 2, label %join
    i8 5, label %other
    i8 6, label %other
  ]
one:
  %bumped = add i8 %b, 1
  br label %join
other:
  br label %join
join:
  %result = phi i8 [ %bumped, %one ], [ 40, %other ], [ %b, %entry ], [ %b, %entry ]
  ret i8 %result
}
"""
    function = parse_module(text)["pick"]

    def expected(a: int, b: int) -> int:
        if a == 1:
            return (b + 1) & 0xFF
        return 40 if a in (5, 6) else b

    for a in range(16):
        for b in (0, 3, 255):
            assert evaluate(function, {"a": a, "b": b}) == expected(a, b)
    for opt in (0, 3):
        circuit = qcc.compile_c(function, function="pick", opt=opt)
        assert_verified(circuit, function, exhaustive_limit=16)


def test_reachable_undefined_behavior_is_rejected() -> None:
    source = (
        "#include <stdint.h>\n"
        "uint8_t trap(uint8_t a) { __builtin_unreachable(); }\n"
        "uint8_t fine(uint8_t a) { return a; }\n"
    )

    with pytest.raises(UnsupportedConstructError, match="undefined behavior"):
        qcc.compile_c(source=source, function="trap")
    assert qcc.simulate(qcc.compile_c(source=source, function="fine"), {"a": 9}) == 9


def test_global_diagnostic_points_at_the_declaration(tmp_path: Path) -> None:
    source = tmp_path / "globals.c"
    source.write_text(
        "#include <stdint.h>\n"
        "\n"
        "uint8_t counter;\n"
        "uint8_t table[4] = {3, 1, 4, 1};\n"
        "\n"
        "uint8_t writes(uint8_t a) { counter = a; return a; }\n"
        "uint8_t reads(uint8_t a) { return (uint8_t)(a + counter); }\n"
        "uint8_t indexes(uint8_t a) { return table[a & 3]; }\n"
        "uint8_t pure(uint8_t a) { return (uint8_t)(a + 1); }\n"
    )

    for function, name, line in (
        ("writes", "counter", 3),
        ("reads", "counter", 3),
        ("indexes", "table", 4),
    ):
        with pytest.raises(UnsupportedConstructError) as captured:
            qcc.compile_c(source, function=function)
        error = captured.value
        assert f"@{name}" in error.message
        assert error.location is not None
        assert (error.location.line, error.location.column) == (line, 9)
    assert qcc.simulate(qcc.compile_c(source, function="pure"), {"a": 1}) == 2


def test_global_diagnostic_without_a_readable_source_has_no_line() -> None:
    source = (
        "#include <stdint.h>\n"
        "uint8_t counter;\n"
        "uint8_t reads(uint8_t a) { return (uint8_t)(a + counter); }\n"
    )

    with pytest.raises(UnsupportedConstructError) as captured:
        qcc.compile_c(source=source, function="reads")

    # The only line number available is one in the generated LLVM text, which
    # would be wrong for the C source the caller passed in.
    assert captured.value.location is not None
    assert captured.value.location.line is None


# ---------------------------------------------------------------------------
# Randomized switch programs.  The generator writes LLVM text and, from the same
# decisions, a Python model of what the function returns, so the model never
# goes through the parser under test.

_Outcome = tuple[str, int]  # ("ret", v) leaves the function; ("val", v) reaches a merge
_Model = Callable[[int, int], _Outcome]  # (the %s argument, incoming i8) -> outcome
_ARM_OPS: dict[str, Callable[[int, int], int]] = {
    "add": lambda x, c: (x + c) & 255,
    "xor": lambda x, c: x ^ c,
    "sub": lambda x, c: (x - c) & 255,
    "mul": lambda x, c: (x * c) & 255,
}


def _never_taken(value: int) -> _Outcome:
    """Model of an ``unreachable`` default: no selector value selects it."""

    del value
    return ("ret", -1)


class _SwitchGen:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.count = 0
        self.blocks: list[str] = []
        self.tags: set[str] = set()

    def fresh(self) -> int:
        self.count += 1
        return self.count

    def region(
        self,
        entry: str,
        src: str,
        width: int,
        *,
        direct: bool,
        depth: int,
        exit_label: str | None,
        extra: list[tuple[str, str]] = (),  # type: ignore[assignment]
    ) -> tuple[_Model, str | None, str | None]:
        """Emit ``entry`` (ending in a switch), its arms, and a merge block.

        Returns the model, the merge block's label and its phi value; the last two
        are ``None`` when no path reaches the merge.
        """

        rng = self.rng
        u = self.fresh()
        merge = f"m{u}"
        k = rng.randrange(256)
        head = [f"{entry}:", f"  %x{u} = add i8 {src}, {k}"]
        if direct:
            sel = "%s"
        else:
            sel = f"%t{u}"
            head.append(f"  %t{u} = trunc i8 %x{u} to i{width}")
        slot = len(self.blocks)
        self.blocks.append("")  # the arms need the entry's values to come first
        size = 1 << width
        if rng.random() < 0.35:
            values = list(range(size))
            full = True
        else:
            values = rng.sample(range(size), rng.randrange(0, size + 1))
            full = False
        rng.shuffle(values)
        if not values:
            self.tags.add("empty")
        groups = rng.randrange(1, min(len(values), 4) + 1) if values else 0
        kinds = ["merge", "arm", "ret"] + (["nested"] if depth == 0 else [])
        group_kinds = [rng.choice(kinds) for _ in range(groups)]
        assignment = [
            index if index < groups else rng.randrange(groups)
            for index in range(len(values))
        ]
        if groups < len(values):
            self.tags.add("shared")

        edges: list[tuple[str, str]] = list(extra)
        labels: list[str] = []
        models: list[Callable[[int], _Outcome]] = []
        direct_count = [0]

        def make(kind: str) -> tuple[str, Callable[[int], _Outcome]]:
            n = self.fresh()
            c = rng.randrange(1, 256)
            op = rng.choice(sorted(_ARM_OPS))
            if kind == "merge":
                self.tags.add("direct")
                return merge, lambda x: ("val", x)
            if kind == "arm":
                self.blocks.append(
                    f"arm{n}:\n  %a{n} = {op} i8 %x{u}, {c}\n  br label %{merge}"
                )
                edges.append((f"%a{n}", f"arm{n}"))
                return f"arm{n}", lambda x: ("val", _ARM_OPS[op](x, c))
            if kind == "ret":
                self.tags.add("ret")
                self.blocks.append(
                    f"arm{n}:\n  %a{n} = {op} i8 %x{u}, {c}\n  ret i8 %a{n}"
                )
                return f"arm{n}", lambda x: ("ret", _ARM_OPS[op](x, c))
            self.tags.add("nested")
            inner, tail, result = self.region(
                f"in{n}",
                f"%x{u}",
                rng.randrange(1, 5),
                direct=False,
                depth=depth + 1,
                exit_label=merge,
            )
            if tail is not None and result is not None:
                edges.append((result, tail))
            return f"in{n}", lambda x: inner(0, x)

        for kind in group_kinds:
            label, model = make(kind)
            labels.append(label)
            models.append(model)
        unreachable = full and rng.random() < 0.8
        if unreachable:
            self.tags.add("unreachable")
            default_label = f"dead{u}"
            default_model: Callable[[int], _Outcome] = _never_taken
            self.blocks.append(f"dead{u}:\n  unreachable")
        elif groups and rng.random() < 0.4:
            self.tags.add("default_is_case_target")
            index = rng.randrange(groups)
            default_label, default_model = labels[index], models[index]
        else:
            default_label, default_model = make(rng.choice(kinds))

        by_value = {v: models[assignment[i]] for i, v in enumerate(values)}
        spelled = []
        for i, v in enumerate(values):
            target = labels[assignment[i]]
            if width == 1:
                text = ("true", "false")[1 - v] if rng.random() < 0.5 else str(v)
            elif rng.random() < 0.5 and v >= size // 2:
                text = str(v - size)
                self.tags.add("signed_case")
            else:
                text = str(v)
            spelled.append(f"i{width} {text}, label %{target}")
            if target == merge:
                direct_count[0] += 1
        if default_label == merge:
            direct_count[0] += 1
        edges.extend((f"%x{u}", entry) for _ in range(direct_count[0]))
        if direct_count[0] > 1:
            self.tags.add("repeated_phi_edge")

        if rng.random() < 0.3:
            self.tags.add("single_line")
            switch = (
                f"  switch i{width} {sel}, label %{default_label} [ "
                + " ".join(spelled)
                + " ]"
            )
        else:
            body = "".join(f"\n    {case}" for case in spelled)
            switch = f"  switch i{width} {sel}, label %{default_label} [{body}\n  ]"
        self.blocks[slot] = "\n".join(head) + "\n" + switch

        result_name: str | None = None
        tail: str | None = None
        if edges:
            result_name = f"%r{u}"
            tail = merge
            phi = ", ".join(f"[ {value}, %{label} ]" for value, label in edges)
            last = f"br label %{exit_label}" if exit_label else f"ret i8 %r{u}"
            self.blocks.append(f"{merge}:\n  %r{u} = phi i8 {phi}\n  {last}")

        def model(s: int, incoming: int) -> _Outcome:
            x = (incoming + k) & 255
            selector = s if direct else x & (size - 1)
            return by_value.get(selector, default_model)(x)

        return model, tail, result_name


def _random_switch_function(
    seed: int,
) -> tuple[str, int, Callable[[int, int], int], set[str]]:
    rng = random.Random(seed)
    gen = _SwitchGen(rng)
    width = rng.choice([1, 2, 3, 4])
    shape = rng.choice(["plain", "head", "two"])
    if shape == "head":
        gen.tags.add("not_entry")
        threshold = rng.randrange(1, 255)
        pre = rng.randrange(256)
        model, merge, _ = gen.region(
            "sw",
            "%b",
            width,
            direct=True,
            depth=0,
            exit_label=None,
            extra=[("%pv", "pre")],
        )
        assert merge is not None  # the pre block always reaches it
        gen.blocks.append(
            f"entry:\n  %c = icmp ult i8 %b, {threshold}\n"
            "  br i1 %c, label %sw, label %pre",
        )
        gen.blocks.append(f"pre:\n  %pv = xor i8 %b, {pre}\n  br label %{merge}")

        def outcome(s: int, b: int) -> int:
            return model(s, b)[1] if b < threshold else (b ^ pre)

    elif shape == "two":
        gen.tags.add("two_switches")
        first, tail, result = gen.region(
            "entry", "%b", width, direct=True, depth=0, exit_label="second"
        )
        second: _Model | None = None
        if tail is not None and result is not None:
            second, _, _ = gen.region(
                "second",
                result,
                rng.randrange(1, 5),
                direct=False,
                depth=0,
                exit_label=None,
            )

        def outcome(s: int, b: int) -> int:
            kind, value = first(s, b)
            if kind == "ret" or second is None:
                return value
            return second(0, value)[1]

    else:
        model, _, _ = gen.region(
            "entry", "%b", width, direct=True, depth=0, exit_label=None
        )

        def outcome(s: int, b: int) -> int:
            return model(s, b)[1]

    # LLVM takes the first block as the entry; regions append theirs as they go.
    gen.blocks.sort(key=lambda block: not block.startswith("entry:"))
    text = f"define i8 @f(i{width} %s, i8 %b) {{\n" + "\n".join(gen.blocks) + "\n}\n"
    return text, width, outcome, gen.tags


_SEEDS = range(150)


def _spread() -> list[int]:
    return [0, 1, 37, 99, 100, 128, 200, 255]


@pytest.mark.parametrize("seed", _SEEDS)
def test_random_switches_match_an_independent_model(seed: int) -> None:
    text, width, outcome, _ = _random_switch_function(seed)
    function = parse_module(text)["f"]

    for s in range(1 << width):
        for b in _spread():
            assert evaluate(function, {"s": s, "b": b}) == outcome(s, b), (
                text,
                s,
                b,
            )


@pytest.mark.parametrize("opt", (0, 3))
@pytest.mark.parametrize("seed", range(0, 150, 10))
def test_random_switches_compile_to_verified_circuits(seed: int, opt: int) -> None:
    text, _, _, _ = _random_switch_function(seed)
    function = parse_module(text)["f"]
    circuit = qcc.compile_c(function, function="f", opt=opt)

    assert assert_verified(circuit, function, exhaustive_limit=16).exhaustive


def test_random_switch_generator_covers_the_requested_shapes() -> None:
    tags: set[str] = set()
    widths: set[int] = set()
    for seed in _SEEDS:
        _, width, _, found = _random_switch_function(seed)
        tags |= found
        widths.add(width)

    assert widths == {1, 2, 3, 4}
    assert tags >= {
        "empty",
        "shared",
        "direct",
        "ret",
        "unreachable",
        "default_is_case_target",
        "repeated_phi_edge",
        "signed_case",
        "single_line",
        "nested",
        "not_entry",
        "two_switches",
    }
