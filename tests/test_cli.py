"""CLI argument and output acceptance tests."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from qcc import cli


@dataclass(frozen=True)
class LayoutFixture:
    registers: tuple[tuple[str, int], ...] = (("q", 2),)


@dataclass(frozen=True)
class CircuitFixture:
    layout: LayoutFixture = LayoutFixture()
    operations: tuple[tuple[object, ...], ...] = (("cx", 0, 1),)


@dataclass(frozen=True)
class ResultFixture:
    circuit: CircuitFixture = CircuitFixture()
    ir: str = "; ModuleID = 'fixture'\ndefine i8 @add(i8 %a, i8 %b) { ret i8 0 }\n"


def install_fake_compiler(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
) -> None:
    def fake_compile(
        source: str,
        *,
        function: str,
        opt_level: int,
        samples: int | None = None,
        dump_ir: bool = False,
    ) -> ResultFixture:
        calls.append(
            {
                "source": source,
                "function": function,
                "opt_level": opt_level,
                "samples": samples,
                "dump_ir": dump_ir,
            }
        )
        return ResultFixture()

    monkeypatch.setattr(cli, "compile_c", fake_compile)


def test_compile_accepts_requested_options_and_emits_qasm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "input.c"
    source.write_text("int add(int a, int b) { return a + b; }\n")
    calls: list[dict[str, Any]] = []
    install_fake_compiler(monkeypatch, calls)

    status = cli.main(
        [
            "compile",
            str(source),
            "--function",
            "add",
            "--emit",
            "qasm3",
            "--opt",
            "O2",
            "--samples",
            "7",
            "--dump-ir",
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert "OPENQASM 3.0;" in captured.out
    assert "define i8 @add" in captured.err
    assert calls == [
        {
            "source": str(source),
            "function": "add",
            "opt_level": 2,
            "samples": 7,
            "dump_ir": True,
        }
    ]


def test_stats_and_json_can_write_to_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "input.c"
    source.write_text("int add(int a, int b) { return a + b; }\n")
    output = tmp_path / "result.json"
    calls: list[dict[str, Any]] = []
    install_fake_compiler(monkeypatch, calls)

    assert cli.main(["stats", str(source), "-o", str(output)]) == 0
    stats = json.loads(output.read_text())
    assert stats["gate_count"] == 1
    assert stats["gate_counts"] == {"cx": 1}
    assert capsys.readouterr().out == ""

    assert cli.main(["compile", str(source), "--emit", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["circuit"]["operations"] == [["cx", 0, 1]]


def test_compiler_diagnostics_are_reported_as_nonzero_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "unsupported.c"
    source.write_text("float unsupported(float x) { return x; }\n")

    def failing_compile(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unsupported construct: floating-point expression")

    monkeypatch.setattr(cli, "compile_c", failing_compile)

    assert cli.main(["compile", str(source), "--function", "unsupported"]) == 1
    assert "unsupported construct" in capsys.readouterr().err


def test_argument_errors_and_missing_sources_are_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, Any]] = []
    install_fake_compiler(monkeypatch, calls)

    assert cli.main(["compile", str(tmp_path / "missing.c")]) == 1
    assert "source file not found" in capsys.readouterr().err

    assert cli.main(["compile", str(tmp_path / "missing.c"), "--emit", "qasm"]) == 2
    assert "invalid choice" in capsys.readouterr().err
    assert calls == []
