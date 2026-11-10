"""v0.5 verification tooling: reference evaluators, bit-sliced simulation."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from scripts.fuzz import random_function

import qcc
from qcc import Circuit, Gate, cli
from qcc.sim.bitsliced import (
    exhaustive_columns,
    sample_columns,
    simulate_columns,
    truth_table_column,
)
from qcc.sim.classical import simulate
from qcc.verify import (
    VerificationError,
    assert_verified,
    evaluate,
    evaluate_columns,
    verify,
)

FIXTURES = Path(__file__).parent / "fixtures"
ADD = FIXTURES / "add.c"


def _with_extra_gate(circuit: Circuit, gate: Gate) -> Circuit:
    return Circuit(
        (*circuit.gates, gate), num_qubits=circuit.num_qubits, layout=circuit.layout
    )


def test_truth_table_columns_enumerate_every_assignment_once() -> None:
    bits = 5
    columns = [truth_table_column(bit, bits) for bit in range(bits)]

    seen = {
        sum(((column >> index) & 1) << bit for bit, column in enumerate(columns))
        for index in range(1 << bits)
    }
    assert seen == set(range(1 << bits))


@pytest.mark.parametrize("chunk_bits", (2, 3, 6))
def test_chunked_sweep_covers_the_whole_input_space(chunk_bits: int) -> None:
    bits = 6
    values: list[int] = []
    for chunk in exhaustive_columns(bits, chunk_bits=chunk_bits):
        assert len(chunk.columns) == bits
        for index in range(chunk.count):
            decoded = sum(
                ((column >> index) & 1) << bit
                for bit, column in enumerate(chunk.columns)
            )
            assert decoded == chunk.value(index)
            values.append(decoded)
    assert sorted(values) == list(range(1 << bits))


def test_sampled_chunk_reports_its_own_values() -> None:
    chunk = sample_columns([5, 0, 255, 18], 8)

    assert chunk.count == 4
    assert [chunk.value(index) for index in range(4)] == [5, 0, 255, 18]
    assert chunk.columns[0] == 0b0101  # bit 0 is set in 5 and 255


def test_bit_sliced_simulation_matches_the_scalar_simulator() -> None:
    circuit = qcc.compile_c(FIXTURES / "v02_nested_if.c", function="nested_if_else")
    generator = random.Random(3)
    states = [generator.getrandbits(16) for _ in range(64)]
    wires = 16
    columns = {
        wire: sum(((state >> wire) & 1) << index for index, state in enumerate(states))
        for wire in range(wires)
    }

    final = simulate_columns(circuit, columns, len(states))

    for index, state in enumerate(states):
        expected = simulate(circuit, initial=state, check_ancillas=False)
        actual = sum(
            ((column >> index) & 1) << wire for wire, column in enumerate(final)
        )
        assert actual == expected


def test_reference_evaluators_agree_on_random_functions() -> None:
    """The CFG interpreter and the bit-sliced evaluator are written apart."""

    for seed in range(120):
        function = random_function(seed, branch_chance=0.25)
        widths = [parameter.type.width for parameter in function.parameters]
        for chunk in exhaustive_columns(sum(widths)):
            arguments = {}
            offset = 0
            for parameter in function.parameters:
                width = parameter.type.width
                arguments[parameter.name] = chunk.columns[offset : offset + width]
                offset += width
            sliced = evaluate_columns(function, arguments, chunk.count)
            for index in range(0, chunk.count, 7):
                combined = chunk.value(index)
                scalar_arguments = []
                for width in widths:
                    scalar_arguments.append(combined & ((1 << width) - 1))
                    combined >>= width
                expected = evaluate(function, scalar_arguments)
                actual = sum(
                    ((column >> index) & 1) << bit for bit, column in enumerate(sliced)
                )
                assert actual == expected, f"seed {seed}"


def test_verify_accepts_every_optimization_level() -> None:
    function = qcc.parse_c(ADD, "add")

    for opt in (0, 1, 2, 3):
        circuit = qcc.compile_c(function, function="add", opt=opt)
        result = verify(circuit, function)
        assert result.ok
        assert result.exhaustive
        assert result.vectors == 65536
        assert "all 65536 inputs" in result.describe()


def test_verify_reports_a_wrong_output_with_a_counterexample() -> None:
    function = qcc.parse_c(ADD, "add")
    circuit = qcc.compile_c(function, function="add", opt=3)
    broken = _with_extra_gate(circuit, Gate.x(circuit.layout.output[3]))

    result = verify(broken, function)

    assert not result.ok
    failure = result.counterexample
    assert failure is not None
    assert failure.kind == "output"
    assert failure.expected == (failure.inputs["a"] + failure.inputs["b"]) & 0xFF
    assert failure.actual == failure.expected ^ 0b1000
    with pytest.raises(VerificationError, match="wrong output"):
        assert_verified(broken, function)


def test_verify_reports_dirty_work_wires_and_clobbered_inputs() -> None:
    function = qcc.parse_c(ADD, "add")
    circuit = qcc.compile_c(function, function="add", opt=3)
    ancilla = circuit.layout.ancillas[0]
    first_input = circuit.layout.inputs["a"][0]

    dirty = verify(_with_extra_gate(circuit, Gate.x(ancilla)), function)
    clobbered = verify(
        _with_extra_gate(circuit, Gate.cx(circuit.layout.inputs["b"][0], first_input)),
        function,
    )

    assert dirty.counterexample is not None
    assert dirty.counterexample.kind == "ancilla"
    assert f"q[{int(ancilla)}]" in dirty.describe()
    assert clobbered.counterexample is not None
    assert clobbered.counterexample.kind == "input"


def test_verify_samples_deterministically_when_the_sweep_is_too_large() -> None:
    function = qcc.parse_c(ADD, "add")
    circuit = qcc.compile_c(function, function="add", opt=3)

    first = verify(circuit, function, exhaustive_limit=8, samples=200, seed=4)
    second = verify(circuit, function, exhaustive_limit=8, samples=200, seed=4)

    assert first == second
    assert first.ok
    assert not first.exhaustive
    assert first.vectors == 200
    assert "200 sampled inputs of 2**16" in first.describe()


def test_cli_verify_reports_success_and_failure(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert cli.main(["verify", str(ADD), "--function", "add"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("ok: add: verified on all 65536 inputs")
    assert "O3" in output

    assert cli.main(["verify", str(ADD), "--function", "add", "--opt", "O0"]) == 0
    assert "O0" in capsys.readouterr().out

    # The default entry name selects the only function in the file...
    assert cli.main(["verify", str(ADD)]) == 0
    assert "add: verified" in capsys.readouterr().out
    # ...but an explicit name that does not exist is an error, not a guess.
    assert cli.main(["verify", str(ADD), "--function", "missing"]) == 1
    assert "pass one of: add" in capsys.readouterr().err

    source = tmp_path / "two.c"
    source.write_text(
        "unsigned char f(unsigned char x) { return x; }\n"
        "unsigned char g(unsigned char x) { return x + 1; }\n"
    )
    assert cli.main(["verify", str(source), "--function", "missing"]) == 1
    assert "pass one of: f, g" in capsys.readouterr().err


def test_cli_accepts_level_three_and_rejects_level_four(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["stats", str(ADD), "--function", "add", "--opt", "O3"]) == 0
    assert '"effective_level": 3' in capsys.readouterr().out

    assert cli.main(["stats", str(ADD), "--function", "add", "--opt", "O4"]) == 2
    assert "O0, O1, O2, or O3" in capsys.readouterr().err


def test_cli_dump_ir_prints_the_llvm_ir(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check", str(ADD), "--function", "add", "--dump-ir"]) == 0

    assert "define" in capsys.readouterr().err
