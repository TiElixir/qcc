"""Command-line interface for qcc.

The CLI is deliberately a thin orchestration layer.  Frontend and lowering
work is supplied by the compiler package; this module discovers its public
``compile_c`` entry point at runtime, formats the result, and translates
failures into useful command-line diagnostics.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, NoReturn, cast


class CLIError(Exception):
    """An expected command-line or compilation failure."""


class _ArgumentError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


# Tests and embedding applications can provide a compiler without importing
# the not-yet-built frontend at module import time.  The public package still
# owns the actual compile_c implementation.
compile_c: Callable[..., Any] | None = None


def _optimization_level(value: str) -> int:
    text = value.strip().upper()
    text = text.removeprefix("O")
    try:
        level = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--opt expects a non-negative integer"
        ) from exc
    if level < 0:
        raise argparse.ArgumentTypeError("--opt expects a non-negative integer")
    if level > 2:
        raise argparse.ArgumentTypeError("--opt expects O0, O1, or O2")
    return level


def _positive_samples(value: str) -> int:
    try:
        samples = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--samples expects a positive integer"
        ) from exc
    if samples < 1:
        raise argparse.ArgumentTypeError("--samples expects a positive integer")
    return samples


def _add_common_options(parser: argparse.ArgumentParser, *, emit: str) -> None:
    parser.add_argument(
        "source",
        metavar="SOURCE",
        help="C source file to compile",
    )
    parser.add_argument(
        "--function",
        default="main",
        metavar="NAME",
        help="function to compile (default: main)",
    )
    parser.add_argument(
        "--emit",
        choices=("qasm3", "json", "stats"),
        default=emit,
        help="output format (default: %(default)s)",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="write output to PATH, or '-' for standard output",
    )
    parser.add_argument(
        "--opt",
        type=_optimization_level,
        default=0,
        metavar="LEVEL",
        help="optimizer level (default: 0; O0/O1/O2 are accepted)",
    )
    parser.add_argument(
        "--samples",
        type=_positive_samples,
        metavar="N",
        help="request N deterministic simulator samples",
    )
    parser.add_argument(
        "--dump-ir",
        action="store_true",
        help="write textual LLVM IR to standard error",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser used by :func:`main`."""

    parser = _Parser(prog="qcc", description="compile a bounded pure-C quantum circuit")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    compile_parser = commands.add_parser("compile", help="compile and emit a circuit")
    _add_common_options(compile_parser, emit="qasm3")

    stats_parser = commands.add_parser("stats", help="compile and report resources")
    _add_common_options(stats_parser, emit="stats")

    check_parser = commands.add_parser("check", help="compile and validate the circuit")
    _add_common_options(check_parser, emit="stats")

    return parser


def _read_member(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _resolve_compiler() -> Callable[..., Any]:
    if callable(compile_c):
        return compile_c

    candidates: list[tuple[str, str]] = [
        ("qcc", "compile_c"),
        ("qcc.compiler", "compile_c"),
        ("qcc.api", "compile_c"),
    ]
    for module_name, attribute in candidates:
        try:
            module = __import__(module_name, fromlist=[attribute])
        except ImportError:
            continue
        candidate = getattr(module, attribute, None)
        if callable(candidate):
            return cast(Callable[..., Any], candidate)
    raise CLIError(
        "qcc.compile_c is unavailable; install/build the compiler frontend before "
        "using the CLI"
    )


def _parameter_name(
    parameters: Mapping[str, inspect.Parameter], names: Sequence[str]
) -> str | None:
    for name in names:
        if name in parameters:
            return name
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return names[0]
    return None


def _invoke_compiler(args: argparse.Namespace, source: Path) -> Any:
    compiler = _resolve_compiler()
    kwargs: dict[str, Any] = {}
    parameters: Mapping[str, inspect.Parameter]
    try:
        signature = inspect.signature(compiler)
        parameters = signature.parameters
    except (TypeError, ValueError):
        parameters = {}

    function_name = _parameter_name(parameters, ("function", "function_name", "entry"))
    if function_name is not None:
        kwargs[function_name] = args.function
    optimization_name = _parameter_name(
        parameters, ("opt_level", "optimization_level", "opt", "optimization")
    )
    if optimization_name is not None:
        kwargs[optimization_name] = args.opt
    samples_name = _parameter_name(parameters, ("samples", "sample_count"))
    if args.samples is not None and samples_name is not None:
        kwargs[samples_name] = args.samples
    dump_name = _parameter_name(parameters, ("dump_ir", "emit_ir", "return_ir"))
    if dump_name is not None:
        kwargs[dump_name] = args.dump_ir

    try:
        return compiler(str(source), **kwargs)
    except TypeError as exc:
        # A callable with an opaque signature may not accept the keyword shape
        # above.  Retry only after a signature failure, and retain the useful
        # original exception if the compiler itself raised TypeError.
        if parameters:
            raise
        try:
            return compiler(str(source))
        except TypeError:
            raise exc from None


def _result_part(result: Any, names: Sequence[str], default: Any = None) -> Any:
    if isinstance(result, Mapping):
        for name in names:
            if name in result and result[name] is not None:
                return result[name]
    for name in names:
        if hasattr(result, name):
            value = getattr(result, name)
            if value is not None:
                return value
    return default


def _circuit_from_result(result: Any) -> Any:
    if isinstance(result, tuple) and result:
        for item in result:
            if _looks_like_circuit(item):
                return item
        return result[0]
    circuit = _result_part(
        result,
        ("circuit", "netlist", "lowered", "compiled", "program"),
        default=None,
    )
    return result if circuit is None else circuit


def _looks_like_circuit(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            name in value for name in ("operations", "gates", "ops", "instructions")
        )
    return any(
        hasattr(value, name) for name in ("operations", "gates", "ops", "instructions")
    )


def _ir_from_result(result: Any) -> str | None:
    value = _result_part(
        result,
        ("ir", "llvm_ir", "textual_ir", "llvm", "intermediate_representation"),
        default=None,
    )
    if value is None and isinstance(result, tuple):
        for item in result:
            if isinstance(item, str) and ("define" in item or "; ModuleID" in item):
                value = item
                break
    return value if isinstance(value, str) else None


def _operations(circuit: Any) -> list[Any]:
    if isinstance(circuit, Mapping):
        for name in ("operations", "gates", "ops", "instructions"):
            if name in circuit:
                value = circuit[name]
                return list(value)
    for name in ("operations", "gates", "ops", "instructions", "netlist"):
        value = _read_member(circuit, name, default=None)
        if value is not None:
            if callable(value):
                value = value()
            try:
                return list(value)
            except TypeError:
                return []
    try:
        return list(circuit)
    except TypeError:
        return []


def _gate_name(operation: Any) -> str:
    if isinstance(operation, Mapping):
        value = operation.get(
            "name", operation.get("gate", operation.get("opcode", "unknown"))
        )
    elif isinstance(operation, (tuple, list)) and operation:
        value = operation[0]
    else:
        value = _read_member(
            operation, "name", "gate", "opcode", "kind", default="unknown"
        )
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.lower()
    if isinstance(value, str):
        return value.lower()
    return str(value)


def _qubit_operands(operation: Any) -> list[Any]:
    if isinstance(operation, Mapping):
        value = operation.get(
            "qubits",
            operation.get("wires", operation.get("targets", operation.get("operands"))),
        )
        if value is not None:
            return list(value)
        controls = operation.get("controls")
        target = operation.get("target")
    elif isinstance(operation, (tuple, list)):
        return list(operation[1:])
    else:
        value = _read_member(
            operation,
            "qubits",
            "wires",
            "targets",
            "operands",
            "indices",
            default=None,
        )
        if value is not None:
            return list(value)
        controls = _read_member(operation, "controls", default=None)
        target = _read_member(operation, "target", default=None)
    if controls is not None and target is not None:
        if isinstance(controls, (str, bytes)):
            controls = [controls]
        return list(controls) + [target]
    return [target] if target is not None else []


def _stats_from_result(result: Any, circuit: Any) -> dict[str, Any]:
    supplied = _result_part(result, ("stats", "report", "resources"), default=None)
    if callable(supplied):
        supplied = supplied()
    as_dict = getattr(supplied, "as_dict", None)
    if callable(as_dict):
        supplied = as_dict()
    if isinstance(supplied, Mapping):
        stats: dict[str, Any] = {
            str(key): _jsonable(value) for key, value in supplied.items()
        }
    else:
        stats = {}

    operations = _operations(circuit)
    counts = Counter(_gate_name(operation) for operation in operations)
    qubits: set[str] = set()
    last_layer: dict[str, int] = {}
    depth = 0
    for operation in operations:
        operands = _qubit_operands(operation)
        layer = 0
        normalized_operands = []
        for operand in operands:
            token = _jsonable(operand)
            key = json.dumps(token, sort_keys=True, default=str)
            normalized_operands.append(key)
            qubits.add(key)
            layer = max(layer, last_layer.get(key, 0))
        layer += 1
        for key in normalized_operands:
            last_layer[key] = layer
        depth = max(depth, layer)

    stats.setdefault("gate_count", stats.get("gates", len(operations)))
    stats.setdefault("gate_counts", dict(sorted(counts.items())))
    stats.setdefault("qubit_count", stats.get("qubits", len(qubits)))
    stats.setdefault("depth", depth)
    layout = _read_member(circuit, "layout", "circuit_layout", default=None)
    layout_count = _read_member(
        layout, "num_qubits", "n_qubits", "total_qubits", default=None
    )
    if isinstance(layout_count, int):
        stats["qubit_count"] = layout_count
    stats.setdefault("optimizer", None)
    return stats


def _jsonable(value: Any, *, _seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _seen is None:
        _seen = set()
    identifier = id(value)
    if identifier in _seen:
        return "<recursive>"
    _seen.add(identifier)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name), _seen=_seen)
            for item in fields(cast(Any, value))
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item, _seen=_seen) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item, _seen=_seen) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: _jsonable(item, _seen=_seen)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return str(value)


def _write_output(text: str, destination: str | None) -> None:
    if destination is None or destination == "-":
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")
        return
    try:
        Path(destination).write_text(text)
    except OSError as exc:
        raise CLIError(f"cannot write {destination!r}: {exc}") from exc


def _emit_qasm(circuit: Any) -> str:
    try:
        from qcc.backend.qasm3 import emit_qasm3
    except ImportError as exc:
        raise CLIError("the OpenQASM 3 backend is unavailable") from exc
    return emit_qasm3(circuit)


def _emit_for_command(args: argparse.Namespace, result: Any, circuit: Any) -> str:
    if args.emit == "qasm3":
        return _emit_qasm(circuit)
    if args.emit == "stats":
        return (
            json.dumps(_stats_from_result(result, circuit), indent=2, sort_keys=True)
            + "\n"
        )
    if args.emit == "json":
        return json.dumps(_jsonable(result), indent=2, sort_keys=True) + "\n"
    raise CLIError(f"unsupported output format {args.emit!r}")


def _run(args: argparse.Namespace) -> int:
    source = Path(args.source)
    if not source.is_file():
        raise CLIError(f"source file not found: {source}")
    result = _invoke_compiler(args, source)
    circuit = _circuit_from_result(result)

    if args.dump_ir:
        ir = _ir_from_result(result)
        if ir is None:
            print("qcc: warning: compiler returned no textual IR", file=sys.stderr)
        else:
            sys.stderr.write(ir)
            if not ir.endswith("\n"):
                sys.stderr.write("\n")

    if args.command == "check":
        qasm = _emit_qasm(circuit)
        # emit_qasm3 validates by default; importing the helper here also
        # makes check's intent explicit for custom backend implementations.
        from qcc.backend.qasm3 import validate_qasm3

        validate_qasm3(qasm)
        if args.output is not None:
            output = (
                qasm
                if args.emit == "qasm3"
                else _emit_for_command(args, result, circuit)
            )
            _write_output(output, args.output)
        elif args.emit == "qasm3":
            _write_output(qasm, None)
        else:
            print(f"ok: {source} ({args.function})")
        return 0

    _write_output(_emit_for_command(args, result, circuit), args.output)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the qcc CLI and return a process-style status code."""

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _ArgumentError as exc:
        print(f"qcc: error: {exc}", file=sys.stderr)
        print(parser.format_usage().rstrip(), file=sys.stderr)
        return 2
    except SystemExit as exc:
        # argparse uses SystemExit for --help.  Returning the code keeps the
        # function pleasant to call from tests while console entry points still
        # receive the normal process status.
        return exc.code if isinstance(exc.code, int) else 1

    try:
        return _run(args)
    except KeyboardInterrupt:
        print("qcc: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - compiler diagnostics are user-facing
        print(f"qcc: error: {exc}", file=sys.stderr)
        return 1


__all__ = ["CLIError", "build_parser", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
