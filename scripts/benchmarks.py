#!/usr/bin/env python3
"""Generate deterministic resource rows for the v0.4 benchmark baseline.

The default command measures the five checked-in examples at optimizer levels
0 and 2, then writes ``docs/benchmarks.md``. Resource values come directly
from the compiled circuit's public resource report. Compile time is measured
around the real ``qcc.compile_c`` call and is intentionally not synthesized.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import qcc  # noqa: E402
from qcc.backend.report import ResourceReport, resource_report  # noqa: E402

OPT_LEVELS: tuple[int, ...] = (0, 2)
ROW_SCHEMA: tuple[str, ...] = (
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

_TABLE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("Benchmark", "benchmark", "left"),
    ("Opt", "opt", "right"),
    ("Qubits", "qubits", "right"),
    ("Ancillas", "ancillas", "right"),
    ("Gates", "gates", "right"),
    ("X", "x", "right"),
    ("CX", "cx", "right"),
    ("CCX", "ccx", "right"),
    ("MCX", "mcx", "right"),
    ("Depth", "depth", "right"),
    ("T estimate", "t_estimate", "right"),
    ("Compile time (ms)", "compile_time_ms", "right"),
)

RowValue: TypeAlias = str | int | float
CompileFn: TypeAlias = Callable[..., Any]
ClockFn: TypeAlias = Callable[[], float]
ReportFn: TypeAlias = Callable[[Any], ResourceReport]


@dataclass(frozen=True)
class BenchmarkSpec:
    """One source function in the fixed benchmark order."""

    name: str
    function: str
    source: Path


BENCHMARKS: tuple[BenchmarkSpec, ...] = (
    BenchmarkSpec("add", "add", Path("examples/benchmarks.c")),
    BenchmarkSpec("multiply", "multiply", Path("examples/benchmarks.c")),
    BenchmarkSpec("compare", "compare", Path("examples/benchmarks.c")),
    BenchmarkSpec("popcount", "popcount", Path("examples/benchmarks.c")),
    BenchmarkSpec("is_solution", "is_solution", Path("examples/benchmarks.c")),
)


@dataclass(frozen=True)
class BenchmarkRow:
    """A measured resource row with a stable serialization schema."""

    benchmark: str
    opt: int
    qubits: int
    ancillas: int
    gates: int
    x: int
    cx: int
    ccx: int
    mcx: int
    depth: int
    t_estimate: int
    compile_time_ms: float

    def as_dict(self) -> dict[str, RowValue]:
        """Return values in the documented, deterministic column order."""

        return {
            "benchmark": self.benchmark,
            "opt": self.opt,
            "qubits": self.qubits,
            "ancillas": self.ancillas,
            "gates": self.gates,
            "x": self.x,
            "cx": self.cx,
            "ccx": self.ccx,
            "mcx": self.mcx,
            "depth": self.depth,
            "t_estimate": self.t_estimate,
            "compile_time_ms": self.compile_time_ms,
        }


def _validate_levels(levels: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(levels)
    if not normalized:
        raise ValueError("at least one optimizer level is required")
    if any(level < 0 for level in normalized):
        raise ValueError("optimizer levels must be non-negative")
    if len(set(normalized)) != len(normalized):
        raise ValueError("optimizer levels must not repeat")
    return normalized


def collect_rows(
    root: Path = ROOT,
    *,
    specs: Sequence[BenchmarkSpec] = BENCHMARKS,
    opt_levels: Sequence[int] = OPT_LEVELS,
    compile_fn: CompileFn | None = None,
    report_fn: ReportFn = resource_report,
    clock: ClockFn = time.perf_counter,
) -> tuple[BenchmarkRow, ...]:
    """Compile every selected benchmark and return rows in fixed order.

    ``compile_fn`` and ``clock`` are injectable so tests can verify the row
    schema and ordering without replacing the compiler or inventing timings
    in production runs.
    """

    levels = _validate_levels(opt_levels)
    selected_specs = tuple(specs)
    if not selected_specs:
        raise ValueError("at least one benchmark is required")
    source_paths = {spec: root / spec.source for spec in selected_specs}
    for source in source_paths.values():
        if not source.is_file():
            raise FileNotFoundError(f"benchmark source not found: {source}")

    compiler = qcc.compile_c if compile_fn is None else compile_fn
    rows: list[BenchmarkRow] = []
    for spec in selected_specs:
        source = source_paths[spec]
        for level in levels:
            started = clock()
            circuit = compiler(source, function=spec.function, opt=level)
            elapsed_ms = (clock() - started) * 1000.0
            if elapsed_ms < 0:
                raise ValueError("benchmark clock moved backwards")
            report = report_fn(circuit)
            rows.append(
                BenchmarkRow(
                    benchmark=spec.name,
                    opt=level,
                    qubits=report.qubits,
                    ancillas=report.ancillas,
                    gates=report.gates,
                    x=report.x,
                    cx=report.cnot,
                    ccx=report.toffoli,
                    mcx=report.mcx,
                    depth=report.depth,
                    t_estimate=report.t_count,
                    compile_time_ms=elapsed_ms,
                )
            )
    return tuple(rows)


def _format_cell(key: str, value: RowValue) -> str:
    if key == "compile_time_ms":
        if not isinstance(value, (int, float)):
            raise TypeError("compile time must be numeric")
        return f"{value:.3f}"
    return str(value)


def render_markdown(rows: Sequence[BenchmarkRow]) -> str:
    """Render rows without timestamps, host paths, or other unstable metadata."""

    if not rows:
        raise ValueError("cannot render an empty benchmark report")
    header = "| " + " | ".join(column[0] for column in _TABLE_COLUMNS) + " |"
    separator = (
        "| "
        + " | ".join(
            "---" if alignment == "left" else "---:"
            for _, _, alignment in _TABLE_COLUMNS
        )
        + " |"
    )
    lines = [
        "# v0.4 benchmark measurements",
        "",
        "Generated by `python scripts/benchmarks.py` from the checked-in "
        "`examples/benchmarks.c` fixture.",
        "",
        "Resource columns are measured from the compiled circuit's "
        "`qcc.resource_report` result. `Gates` is the logical netlist operation "
        "count; `MCX` is the logical multi-controlled-X count, while the T "
        "estimate uses qcc's clean-ancilla Toffoli accounting.",
        "",
        "Compile time is the measured wall-clock duration of each real "
        "`qcc.compile_c` call, reported in milliseconds to three decimal places. "
        "It varies with the host and is not used to infer resource reductions.",
        "",
        "Rows are emitted in the fixed order `add`, `multiply`, `compare`, "
        "`popcount`, `is_solution`, with optimizer levels 0 and 2 adjacent. "
        "O0 is the v0.3 baseline; O2 enables the conservative v0.4 SSA, gate, "
        "and clean-ancilla reuse passes. Reductions are measured, not inferred.",
        "",
        header,
        separator,
    ]
    for row in rows:
        values = row.as_dict()
        lines.append(
            "| "
            + " | ".join(_format_cell(key, values[key]) for _, key, _ in _TABLE_COLUMNS)
            + " |"
        )
    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            "Full report:",
            "",
            "```console",
            "$ python scripts/benchmarks.py",
            "```",
            "",
            "Smoke check (the `add` fixture at both default levels):",
            "",
            "```console",
            "$ python scripts/benchmarks.py --smoke",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _level(value: str) -> int:
    text = value.strip().upper()
    if text.startswith("O"):
        text = text[1:]
    try:
        level = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--opt expects a non-negative integer"
        ) from exc
    if level < 0:
        raise argparse.ArgumentTypeError("--opt expects a non-negative integer")
    return level


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "benchmarks.md",
        metavar="PATH",
        help="write the full report to PATH (default: docs/benchmarks.md)",
    )
    parser.add_argument(
        "--opt",
        dest="opt_levels",
        action="append",
        type=_level,
        metavar="LEVEL",
        help="optimizer level to measure; repeat for multiple levels (default: 0, 2)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="measure only add at the selected levels and print the report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    levels = tuple(args.opt_levels) if args.opt_levels else OPT_LEVELS
    specs = BENCHMARKS[:1] if args.smoke else BENCHMARKS
    rows = collect_rows(ROOT, specs=specs, opt_levels=levels)
    report = render_markdown(rows)
    if args.smoke:
        sys.stdout.write(report)
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"wrote {args.output} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
