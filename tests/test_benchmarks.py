"""Deterministic schema and ordering coverage for the benchmark generator."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import benchmarks


def test_benchmark_schema_and_rows_are_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "benchmarks.c"
    source.write_text("/* fixture path only */\n", encoding="utf-8")
    specs = (
        benchmarks.BenchmarkSpec("first", "first", Path("benchmarks.c")),
        benchmarks.BenchmarkSpec("second", "second", Path("benchmarks.c")),
    )
    calls: list[tuple[Path, str, int]] = []

    def fake_compile(source_path: Path, *, function: str, opt: int) -> object:
        calls.append((source_path, function, opt))
        return SimpleNamespace(opt=opt)

    def fake_report(circuit: SimpleNamespace) -> SimpleNamespace:
        opt = circuit.opt
        return SimpleNamespace(
            qubits=10 + opt,
            ancillas=2,
            gates=30,
            x=1,
            cnot=4,
            toffoli=5,
            mcx=6,
            depth=7,
            t_count=35,
        )

    ticks = iter(float(value) for value in range(12))
    rows = benchmarks.collect_rows(
        tmp_path,
        specs=specs,
        compile_fn=fake_compile,
        report_fn=fake_report,
        clock=lambda: next(ticks),
    )

    assert benchmarks.ROW_SCHEMA == (
        "benchmark",
        "opt",
        "qubits",
        "ancillas",
        "gates",
        "x",
        "cx",
        "ccx",
        "mcx",
        "depth",
        "t_estimate",
        "compile_time_ms",
    )
    assert [(row.benchmark, row.opt) for row in rows] == [
        ("first", 0),
        ("first", 2),
        ("first", 3),
        ("second", 0),
        ("second", 2),
        ("second", 3),
    ]
    assert calls == [
        (source, "first", 0),
        (source, "first", 2),
        (source, "first", 3),
        (source, "second", 0),
        (source, "second", 2),
        (source, "second", 3),
    ]
    assert [row.compile_time_ms for row in rows] == [1000.0] * 6
    assert all(tuple(row.as_dict()) == benchmarks.ROW_SCHEMA for row in rows)

    first_render = benchmarks.render_markdown(rows)
    second_render = benchmarks.render_markdown(tuple(rows))
    assert first_render == second_render
    assert first_render.count("| first |") == 3
    assert first_render.count("| second |") == 3
    assert "## O0 to O3" in first_render
    assert "| `first` | 10 → 13 | 35 → 35 | 1.0x |" in first_render
    assert "| Compile time (ms) |" in first_render
