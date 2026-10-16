"""Clang driver wrapper used by the qcc LLVM frontend."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TypeAlias

from qcc.errors import (
    ClangCompilationError,
    ClangNotFoundError,
    Diagnostic,
    DiagnosticSeverity,
    SourceInputError,
    SourceLocation,
)

SourceInput: TypeAlias = str | os.PathLike[str]

# These options are deliberately explicit: the parser consumes textual LLVM,
# and vectorization can produce IR outside the v0.1 straight-line subset.
_REQUIRED_FLAGS: tuple[str, ...] = (
    "-std=c11",
    "-O1",
    "-S",
    "-emit-llvm",
    "-fno-vectorize",
    "-fno-slp-vectorize",
)
_OPTIONAL_PRESERVE_NAMES_FLAG = "-fno-discard-value-names"
_DIAGNOSTIC_RE = re.compile(
    r"^(?P<source>.*?):(?P<line>[0-9]+):(?P<column>[0-9]+):\s*"
    r"(?P<severity>fatal error|error|warning|note):\s*(?P<message>.*)$"
)


def _resolve_executable(executable: str | os.PathLike[str]) -> str:
    value = os.fspath(executable)
    resolved = shutil.which(value)
    if resolved is None:
        raise ClangNotFoundError(value)
    return resolved


def _existing_source_path(source: SourceInput) -> Path | None:
    if isinstance(source, os.PathLike):
        path = Path(source)
        if not path.is_file():
            raise SourceInputError(
                f"C source path {str(path)!r} does not exist or is not a file"
            )
        return path
    try:
        candidate = Path(source)
        if candidate.is_file():
            return candidate
    except (OSError, ValueError):
        # A source string can contain characters that are not valid in a path.
        pass
    return None


def _parse_diagnostics(stderr: str, default_source: str) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    for line in stderr.splitlines():
        match = _DIAGNOSTIC_RE.match(line.strip())
        if match is None:
            continue
        source = match.group("source") or default_source
        if source in {"<stdin>", "-"} and default_source not in {"<stdin>", "-"}:
            source = default_source
        severity_name = match.group("severity")
        severity = (
            DiagnosticSeverity.WARNING
            if severity_name == "warning"
            else DiagnosticSeverity.NOTE
            if severity_name == "note"
            else DiagnosticSeverity.ERROR
        )
        diagnostics.append(
            Diagnostic(
                match.group("message"),
                location=SourceLocation(
                    source,
                    int(match.group("line")),
                    int(match.group("column")),
                ),
                severity=severity,
            )
        )
    return tuple(diagnostics)


def _flag_was_rejected(stderr: str, flag: str) -> bool:
    if flag not in stderr:
        return False
    lowered = stderr.lower()
    return any(
        phrase in lowered
        for phrase in (
            "unknown argument",
            "unknown option",
            "unsupported option",
            "invalid argument",
            "unrecognized command line option",
        )
    )


def _display_command(command: Sequence[str]) -> str:
    # repr() keeps paths containing spaces unambiguous without depending on a
    # shell-specific quoting implementation.
    return " ".join(repr(item) for item in command)


def compile_c(
    source: SourceInput,
    *,
    source_name: str | None = None,
    clang: str | os.PathLike[str] = "clang",
    extra_flags: Iterable[str] = (),
    timeout: float | None = 120.0,
    cwd: str | os.PathLike[str] | None = None,
    executable: str | os.PathLike[str] | None = None,
) -> str:
    """Compile C source text or a C path to textual LLVM IR.

    The input is sent through Clang's driver with C11, ``-O1``, textual LLVM,
    and both vectorizers disabled.  Clang versions that support
    ``-fno-discard-value-names`` receive it; if that one option is rejected,
    the invocation is retried without it so numeric SSA identifiers remain a
    supported fallback.

    ``source`` is treated as a path when it is an ``os.PathLike`` object or an
    existing path string.  Any other string is compiled from stdin.
    """

    if executable is not None:
        clang = executable
    executable_path = _resolve_executable(clang)
    path = _existing_source_path(source)
    if path is not None:
        input_name = source_name or str(path)
        input_argument = str(path.resolve())
        stdin_text: str | None = None
    else:
        input_name = source_name or "<stdin>"
        input_argument = "-"
        stdin_text = source if isinstance(source, str) else os.fspath(source)

    flags = [*_REQUIRED_FLAGS, _OPTIONAL_PRESERVE_NAMES_FLAG, *tuple(extra_flags)]
    base_command = [
        executable_path,
        *flags,
        "-x",
        "c",
        input_argument,
        "-o",
        "-",
    ]

    while True:
        try:
            completed = subprocess.run(
                base_command,
                input=stdin_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                cwd=cwd,
            )
        except FileNotFoundError as exc:
            # A race can remove an executable after shutil.which().
            raise ClangNotFoundError(os.fspath(clang)) from exc
        except PermissionError as exc:
            raise ClangCompilationError(
                f"clang executable {os.fspath(clang)!r} is not executable",
                command=base_command,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ClangCompilationError(
                f"clang did not finish within {timeout} seconds",
                command=base_command,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
            ) from exc
        except OSError as exc:
            raise ClangCompilationError(
                f"could not execute clang: {exc}",
                command=base_command,
            ) from exc

        if completed.returncode == 0:
            if not completed.stdout.strip():
                raise ClangCompilationError(
                    "clang completed successfully but emitted no textual LLVM IR",
                    command=base_command,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            return completed.stdout

        rejected_optional = next(
            (
                flag
                for flag in (_OPTIONAL_PRESERVE_NAMES_FLAG, *(_REQUIRED_FLAGS[4:],))
                if flag in base_command and _flag_was_rejected(completed.stderr, flag)
            ),
            None,
        )
        if rejected_optional is not None:
            base_command.remove(rejected_optional)
            continue

        diagnostics = _parse_diagnostics(completed.stderr, input_name)
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "no diagnostic output"
        )
        location = diagnostics[0].location if diagnostics else None
        raise ClangCompilationError(
            f"clang failed to compile {input_name!r}; command: "
            f"{_display_command(base_command)}\n{detail}",
            command=base_command,
            stdout=completed.stdout,
            stderr=completed.stderr,
            location=location,
            diagnostics=diagnostics,
        )


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


# Names used by callers that describe the operation rather than the language.
compile_source = compile_c
compile_llvm = compile_c
run_clang = compile_c
invoke_clang = compile_c


__all__ = [
    "SourceInput",
    "compile_c",
    "compile_llvm",
    "compile_source",
    "invoke_clang",
    "run_clang",
]
