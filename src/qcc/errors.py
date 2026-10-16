"""Diagnostics and typed errors shared by qcc frontends.

The frontend deliberately keeps diagnostics small and dependency-free.  A
``SourceLocation`` can be attached to every parser diagnostic, while the
exception subclasses let callers distinguish a missing tool, a failed C
compilation, malformed LLVM, and a construct that is outside qcc's supported
subset.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum


class DiagnosticSeverity(str, Enum):
    """Severity used by a :class:`Diagnostic`."""

    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """A source position suitable for compiler and parser diagnostics.

    Lines and columns are one-based when present.  ``source_name`` is allowed
    to be a virtual name such as ``<stdin>`` or ``<llvm>``.
    """

    source_name: str = "<unknown>"
    line: int | None = None
    column: int | None = None

    def __post_init__(self) -> None:
        if self.line is not None and self.line < 1:
            raise ValueError("source line numbers are one-based")
        if self.column is not None and self.column < 1:
            raise ValueError("source column numbers are one-based")

    def __str__(self) -> str:
        if self.line is None:
            return self.source_name
        if self.column is None:
            return f"{self.source_name}:{self.line}"
        return f"{self.source_name}:{self.line}:{self.column}"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A structured compiler diagnostic."""

    message: str
    location: SourceLocation | None = None
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    code: str | None = None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("a diagnostic message cannot be empty")
        object.__setattr__(self, "notes", tuple(self.notes))

    def __str__(self) -> str:
        prefix = f"{self.location}: " if self.location is not None else ""
        code = f" [{self.code}]" if self.code else ""
        result = f"{prefix}{self.severity.value}{code}: {self.message}"
        if self.notes:
            result += "\n" + "\n".join(f"note: {note}" for note in self.notes)
        return result


class CompilerError(Exception):
    """Base class for all expected qcc compilation failures."""

    diagnostic: Diagnostic
    diagnostics: tuple[Diagnostic, ...]

    def __init__(
        self,
        message: str | Diagnostic,
        location: SourceLocation | None = None,
        *,
        code: str | None = None,
        diagnostics: Iterable[Diagnostic] = (),
    ) -> None:
        primary = (
            message
            if isinstance(message, Diagnostic)
            else Diagnostic(message, location=location, code=code)
        )
        extra = tuple(diagnostics)
        self.diagnostic = primary
        self.diagnostics = (primary, *extra)
        super().__init__(str(primary))

    @property
    def location(self) -> SourceLocation | None:
        """The primary diagnostic's source location, if one was supplied."""

        return self.diagnostic.location

    @property
    def message(self) -> str:
        """The primary diagnostic message without its source prefix."""

        return self.diagnostic.message


class SourceInputError(CompilerError):
    """The requested C source path could not be used as input."""


class ClangNotFoundError(CompilerError):
    """The requested Clang executable is not available."""

    executable: str

    def __init__(self, executable: str) -> None:
        self.executable = executable
        super().__init__(
            f"clang executable {executable!r} was not found; install Clang/LLVM "
            "or pass the path to a clang executable",
            code="clang-not-found",
        )


class ClangCompilationError(CompilerError):
    """Clang was found but rejected the source or failed to emit LLVM IR."""

    command: tuple[str, ...]
    stdout: str
    stderr: str

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str] = (),
        stdout: str = "",
        stderr: str = "",
        location: SourceLocation | None = None,
        diagnostics: Iterable[Diagnostic] = (),
    ) -> None:
        self.command = tuple(command)
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            message,
            location=location,
            code="clang-compilation-failed",
            diagnostics=diagnostics,
        )


class LLVMParseError(CompilerError):
    """LLVM text was malformed or could not be represented by the v0.1 IR."""


class UnsupportedConstructError(CompilerError):
    """A valid construct exists, but it is outside qcc's supported subset."""

    construct: str

    def __init__(
        self,
        construct: str,
        message: str | None = None,
        location: SourceLocation | None = None,
    ) -> None:
        self.construct = construct
        detail = message or f"unsupported construct: {construct}"
        super().__init__(detail, location=location, code="unsupported-construct")


# Friendly aliases used by callers that prefer shorter names.
ParseError = LLVMParseError
UnsupportedLLVMConstructError = UnsupportedConstructError
ClangError = ClangCompilationError


def format_diagnostics(diagnostics: Iterable[Diagnostic]) -> str:
    """Render a sequence of diagnostics as one human-readable string."""

    return "\n".join(str(diagnostic) for diagnostic in diagnostics)


__all__ = [
    "ClangCompilationError",
    "ClangError",
    "ClangNotFoundError",
    "CompilerError",
    "Diagnostic",
    "DiagnosticSeverity",
    "LLVMParseError",
    "ParseError",
    "SourceInputError",
    "SourceLocation",
    "UnsupportedConstructError",
    "UnsupportedLLVMConstructError",
    "format_diagnostics",
]
