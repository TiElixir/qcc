"""v0.4 optimizer invariants over the complete supported fixture inventory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from itertools import product
from pathlib import Path
from typing import Any

import pytest

import qcc
from qcc import cli
from qcc.backend.report import resource_report
from qcc.errors import CompilerError
from qcc.ir.netlist import CircuitBuilder
from qcc.sim.classical import assign_inputs, read_registers, simulate

FIXTURE_DIR = Path(__file__).with_name("fixtures")
EXAMPLE_DIR = Path(__file__).parents[1] / "examples"


@dataclass(frozen=True)
class FixtureCase:
    """A supported source fixture and its entry point."""

    source: Path
    function: str

    @property
    def identifier(self) -> str:
        return f"{self.source.name}:{self.function}"


@dataclass(frozen=True)
class UnsupportedCase:
    """A source fixture that must be rejected at either optimizer level."""

    source: Path
    function: str
    diagnostic_terms: tuple[str, ...]

    @property
    def identifier(self) -> str:
        return f"{self.source.name}:{self.function}"


# Every supported entry point in tests/fixtures is intentionally listed here.
# The small example oracle is included as a public source input as well.
SUPPORTED_CASES = (
    FixtureCase(FIXTURE_DIR / "add.c", "add"),
    FixtureCase(FIXTURE_DIR / "self_inverse.c", "self_inverse"),
    FixtureCase(FIXTURE_DIR / "v02_arithmetic.c", "min8"),
    FixtureCase(FIXTURE_DIR / "v02_arithmetic.c", "max8"),
    FixtureCase(FIXTURE_DIR / "v02_arithmetic.c", "abs_diff8"),
    FixtureCase(FIXTURE_DIR / "v02_arithmetic.c", "clamp8"),
    FixtureCase(FIXTURE_DIR / "v02_loop.c", "popcount8"),
    FixtureCase(FIXTURE_DIR / "v02_nested_if.c", "nested_if_else"),
    FixtureCase(FIXTURE_DIR / "v02_predicates.c", "all_icmp_predicates"),
    FixtureCase(FIXTURE_DIR / "v02_shifts.c", "constant_shifts"),
    FixtureCase(FIXTURE_DIR / "v03_arithmetic.c", "multiply8"),
    FixtureCase(FIXTURE_DIR / "v03_arithmetic.c", "udiv3_8"),
    FixtureCase(FIXTURE_DIR / "v03_arithmetic.c", "urem3_8"),
    FixtureCase(FIXTURE_DIR / "v03_arithmetic.c", "udiv5_8"),
    FixtureCase(FIXTURE_DIR / "v03_arithmetic.c", "urem5_8"),
    FixtureCase(FIXTURE_DIR / "v03_arithmetic.c", "udiv255_8"),
    FixtureCase(FIXTURE_DIR / "v03_arithmetic.c", "urem255_8"),
    FixtureCase(FIXTURE_DIR / "v03_grover.c", "is_solution"),
    FixtureCase(EXAMPLE_DIR / "grover_oracle.c", "grover_oracle"),
)

# Rejected fixtures are checked separately: equivalence for an unsupported
# program means preserving the rejection, not manufacturing a circuit.
UNSUPPORTED_CASES = (
    UnsupportedCase(FIXTURE_DIR / "unsupported_float.c", "unsupported", ("float",)),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_pointer.c",
        "unsupported_pointer",
        ("pointer", "memory"),
    ),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_atomic.c", "uses_atomic", ("atomic",)
    ),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_data_loop.c",
        "data_dependent_loop",
        ("data-dependent", "loop"),
    ),
    UnsupportedCase(FIXTURE_DIR / "unsupported_v02_float.c", "uses_float", ("float",)),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_function_pointer.c",
        "uses_function_pointer",
        ("function pointer", "call"),
    ),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_global.c", "reads_global", ("global",)
    ),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_inline_asm.c",
        "uses_inline_asm",
        ("inline asm", "function call", "call"),
    ),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_malloc.c", "uses_malloc", ("malloc",)
    ),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_pointer.c",
        "unresolved_pointer",
        ("pointer", "memory"),
    ),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_printf.c", "uses_printf", ("printf", "call")
    ),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_recursion.c",
        "recursive_value",
        ("recursion", "function call", "call"),
    ),
    UnsupportedCase(
        FIXTURE_DIR / "unsupported_v02_volatile.c", "uses_volatile", ("volatile",)
    ),
    UnsupportedCase(FIXTURE_DIR / "v03_zero_divisor.c", "udiv_zero8", ("zero", "div")),
    UnsupportedCase(
        FIXTURE_DIR / "v03_zero_divisor.c", "urem_zero8", ("zero", "remainder")
    ),
)

OPTIMIZER_LEVELS = (0, 2)
MAX_INPUT_SAMPLES = 32


def _case_id(case: FixtureCase | UnsupportedCase) -> str:
    return case.identifier


@cache
def _compiled(case: FixtureCase, opt_level: int) -> Any:
    return qcc.compile_c(case.source, function=case.function, opt=opt_level)


def _registers(circuit: Any) -> dict[str, tuple[int, ...]]:
    layout = getattr(circuit, "layout", None)
    assert layout is not None, "compiled fixture has no circuit layout"
    registers = getattr(layout, "registers", None)
    assert isinstance(registers, Mapping), "compiled fixture has no named registers"
    return {
        str(name): tuple(int(qubit) for qubit in bits)
        for name, bits in registers.items()
    }


def _input_registers(circuit: Any) -> dict[str, tuple[int, ...]]:
    layout = getattr(circuit, "layout", None)
    assert layout is not None
    inputs = getattr(layout, "inputs", None)
    if isinstance(inputs, Mapping):
        return {
            str(name): tuple(int(qubit) for qubit in bits)
            for name, bits in inputs.items()
        }
    return {
        name: bits for name, bits in _registers(circuit).items() if name != "output"
    }


def _ancillas(circuit: Any) -> tuple[int, ...]:
    layout = getattr(circuit, "layout", None)
    assert layout is not None
    return tuple(int(qubit) for qubit in getattr(layout, "ancillas", ()))


def _layout_shape(circuit: Any) -> tuple[tuple[str, int], ...]:
    return tuple((name, len(bits)) for name, bits in _registers(circuit).items())


def _assert_well_formed_circuit(circuit: Any) -> None:
    """Reject malformed optimized netlists before comparing their behavior."""

    width = int(circuit.num_qubits)
    registers = _registers(circuit)
    seen: set[int] = set()
    for name, bits in registers.items():
        assert name, "register names must not be empty"
        assert bits, f"register {name!r} must not be empty"
        assert len(set(bits)) == len(bits), f"register {name!r} aliases a bit"
        assert all(0 <= qubit < width for qubit in bits)
        assert not seen.intersection(bits), (
            f"register {name!r} aliases another register"
        )
        seen.update(bits)

    ancillas = _ancillas(circuit)
    assert len(set(ancillas)) == len(ancillas), "ancilla layout contains duplicates"
    assert all(0 <= qubit < width for qubit in ancillas)
    assert not seen.intersection(ancillas), "ancilla aliases a named register"

    # Circuit construction validates these already; calling the public
    # validator here protects this regression suite if an optimizer mutates a
    # circuit after construction.
    assert circuit.validate() is circuit
    arities = {"X": 1, "CX": 2, "CCX": 3}
    for gate in circuit.gates:
        assert gate.op in {"X", "CX", "CCX", "MCX"}
        assert len(set(int(qubit) for qubit in gate.qubits)) == len(gate.qubits)
        assert all(0 <= int(qubit) < width for qubit in gate.qubits)
        if gate.op in arities:
            assert len(gate.qubits) == arities[gate.op]
        else:
            assert len(gate.qubits) >= 2


def _assert_compatible_logical_layouts(reference: Any, candidate: Any) -> None:
    _assert_well_formed_circuit(reference)
    _assert_well_formed_circuit(candidate)
    assert _layout_shape(candidate) == _layout_shape(reference)
    assert tuple(_input_registers(candidate)) == tuple(_input_registers(reference))


def _sample_values(width: int) -> tuple[int, ...]:
    mask = (1 << width) - 1
    candidates = (0, 1, mask, mask >> 1, 1 << (width - 1), 0x55 & mask, 0xAA & mask)
    return tuple(dict.fromkeys(value for value in candidates if 0 <= value <= mask))


def _sample_inputs(circuit: Any) -> tuple[dict[str, int], ...]:
    inputs = _input_registers(circuit)
    if not inputs:
        return ({},)
    choices = tuple(_sample_values(len(bits)) for bits in inputs.values())
    combinations = tuple(product(*choices))
    if len(combinations) <= MAX_INPUT_SAMPLES:
        selected = combinations
    else:
        positions = {
            round(index * (len(combinations) - 1) / (MAX_INPUT_SAMPLES - 1))
            for index in range(MAX_INPUT_SAMPLES)
        }
        selected = tuple(combinations[index] for index in sorted(positions))
    names = tuple(inputs)
    return tuple(
        {name: int(value) for name, value in zip(names, values, strict=True)}
        for values in selected
    )


def _assert_clean_and_mapped(circuit: Any, state: int) -> None:
    ancillas = _ancillas(circuit)
    dirty = tuple(qubit for qubit in ancillas if (state >> qubit) & 1)
    assert not dirty, f"declared ancillas were left dirty: {dirty}"

    declared = set(qubit for bits in _registers(circuit).values() for qubit in bits)
    declared.update(ancillas)
    unmapped = set(range(int(circuit.num_qubits))).difference(declared)
    leaked = tuple(qubit for qubit in unmapped if (state >> qubit) & 1)
    assert not leaked, f"state set on qubits absent from the layout: {leaked}"


def _simulate_clean(circuit: Any, inputs: Mapping[str, int]) -> int:
    state = int(simulate(circuit, inputs=inputs))
    _assert_clean_and_mapped(circuit, state)
    return state


def _resource_value(report: Any, name: str) -> int:
    value = getattr(report, name, None)
    if value is not None:
        return int(value)
    aliases = {
        "qubits": ("qubit_count", "num_qubits"),
        "toffoli_estimate": ("toffoli", "ccx_count", "toffoli_count"),
    }
    for alias in aliases[name]:
        value = getattr(report, alias, None)
        if value is not None:
            return int(value)
    raise AssertionError(f"resource report has no {name!r} field")


@pytest.mark.parametrize("case", SUPPORTED_CASES, ids=_case_id)
def test_optimizer_off_and_on_are_equivalent_on_every_supported_fixture(
    case: FixtureCase,
) -> None:
    """Optimizer changes may alter resources, never the logical permutation."""

    off = _compiled(case, 0)
    on = _compiled(case, 2)
    _assert_compatible_logical_layouts(off, on)

    for inputs in _sample_inputs(off):
        off_state = _simulate_clean(off, inputs)
        on_state = _simulate_clean(on, inputs)
        assert read_registers(off_state, off) == read_registers(on_state, on)


@pytest.mark.parametrize("case", SUPPORTED_CASES, ids=_case_id)
def test_every_optimizer_simulation_leaves_every_ancilla_clean(
    case: FixtureCase,
) -> None:
    """Every sampled clean-input run checks the complete declared workspace."""

    for opt_level in OPTIMIZER_LEVELS:
        circuit = _compiled(case, opt_level)
        for inputs in _sample_inputs(circuit):
            state = _simulate_clean(circuit, inputs)
            assert all(((state >> qubit) & 1) == 0 for qubit in _ancillas(circuit))


@pytest.mark.parametrize("case", SUPPORTED_CASES, ids=_case_id)
def test_full_compiled_circuit_followed_by_inverse_restores_every_sample(
    case: FixtureCase,
) -> None:
    """The optimizer must preserve the complete reversible circuit identity."""

    for opt_level in OPTIMIZER_LEVELS:
        circuit = _compiled(case, opt_level)
        round_trip = circuit.compose(circuit.inverse())
        for inputs in _sample_inputs(circuit):
            initial = assign_inputs(circuit, inputs)
            restored = int(simulate(round_trip, initial=initial))
            _assert_clean_and_mapped(round_trip, restored)
            assert restored == initial


@pytest.mark.parametrize("case", SUPPORTED_CASES, ids=_case_id)
def test_optimized_netlists_do_not_silently_corrupt_gates_or_layout(
    case: FixtureCase,
) -> None:
    """Logical layout shape is stable even when physical workspace is reduced."""

    off = _compiled(case, 0)
    on = _compiled(case, 2)
    _assert_compatible_logical_layouts(off, on)

    assert resource_report(off).gate_count == len(off.gates)
    assert resource_report(on).gate_count == len(on.gates)
    assert tuple(_registers(off)) == tuple(_registers(on))
    assert tuple(name for name, _ in _layout_shape(off)) == tuple(
        name for name, _ in _layout_shape(on)
    )


@pytest.mark.parametrize("case", UNSUPPORTED_CASES, ids=_case_id)
def test_optimizer_levels_preserve_rejection_of_unsupported_fixtures(
    case: UnsupportedCase,
) -> None:
    """An optimizer must not turn a rejected source into a guessed circuit."""

    errors: list[CompilerError] = []
    for opt_level in OPTIMIZER_LEVELS:
        with pytest.raises(CompilerError) as captured:
            qcc.compile_c(case.source, function=case.function, opt=opt_level)
        errors.append(captured.value)

    assert type(errors[0]) is type(errors[1])
    first_code = getattr(getattr(errors[0], "diagnostic", None), "code", None)
    second_code = getattr(getattr(errors[1], "diagnostic", None), "code", None)
    assert first_code == second_code
    for error in errors:
        message = f"{error} {getattr(error, 'message', '')}".lower()
        assert any(term in message for term in case.diagnostic_terms)


def _adjacent_self_inverse_pairs(circuit: Any) -> tuple[int, ...]:
    return tuple(
        index
        for index, (left, right) in enumerate(
            zip(circuit.gates, circuit.gates[1:], strict=False)
        )
        if left == right
    )


def test_optimizer_peephole_cancellation_is_deterministic() -> None:
    """Repeated self-inverse gates are removed reproducibly, not randomly."""

    case = FixtureCase(FIXTURE_DIR / "add.c", "add")
    off = _compiled(case, 0)
    optimized_first = qcc.compile_c(case.source, function=case.function, opt=2)
    optimized_second = qcc.compile_c(case.source, function=case.function, opt=2)

    # The v0.3 baseline contains deterministic G;G identity pairs in this
    # fixture.  Keeping this assertion makes the regression meaningful rather
    # than allowing a future change to accidentally remove the test stimulus.
    assert _adjacent_self_inverse_pairs(off)
    assert optimized_first.gates == optimized_second.gates
    assert _layout_shape(optimized_first) == _layout_shape(optimized_second)
    assert not _adjacent_self_inverse_pairs(optimized_first)
    assert len(optimized_first.gates) < len(off.gates)

    for inputs in _sample_inputs(off):
        assert read_registers(_simulate_clean(off, inputs), off) == read_registers(
            _simulate_clean(optimized_first, inputs), optimized_first
        )


@pytest.mark.parametrize("case", SUPPORTED_CASES, ids=_case_id)
def test_resource_reductions_are_asserted_only_when_measured(case: FixtureCase) -> None:
    """Equal resources are valid; any changed qubit/Toffoli value must improve."""

    off_report = resource_report(_compiled(case, 0))
    on_report = resource_report(_compiled(case, 2))
    for field in ("qubits", "toffoli_estimate"):
        baseline = _resource_value(off_report, field)
        optimized = _resource_value(on_report, field)
        assert optimized <= baseline, f"optimizer increased {field}"
        if optimized != baseline:
            # Do not hard-code a reduction that a fixture has not actually
            # measured.  When a result changes, however, it must be a strict
            # reduction in the documented resource dimension.
            assert optimized < baseline


def test_cli_keeps_optimizer_off_available_via_opt_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI must be able to request the unoptimized reference path."""

    source = tmp_path / "input.c"
    source.write_text("int add(int a, int b) { return a + b; }\n")
    calls: list[dict[str, Any]] = []

    def compile_stub(source_name: str, *, function: str, opt: int) -> Any:
        calls.append({"source": source_name, "function": function, "opt": opt})
        return CircuitBuilder(2).cx(0, 1).build()

    monkeypatch.setattr(cli, "compile_c", compile_stub)

    assert cli.main(["stats", str(source), "--function", "add", "--opt", "0"]) == 0
    capsys.readouterr()
    assert calls == [{"source": str(source), "function": "add", "opt": 0}]
