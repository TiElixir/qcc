"""Public qcc compiler pipeline and reversible-circuit building blocks."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from qcc.errors import (
    ClangCompilationError,
    ClangNotFoundError,
    CompilerError,
    Diagnostic,
    LLVMParseError,
    SourceLocation,
    UnsupportedConstructError,
)
from qcc.ir.netlist import (
    CircuitBuilder,
    Gate,
    Qubit,
    QubitAllocator,
    ResourceCounts,
)

from .backend.report import ResourceReport, resource_report
from .lower.ancilla import CircuitLayout as _LoweringCircuitLayout
from .lower.bennett import Gate as _LoweringGate
from .lower.ssa_to_rev import (
    LoweredCircuit,
    LoweringError,
    SSAFormatError,
    UnsupportedSSAOperation,
    lower_ssa,
)


def _load_netlist() -> tuple[Any | None, Any, Any]:
    for module_name in ("qcc.netlist", "qcc.circuit", "qcc.ir.netlist", "qcc.ir"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        circuit = getattr(module, "Circuit", None)
        layout = getattr(module, "CircuitLayout", _LoweringCircuitLayout)
        if circuit is not None:
            return module, circuit, layout
    return None, _FallbackCircuit, _LoweringCircuitLayout


@dataclass(frozen=True)
class _FallbackCircuit:
    """Minimal backend used until the netlist package is installed.

    The real circuit agent supplies ``qcc.netlist.Circuit``.  Keeping this
    fallback makes the lowering branch directly testable and gives the public
    API the same data and simulation contract in isolation.
    """

    gates: tuple[_LoweringGate, ...]
    num_qubits: int
    layout: _LoweringCircuitLayout

    @property
    def qubit_count(self) -> int:
        return self.num_qubits

    @property
    def n_qubits(self) -> int:
        return self.num_qubits

    @classmethod
    def from_gates(
        cls,
        gates: Sequence[_LoweringGate],
        num_qubits: int,
        layout: _LoweringCircuitLayout,
    ) -> _FallbackCircuit:
        return cls(tuple(gates), int(num_qubits), layout)

    def simulate(self, inputs: Any = None) -> int:
        return _simulate_gate_stream(self, inputs)


_netlist_module, _circuit_type, _BaseCircuitLayout = _load_netlist()
Circuit: Any = _circuit_type


if _netlist_module is not None and hasattr(_BaseCircuitLayout, "registers"):

    class _PublicCircuitLayout(_BaseCircuitLayout):  # type: ignore[misc, valid-type]
        """Netlist layout with explicit ``inputs`` and ``output`` views."""

        def __init__(
            self,
            registers: Mapping[str, Sequence[int]] | None = None,
            ancillas: Sequence[int] = (),
            num_qubits: int | None = None,
            domain: Any | None = None,
            *,
            inputs: Mapping[str, Sequence[int]] | None = None,
            output: Sequence[int] | None = None,
        ) -> None:
            merged = dict(registers or {})
            if inputs is not None:
                merged.update({name: tuple(bits) for name, bits in inputs.items()})
            if output is not None:
                merged["output"] = tuple(output)
            super().__init__(
                merged,
                ancillas=ancillas,
                num_qubits=num_qubits,
                domain=domain,
            )

        @property
        def inputs(self) -> Mapping[str, tuple[int, ...]]:
            return {
                name: tuple(bits)
                for name, bits in self.registers.items()
                if name != "output"
            }

        @property
        def output(self) -> tuple[int, ...]:
            return tuple(self.registers.get("output", ()))

    _layout_class: Any = _PublicCircuitLayout
else:
    _layout_class = _BaseCircuitLayout

CircuitLayout: Any = _layout_class


def _attribute(value: Any, names: Sequence[str], default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        try:
            candidate = getattr(value, name)
        except AttributeError:
            continue
        if candidate is not None:
            return candidate
    return default


def _call_variants(
    callable_object: Callable[..., Any],
    variants: Sequence[tuple[tuple[Any, ...], dict[str, Any]]],
) -> Any:
    last_error: TypeError | None = None
    for args, kwargs in variants:
        try:
            return callable_object(*args, **kwargs)
        except TypeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise TypeError("no call variants supplied")


def _external_layout(layout: _LoweringCircuitLayout) -> Any:
    if CircuitLayout is _LoweringCircuitLayout:
        return layout
    variants: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...] = (
        (
            (),
            {
                "inputs": dict(layout.inputs),
                "output": layout.output,
                "ancillas": layout.ancillas,
                "num_qubits": max(
                    (
                        *layout.input_bits,
                        *layout.output,
                        *layout.ancillas,
                    ),
                    default=-1,
                )
                + 1,
            },
        ),
        (
            (
                {**dict(layout.inputs), "output": layout.output},
                layout.ancillas,
                max(
                    (*layout.input_bits, *layout.output, *layout.ancillas),
                    default=-1,
                )
                + 1,
            ),
            {},
        ),
    )
    try:
        return _call_variants(CircuitLayout, variants)
    except TypeError:
        # A backend may intentionally keep layout as a plain structural object.
        return layout


def _external_gate(gate: _LoweringGate) -> Any:
    if _netlist_module is None:
        return gate
    gate_class = getattr(_netlist_module, "Gate", None)
    if gate_class is None:
        return gate
    variants: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...] = (
        ((gate.name, gate.qubits), {}),
        ((), {"name": gate.name, "qubits": gate.qubits}),
        ((), {"opcode": gate.name, "qubits": gate.qubits}),
        ((gate.name, *gate.qubits), {}),
    )
    try:
        return _call_variants(gate_class, variants)
    except TypeError:
        return gate


def _construct_circuit(lowered: LoweredCircuit) -> Any:
    if Circuit is _FallbackCircuit:
        return _FallbackCircuit(
            tuple(lowered.gates), lowered.num_qubits, lowered.layout
        )
    layout = _external_layout(lowered.layout)
    gates = tuple(_external_gate(gate) for gate in lowered.gates)
    variants: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for factory_name in ("from_gates", "from_operations", "from_netlist"):
        factory = getattr(Circuit, factory_name, None)
        if callable(factory):
            variants.extend(
                [
                    ((gates,), {"num_qubits": lowered.num_qubits, "layout": layout}),
                    ((gates, lowered.num_qubits, layout), {}),
                    ((gates, layout), {"num_qubits": lowered.num_qubits}),
                    ((gates,), {"width": lowered.num_qubits, "layout": layout}),
                ]
            )
    variants.extend(
        [
            ((), {"gates": gates, "num_qubits": lowered.num_qubits, "layout": layout}),
            (
                (),
                {
                    "operations": gates,
                    "num_qubits": lowered.num_qubits,
                    "layout": layout,
                },
            ),
            ((), {"gates": gates, "n_qubits": lowered.num_qubits, "layout": layout}),
            ((gates, lowered.num_qubits, layout), {}),
            ((gates, lowered.num_qubits), {}),
            ((gates, layout), {}),
        ]
    )
    try:
        return _call_variants(Circuit, variants)
    except TypeError as exc:
        raise TypeError(
            "could not construct the installed qcc Circuit; expected a "
            "from_gates factory or a (gates, num_qubits, layout) constructor"
        ) from exc


def _looks_like_circuit(value: Any) -> bool:
    if value is None:
        return False
    gates = _attribute(value, ("gates", "operations", "ops"), None)
    if gates is None:
        return False
    # Existing netlists are valid compiler inputs even when they intentionally
    # omit named registers (for example, an anonymous circuit fragment).
    has_layout = _attribute(value, ("layout",), None) is not None
    has_width = (
        _attribute(value, ("num_qubits", "qubit_count", "n_qubits", "width"), None)
        is not None
    )
    return has_layout or has_width


def _function_name(value: Any) -> str | None:
    name = _attribute(value, ("name", "identifier"), None)
    return str(name) if name is not None else None


def _parse_frontend_text(value: Any) -> Any:
    """Turn textual LLVM returned by the clang adapter into an SSA module."""

    if isinstance(value, bytes):
        value = value.decode()
    if not isinstance(value, str):
        return value
    for module_name in ("qcc.frontend.llvm_parser", "qcc.frontend.parser"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        parser = getattr(module, "parse_module", None) or getattr(module, "parse", None)
        if callable(parser):
            return parser(value)
    raise ImportError("qcc frontend returned LLVM text but no LLVM parser is available")


def _materialize_frontend_result(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        return _parse_frontend_text(value)
    if isinstance(value, tuple):
        return tuple(_materialize_frontend_result(item) for item in value)
    return value


def _select_function(value: Any, function: str | None) -> Any:
    if _looks_like_circuit(value):
        return value
    if isinstance(value, tuple):
        # Frontends commonly return (module, diagnostics) or (module, function).
        for item in value:
            if item is not None:
                try:
                    selected = _select_function(item, function)
                except LookupError:
                    continue
                if selected is not item or _looks_like_ssa(selected):
                    return selected
    getter = _attribute(value, ("get_function", "function_named"), None)
    if callable(getter) and function is not None:
        try:
            return getter(function)
        except (KeyError, LookupError):
            pass
    functions = _attribute(value, ("functions",), None)
    if functions is not None:
        if isinstance(functions, Mapping):
            if function in functions:
                return functions[function]
            if len(functions) == 1:
                return next(iter(functions.values()))
        else:
            candidates = tuple(functions)
            for candidate in candidates:
                if _function_name(candidate) == function:
                    return candidate
            if len(candidates) == 1:
                return candidates[0]
    singular = _attribute(value, ("function",), None)
    if singular is not None and singular is not value:
        return _select_function(singular, function)
    if _looks_like_ssa(value):
        name = _function_name(value)
        if function is None or name is None or name == function:
            return value
    raise LookupError(f"frontend did not produce function {function!r}")


def _looks_like_ssa(value: Any) -> bool:
    return (
        value is not None
        and (
            _attribute(
                value, ("instructions", "ops", "operations", "blocks", "body"), None
            )
            is not None
        )
        and (
            _attribute(value, ("args", "arguments", "params", "parameters"), None)
            is not None
            or _attribute(value, ("return_type", "result_type"), None) is not None
        )
    )


def _source_and_path(source_or_path: Any) -> tuple[Any, str | None, Path | None]:
    if isinstance(source_or_path, Path):
        return source_or_path, None, source_or_path
    if isinstance(source_or_path, str):
        candidate = Path(source_or_path)
        if (
            "\n" not in source_or_path
            and "\r" not in source_or_path
            and candidate.is_file()
        ):
            return source_or_path, None, candidate
        return source_or_path, source_or_path, None
    return source_or_path, None, None


def _invoke_frontend(
    frontend_callable: Callable[..., Any],
    source_or_path: Any,
    function: str | None,
    opt: int,
) -> Any:
    source_value, source_text, path = _source_and_path(source_or_path)
    try:
        signature = inspect.signature(frontend_callable)
    except (TypeError, ValueError):
        return frontend_callable(source_value, function=function, opt=opt)

    kwargs: dict[str, Any] = {}
    positional: list[Any] = []
    parameters = tuple(signature.parameters.values())
    source_bound = False
    for parameter in parameters:
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        name = parameter.name.lower()
        value: Any
        if name in {"source_or_path", "source_path", "input_path", "path", "filename"}:
            value = path if path is not None else source_value
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                positional.append(value)
            else:
                kwargs[parameter.name] = value
            source_bound = True
        elif name in {
            "source",
            "source_text",
            "text",
            "code",
            "c_source",
            "source_code",
        }:
            value = (
                source_text
                if source_text is not None
                else (path.read_text() if path is not None else source_value)
            )
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                positional.append(value)
            else:
                kwargs[parameter.name] = value
            source_bound = True
        elif name in {"function", "function_name", "entry", "entrypoint"}:
            if parameter.kind is not inspect.Parameter.POSITIONAL_ONLY:
                kwargs[parameter.name] = function
        elif name in {"opt", "optimization", "optimization_level", "opt_level", "o"}:
            if parameter.kind is not inspect.Parameter.POSITIONAL_ONLY:
                kwargs[parameter.name] = opt
        elif parameter.default is inspect.Parameter.empty and not source_bound:
            positional.append(source_value)
            source_bound = True
    if not source_bound:
        positional.insert(0, source_value)
    return frontend_callable(*positional, **kwargs)


def _find_frontend_callable() -> Callable[..., Any]:
    module_names = (
        "qcc.frontend",
        "qcc.frontend.clang",
        "qcc.frontend.llvm",
        "qcc.compiler",
    )
    function_names = (
        "compile_source",
        "compile_c",
        "compile",
        "compile_to_ssa",
        "compile_c_to_ssa",
        "from_c",
        "parse_c",
        "clang_to_ssa",
        "frontend",
    )
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for name in function_names:
            candidate = getattr(module, name, None)
            if callable(candidate):
                return cast(Callable[..., Any], candidate)
    raise ImportError(
        "qcc frontend is unavailable; install the frontend implementation "
        "or pass an SSA function object to lower_ssa"
    )


def compile_c(source_or_path: Any, function: str | None = "main", opt: int = 2) -> Any:
    """Compile C source text or a C path into a reversible :class:`Circuit`.

    ``function`` selects the fixed-width SSA function emitted by the frontend;
    ``opt`` is passed through as the frontend's optimization level.  The
    frontend and netlist implementations are imported lazily so callers can
    use the lowering package independently in tests.
    """

    if _looks_like_circuit(source_or_path):
        return source_or_path
    if _looks_like_ssa(source_or_path):
        lowered = lower_ssa(source_or_path, function_name=function)
        return _construct_circuit(lowered)
    frontend = _find_frontend_callable()
    frontend_result = _materialize_frontend_result(
        _invoke_frontend(frontend, source_or_path, function, opt)
    )
    selected = _select_function(frontend_result, function)
    if _looks_like_circuit(selected):
        return selected
    lowered = lower_ssa(selected, function_name=function)
    return _construct_circuit(lowered)


def _input_values(circuit: Any, inputs: Any) -> dict[str, int]:
    layout = _attribute(circuit, ("layout",), None)
    named_inputs = _attribute(layout, ("inputs",), None)
    if not isinstance(named_inputs, Mapping):
        registers = _attribute(layout, ("registers",), {})
        named_inputs = (
            {name: bits for name, bits in registers.items() if name != "output"}
            if isinstance(registers, Mapping)
            else {}
        )
    names = tuple(named_inputs.keys()) if isinstance(named_inputs, Mapping) else ()
    if inputs is None:
        return {name: 0 for name in names}
    if isinstance(inputs, Mapping):
        return {str(name): int(value) for name, value in inputs.items()}
    if isinstance(inputs, int) and len(names) == 1:
        return {names[0]: inputs}
    if isinstance(inputs, Sequence) and not isinstance(inputs, (str, bytes, bytearray)):
        values = tuple(inputs)
        if len(values) != len(names):
            raise ValueError(
                f"expected {len(names)} input register values, got {len(values)}"
            )
        return {name: int(value) for name, value in zip(names, values, strict=True)}
    raise TypeError(
        "inputs must be a mapping, a register-value sequence, or an integer"
    )


def _gate_parts(gate: Any) -> tuple[str, tuple[int, ...]]:
    name = _attribute(gate, ("name", "opcode", "operation", "kind"), None)
    if name is None:
        name = str(gate).split("(", 1)[0]
    name = str(name).lower().replace("cnot", "cx").replace("toffoli", "ccx")
    qubits = _attribute(gate, ("qubits", "wires", "bits"), None)
    if qubits is None:
        controls = _attribute(gate, ("controls",), ())
        target = _attribute(gate, ("target",), None)
        qubits = tuple(controls) + ((target,) if target is not None else ())
    return name, tuple(int(qubit) for qubit in qubits)


def _simulate_gate_stream(circuit: Any, inputs: Any = None) -> int:
    layout = _attribute(circuit, ("layout",), None)
    input_bits = _attribute(layout, ("inputs",), None)
    if not isinstance(input_bits, Mapping):
        registers = _attribute(layout, ("registers",), {})
        input_bits = (
            {name: bits for name, bits in registers.items() if name != "output"}
            if isinstance(registers, Mapping)
            else {}
        )
    output_value = _attribute(layout, ("output",), None)
    if output_value is None:
        registers = _attribute(layout, ("registers",), {})
        output_value = (
            registers.get("output", ()) if isinstance(registers, Mapping) else ()
        )
    output_bits = tuple(output_value or ())
    num_qubits = int(_attribute(circuit, ("num_qubits", "qubit_count", "n_qubits"), 0))
    bits = [0] * num_qubits
    values = _input_values(circuit, inputs)
    for name, value in values.items():
        if name not in input_bits:
            raise KeyError(f"unknown circuit input {name!r}")
        for bit, qubit in enumerate(input_bits[name]):
            bits[int(qubit)] = (value >> bit) & 1
    gates = _attribute(circuit, ("gates", "operations", "ops"), ())
    for gate in gates:
        name, qubits = _gate_parts(gate)
        if name == "x":
            bits[qubits[0]] ^= 1
        elif name == "cx":
            bits[qubits[1]] ^= bits[qubits[0]]
        elif name == "ccx":
            bits[qubits[2]] ^= bits[qubits[0]] & bits[qubits[1]]
        else:
            raise ValueError(f"cannot simulate unsupported gate {name!r}")
    return sum(bits[int(qubit)] << index for index, qubit in enumerate(output_bits))


def simulate(
    circuit_or_source: Any,
    inputs: Any = None,
    function: str | None = "main",
    opt: int = 2,
) -> int:
    """Simulate a compiled circuit or compile and simulate C source/path."""

    circuit = (
        compile_c(circuit_or_source, function=function, opt=opt)
        if not _looks_like_circuit(circuit_or_source)
        else circuit_or_source
    )
    method = getattr(circuit, "simulate", None)
    if callable(method):
        values = _input_values(circuit, inputs)
        try:
            return int(method(values))
        except TypeError:
            try:
                return int(method(inputs=values))
            except TypeError:
                return int(method()) if inputs is None else int(method(inputs))
    for module_name in ("qcc.simulator", "qcc.netlist"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        simulator = getattr(module, "simulate", None)
        if callable(simulator):
            return int(simulator(circuit, _input_values(circuit, inputs)))
    return _simulate_gate_stream(circuit, inputs)


__all__ = [
    "Circuit",
    "CircuitBuilder",
    "CircuitLayout",
    "Gate",
    "Qubit",
    "QubitAllocator",
    "ResourceCounts",
    "ClangCompilationError",
    "ClangNotFoundError",
    "CompilerError",
    "Diagnostic",
    "LLVMParseError",
    "SourceLocation",
    "UnsupportedConstructError",
    "LoweredCircuit",
    "LoweringError",
    "ResourceReport",
    "SSAFormatError",
    "UnsupportedSSAOperation",
    "compile_c",
    "lower_ssa",
    "resource_report",
    "simulate",
]
