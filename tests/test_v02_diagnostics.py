"""Source-aware rejection tests for constructs outside the v0.2 subset."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

import qcc
from qcc.errors import CompilerError, UnsupportedConstructError

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class UnsupportedCase:
    fixture: str
    function: str
    marker: str
    terms: tuple[str, ...]


CASES = (
    UnsupportedCase(
        "unsupported_v02_data_loop.c",
        "data_dependent_loop",
        "data-dependent loop",
        ("data dependent", "loop"),
    ),
    UnsupportedCase(
        "unsupported_v02_malloc.c",
        "uses_malloc",
        "malloc",
        ("malloc",),
    ),
    UnsupportedCase(
        "unsupported_v02_float.c",
        "uses_float",
        "float",
        ("float",),
    ),
    UnsupportedCase(
        "unsupported_v02_printf.c",
        "uses_printf",
        "printf I/O",
        ("printf",),
    ),
    UnsupportedCase(
        "unsupported_v02_function_pointer.c",
        "uses_function_pointer",
        "function pointer",
        ("function pointer",),
    ),
    UnsupportedCase(
        "unsupported_v02_volatile.c",
        "uses_volatile",
        "volatile",
        ("volatile",),
    ),
    UnsupportedCase(
        "unsupported_v02_atomic.c",
        "uses_atomic",
        "atomic",
        ("atomic",),
    ),
    UnsupportedCase(
        "unsupported_v02_inline_asm.c",
        "uses_inline_asm",
        "inline asm",
        ("inline asm",),
    ),
    UnsupportedCase(
        "unsupported_v02_global.c",
        "reads_global",
        "global mutable state",
        ("global", "mutable"),
    ),
    UnsupportedCase(
        "unsupported_v02_recursion.c",
        "recursive_value",
        "recursion",
        ("recurs",),
    ),
    UnsupportedCase(
        "unsupported_v02_pointer.c",
        "unresolved_pointer",
        "unresolved pointer",
        ("unresolved pointer",),
    ),
)


def _marker_line(source: Path, marker: str) -> int:
    for line_number, line in enumerate(source.read_text().splitlines(), start=1):
        if f"QCC_UNSUPPORTED: {marker}" in line:
            return line_number
    raise AssertionError(f"missing diagnostic marker {marker!r} in {source}")


def _diagnostic_text(error: CompilerError) -> str:
    diagnostics = getattr(error, "diagnostics", ())
    return "\n".join((str(error), *(str(item) for item in diagnostics)))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.function)
def test_unsupported_constructs_have_source_aware_diagnostics(
    case: UnsupportedCase,
) -> None:
    source = FIXTURE_DIR / case.fixture
    expected_line = _marker_line(source, case.marker)

    with pytest.raises(CompilerError) as captured:
        qcc.compile_c(source, function=case.function)

    error = captured.value
    assert isinstance(error, UnsupportedConstructError)
    assert error.diagnostic.code == "unsupported-construct"
    location = error.location
    assert location is not None
    assert Path(location.source_name).name == source.name
    assert location.line == expected_line

    normalized = re.sub(r"[^a-z0-9]+", " ", _diagnostic_text(error).lower())
    for term in case.terms:
        assert term in normalized
