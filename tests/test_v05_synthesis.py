"""v0.5 level-3 synthesis: exhaustive correctness and measured resources."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from scripts.fuzz import check_seed, random_function

import qcc
from qcc.ir.ssa import (
    BasicBlock,
    Function,
    Instruction,
    IntConstant,
    IntType,
    Parameter,
    SSAValue,
)
from qcc.sim.bitsliced import exhaustive_columns, simulate_columns
from qcc.sim.classical import assign_inputs, read_register, simulate
from qcc.synth import build_dag, synthesize_function
from qcc.verify import assert_verified, evaluate_columns, verify
from v02_helpers import build_native_library, native_function

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parents[1] / "examples"


@dataclass(frozen=True)
class Case:
    source: Path
    function: str

    @property
    def identifier(self) -> str:
        return f"{self.source.name}:{self.function}"


CASES = (
    Case(FIXTURES / "add.c", "add"),
    Case(FIXTURES / "self_inverse.c", "self_inverse"),
    Case(FIXTURES / "v02_arithmetic.c", "min8"),
    Case(FIXTURES / "v02_arithmetic.c", "max8"),
    Case(FIXTURES / "v02_arithmetic.c", "abs_diff8"),
    Case(FIXTURES / "v02_arithmetic.c", "clamp8"),
    Case(FIXTURES / "v02_loop.c", "popcount8"),
    Case(FIXTURES / "v02_nested_if.c", "nested_if_else"),
    Case(FIXTURES / "v02_predicates.c", "all_icmp_predicates"),
    Case(FIXTURES / "v02_shifts.c", "constant_shifts"),
    Case(FIXTURES / "v03_arithmetic.c", "multiply8"),
    Case(FIXTURES / "v03_arithmetic.c", "udiv3_8"),
    Case(FIXTURES / "v03_arithmetic.c", "urem3_8"),
    Case(FIXTURES / "v03_arithmetic.c", "udiv5_8"),
    Case(FIXTURES / "v03_arithmetic.c", "urem5_8"),
    Case(FIXTURES / "v03_arithmetic.c", "udiv255_8"),
    Case(FIXTURES / "v03_arithmetic.c", "urem255_8"),
    Case(FIXTURES / "v03_grover.c", "is_solution"),
    Case(EXAMPLES / "grover_oracle.c", "grover_oracle"),
    Case(EXAMPLES / "benchmarks.c", "add"),
    Case(EXAMPLES / "benchmarks.c", "multiply"),
    Case(EXAMPLES / "benchmarks.c", "compare"),
    Case(EXAMPLES / "benchmarks.c", "popcount"),
    Case(EXAMPLES / "benchmarks.c", "is_solution"),
)
BENCHMARKS = tuple(case for case in CASES if case.source.name == "benchmarks.c")


@cache
def _function(case: Case) -> Function:
    return qcc.parse_c(case.source, case.function)


@cache
def _circuit(case: Case, opt: int) -> Any:
    return qcc.compile_c(_function(case), function=case.function, opt=opt)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
def test_level_three_circuit_is_correct_on_every_input(case: Case) -> None:
    """Output, preserved inputs, and clean work wires, for all inputs."""

    result = assert_verified(_circuit(case, 3), _function(case))

    # Up to 20 input bits are swept completely; clamp8 has 24 and is sampled.
    assert result.exhaustive == (result.input_bits <= 20)
    if result.exhaustive:
        assert result.vectors == 1 << result.input_bits


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
def test_level_three_keeps_the_public_layout(case: Case) -> None:
    baseline = _circuit(case, 0)
    optimized = _circuit(case, 3)

    assert dict(optimized.layout.input_widths) == dict(baseline.layout.input_widths)
    assert optimized.layout.output_width == baseline.layout.output_width
    assert all(gate.op in {"X", "CX", "CCX"} for gate in optimized.gates)
    assert optimized.optimization_level == 3


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.identifier)
def test_level_three_never_costs_more_than_level_two(case: Case) -> None:
    older = _circuit(case, 2).report()
    newer = _circuit(case, 3).report()

    assert newer.qubit_count <= older.qubit_count
    assert newer.t_count <= older.t_count


def test_benchmark_reductions_are_large_and_measured() -> None:
    """The headline numbers in docs/benchmarks.md, kept honest by a test."""

    for case in BENCHMARKS:
        baseline = _circuit(case, 0).report()
        optimized = _circuit(case, 3).report()
        assert optimized.t_count * 4 <= baseline.t_count, case.identifier
        assert optimized.qubit_count < baseline.qubit_count, case.identifier

    hardest = Case(EXAMPLES / "benchmarks.c", "is_solution")
    assert _circuit(hardest, 3).report().t_count * 20 <= (
        _circuit(hardest, 0).report().t_count
    )


def test_report_compares_against_the_level_zero_baseline() -> None:
    case = Case(FIXTURES / "add.c", "add")
    report = _circuit(case, 3).report()

    assert report.requested_level == 3
    assert report.effective_level == 3
    assert report.before is not None
    assert report.after is not None
    assert report.before["gate_count"] == 248
    assert report.after["t_count"] < report.before["t_count"]
    assert report.after["qubit_count"] < report.before["qubit_count"]


@pytest.mark.parametrize(
    "case",
    (
        Case(FIXTURES / "v02_shifts.c", "constant_shifts"),
        Case(FIXTURES / "self_inverse.c", "self_inverse"),
        Case(FIXTURES / "v05_frontend.c", "byte_swap"),
    ),
    ids=lambda case: case.identifier,
)
def test_pure_rewiring_needs_no_toffoli_and_no_work_qubits(case: Case) -> None:
    """Shifts, casts, masks, and byte swaps are relabelings, not gates."""

    circuit = _circuit(case, 3)

    assert circuit.report().t_count == 0
    assert not circuit.layout.ancillas
    assert all(gate.op in {"X", "CX"} for gate in circuit.gates)


def test_constant_function_is_only_x_gates() -> None:
    source = "#include <stdint.h>\nuint8_t k(uint8_t unused) { return 0xA5; }\n"
    circuit = qcc.compile_c(source, function="k", opt=3)

    assert sorted(int(gate.target) for gate in circuit.gates) == [
        int(circuit.layout.output[bit]) for bit in (0, 2, 5, 7)
    ]
    assert all(gate.op == "X" for gate in circuit.gates)


def test_shared_subexpressions_are_computed_once() -> None:
    source = """
#include <stdint.h>
uint8_t twice(uint8_t a, uint8_t b) {
    return (uint8_t)((a * b) ^ ((a * b) >> 3));
}
"""
    function = qcc.parse_c(source, "twice")
    dag = build_dag(function)

    assert sum(node.op == "mul" for node in dag.graph.nodes) == 1
    assert_verified(qcc.compile_c(function, function="twice", opt=3), function)


def test_branches_become_a_select_tree_without_path_logic() -> None:
    case = Case(FIXTURES / "v02_nested_if.c", "nested_if_else")
    function = _function(case)
    dag = build_dag(function)
    operations = [node.op for node in dag.graph.nodes]
    source = [instruction.opcode for instruction in function.instructions]

    # Five returns merge through four selects, plus the one in the source.
    assert operations.count("select") == 5
    # No AND/OR is added to combine branch conditions: the only ones present
    # are the two masks the C code wrote itself.
    assert operations.count("and") == source.count("and") == 2
    assert operations.count("or") == source.count("or") == 1
    assert operations.count("eq") + operations.count("ult") == source.count("icmp")


def test_structural_ssa_falls_back_to_level_two_and_says_so() -> None:
    function = {
        "name": "structural",
        "args": {"a": {"width": 4}, "b": {"width": 4}},
        "return_type": {"width": 4},
        "instructions": [
            {"opcode": "add", "result": "sum", "operands": ["%a", "%b"], "width": 4},
            {"opcode": "ret", "operands": ["%sum"], "width": 4},
        ],
    }
    circuit = qcc.compile_c(function, opt=3)

    assert circuit.optimization_level == 2
    report = circuit.report()
    assert (report.requested_level, report.effective_level) == (3, 2)
    state = simulate(circuit, {"a": 9, "b": 12})
    assert read_register(state, circuit.layout.output) == 5


@pytest.fixture(scope="module")
def native_benchmarks(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    return build_native_library(EXAMPLES / "benchmarks.c", tmp_path_factory)


@pytest.mark.parametrize(
    ("name", "arguments", "result"),
    [
        ("add", (ctypes.c_uint8, ctypes.c_uint8), ctypes.c_uint8),
        ("multiply", (ctypes.c_uint8, ctypes.c_uint8), ctypes.c_uint16),
        ("compare", (ctypes.c_uint8, ctypes.c_uint8), ctypes.c_uint8),
        ("popcount", (ctypes.c_uint8,), ctypes.c_uint8),
        ("is_solution", (ctypes.c_uint8,), ctypes.c_bool),
    ],
)
def test_level_three_matches_native_c(
    native_benchmarks: ctypes.CDLL,
    name: str,
    arguments: tuple[Any, ...],
    result: Any,
) -> None:
    """End to end against the machine code Clang produces for the same file."""

    native = native_function(native_benchmarks, name, arguments, result)
    circuit = _circuit(Case(EXAMPLES / "benchmarks.c", name), 3)
    names = tuple(circuit.layout.inputs)
    domains = [range(0, 256, 1 if len(names) == 1 else 5) for _ in names]
    points = (
        [(a,) for a in domains[0]]
        if len(names) == 1
        else [(a, b) for a in domains[0] for b in domains[1]]
    )
    for values in points:
        state = circuit.fast_simulate(
            assign_inputs(circuit, dict(zip(names, values, strict=True)))
        )
        assert read_register(state, circuit.layout.output) == int(native(*values))


def _direct(function: Function, level: int) -> Any:
    """Synthesize without the level-0 baseline that compile_c attaches."""

    del level
    return qcc._construct_circuit(synthesize_function(function))


@pytest.mark.parametrize("block", range(8))
def test_random_functions_verify_exhaustively(block: int) -> None:
    """A slice of scripts/fuzz.py: 400 seeds, half of them with branches."""

    for seed in range(block * 50, block * 50 + 50):
        failure = check_seed(seed, compile_function=_direct)
        assert failure is None, f"seed {seed}: {failure and failure.detail}"


def test_random_function_generator_is_deterministic() -> None:
    assert str(random_function(7)) == str(random_function(7))
    assert any(len(random_function(seed).blocks) > 1 for seed in range(20))
    assert all(
        len(random_function(seed, branch_chance=0.0).blocks) == 1 for seed in range(20)
    )


def test_wide_arithmetic_is_sampled_and_still_verified() -> None:
    """Too many inputs to sweep, so verification samples deterministically."""

    u32 = IntType(32)
    a, b = Parameter("a", u32), Parameter("b", u32)
    product, mixed = SSAValue("product", u32), SSAValue("mixed", u32)
    function = Function(
        "wide",
        u32,
        (a, b),
        (
            BasicBlock(
                "entry",
                (
                    Instruction("mul", result=product, type=u32, operands=(a, b)),
                    Instruction(
                        "add",
                        result=mixed,
                        type=u32,
                        operands=(product, IntConstant(u32, 0x9E3779B9)),
                    ),
                    Instruction("ret", type=u32, operands=(mixed,)),
                ),
            ),
        ),
    )
    circuit = _direct(function, 3)
    result = verify(circuit, function, samples=300)

    assert result.ok
    assert not result.exhaustive
    assert result.vectors == 300
    # Three 32-bit registers are unavoidable; the rest is the product and
    # transient carries, far below one register per SSA value and constant.
    assert circuit.num_qubits <= 6 * 32


def _flips_output(circuit: Any, function: Function) -> bool:
    """Whether the circuit maps output ``y`` to ``y ^ f(x)`` for a set ``y``."""

    layout = circuit.layout
    wires = [int(wire) for name in layout.inputs for wire in layout.inputs[name]]
    outputs = [int(wire) for wire in layout.output]
    for chunk in exhaustive_columns(len(wires)):
        mask = (1 << chunk.count) - 1
        columns = dict(zip(wires, chunk.columns, strict=True))
        for wire in outputs:
            columns[wire] = mask
        final = simulate_columns(circuit, columns, chunk.count)
        arguments = {}
        offset = 0
        for name in layout.inputs:
            width = len(layout.inputs[name])
            arguments[name] = chunk.columns[offset : offset + width]
            offset += width
        expected = evaluate_columns(function, arguments, chunk.count)
        if any(
            final[wire] != mask ^ column
            for wire, column in zip(outputs, expected, strict=True)
        ):
            return False
        if any(final[wire] != columns[wire] for wire in wires):
            return False
        if any(final[int(wire)] for wire in layout.ancillas):
            return False
    return True


@pytest.mark.parametrize(
    "case",
    (
        Case(EXAMPLES / "benchmarks.c", "is_solution"),
        Case(EXAMPLES / "grover_oracle.c", "grover_oracle"),
        Case(FIXTURES / "v03_grover.c", "is_solution"),
    ),
    ids=lambda case: case.identifier,
)
@pytest.mark.parametrize("opt", (0, 2, 3))
def test_predicate_circuit_is_a_bit_flip_oracle(case: Case, opt: int) -> None:
    """A one-bit result is XORed into the output qubit at every level.

    Grover's phase kickback prepares the output qubit in a superposition, so
    the circuit has to be correct when that qubit starts at one as well.
    """

    circuit = _circuit(case, opt)

    assert _flips_output(circuit, _function(case))
    if opt == 3:
        output = int(circuit.layout.output[0])
        assert all(output not in map(int, gate.controls) for gate in circuit.gates)


def test_random_predicates_are_bit_flip_oracles_at_level_three() -> None:
    checked = 0
    for seed in range(400):
        function = random_function(seed, branch_chance=0.25)
        if function.return_type.width != 1:
            continue
        checked += 1
        assert _flips_output(_direct(function, 3), function), f"seed {seed}"
    assert checked > 30


def test_wide_results_are_xored_only_below_level_three() -> None:
    """Level 3 builds a wide result in place, so its register must start clear."""

    case = Case(EXAMPLES / "benchmarks.c", "add")

    assert _flips_output(_circuit(case, 0), _function(case))
    assert _flips_output(_circuit(case, 2), _function(case))
    assert not _flips_output(_circuit(case, 3), _function(case))
