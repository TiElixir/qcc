"""Fast classical simulation of reversible integer-bitmask circuits.

A state is one Python integer: bit ``q`` is the classical value of wire
``q``.  Registers and input assignments use the same LSB-first convention as
:mod:`qcc.ir.netlist`.  The simulator intentionally mirrors the primitive
netlist instead of constructing a state vector, which makes exhaustive small
width checks inexpensive.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from operator import index as _index
from typing import Any, TypeAlias

from qcc.ir.netlist import Circuit, CircuitLayout, Gate, QubitLike

BitValue: TypeAlias = int | bool
TraceSink: TypeAlias = Callable[["TraceEntry"], object]


class AncillaDirtyError(RuntimeError):
    """Raised when a declared clean ancilla is nonzero after simulation."""

    def __init__(self, ancillas: Iterable[QubitLike], state: int) -> None:
        self.ancillas = tuple(int(qubit) for qubit in ancillas)
        self.dirty = tuple(qubit for qubit in self.ancillas if (state >> qubit) & 1)
        self.dirty_qubits = self.dirty
        self.state = state
        rendered = ", ".join(f"q[{qubit}]" for qubit in self.dirty)
        super().__init__(f"declared ancilla(s) left nonzero: {rendered}")


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One simulator transition, including both integer masks."""

    step: int
    gate: Gate
    before: int
    after: int

    @property
    def state(self) -> int:
        return self.after

    @property
    def operation(self) -> str:
        return self.gate.op


def _as_index(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        result = _index(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TypeError(f"expected an integer qubit index, got {value!r}") from exc
    if result < 0:
        raise ValueError("qubit indices must be non-negative")
    return result


def _normalize_register(register: Iterable[QubitLike]) -> tuple[QubitLike, ...]:
    values = tuple(register)
    numeric = tuple(_as_index(value) for value in values)
    if len(set(numeric)) != len(numeric):
        raise ValueError("a register cannot contain duplicate qubits")
    return values


def _layout_of(layout_or_circuit: CircuitLayout | Circuit) -> CircuitLayout:
    if isinstance(layout_or_circuit, Circuit):
        if layout_or_circuit.layout is None:
            raise ValueError("the circuit has no named register layout")
        return layout_or_circuit.layout
    return layout_or_circuit


def _coerce_value(value: object, width: int) -> int:
    if isinstance(value, bool):
        integer = int(value)
    elif isinstance(value, int):
        integer = value
    elif isinstance(value, Iterable):
        bits = tuple(int(bit) for bit in value)
        if len(bits) != width:
            raise ValueError(f"expected {width} bits, got {len(bits)}")
        if any(bit not in (0, 1) for bit in bits):
            raise ValueError("register bit iterables must contain only 0 and 1")
        integer = sum(bit << offset for offset, bit in enumerate(bits))
    else:
        raise TypeError("register values must be integers or bit iterables")
    if integer < 0 or integer >= (1 << width):
        raise ValueError(f"value {integer} does not fit in a {width}-bit register")
    return integer


def read_register(state: int, register: Iterable[QubitLike]) -> int:
    """Read an LSB-first register from an integer state mask."""

    if not isinstance(state, int) or state < 0:
        raise ValueError("state must be a non-negative integer bitmask")
    values = _normalize_register(register)
    if values and all(
        int(qubit) == int(values[0]) + offset for offset, qubit in enumerate(values)
    ):
        return (state >> _as_index(values[0])) & ((1 << len(values)) - 1)
    result = 0
    for offset, qubit in enumerate(values):
        if (state >> _as_index(qubit)) & 1:
            result |= 1 << offset
    return result


def assign_register(state: int, register: Iterable[QubitLike], value: object) -> int:
    """Return ``state`` with an LSB-first register assigned to ``value``.

    Existing bits at the register positions are cleared first.  This makes the
    helper safe for composing several named input assignments.
    """

    if not isinstance(state, int) or state < 0:
        raise ValueError("state must be a non-negative integer bitmask")
    values = _normalize_register(register)
    integer = _coerce_value(value, len(values))
    if values and all(
        int(qubit) == int(values[0]) + offset for offset, qubit in enumerate(values)
    ):
        start = _as_index(values[0])
        mask = ((1 << len(values)) - 1) << start
        return (state & ~mask) | (integer << start)
    for qubit in values:
        state &= ~(1 << _as_index(qubit))
    for offset, qubit in enumerate(values):
        if (integer >> offset) & 1:
            state |= 1 << _as_index(qubit)
    return state


def pack_register(value: object, register: Iterable[QubitLike]) -> int:
    """Pack a register value into an otherwise-zero integer bitmask."""

    return assign_register(0, register, value)


def assign_inputs(
    layout_or_circuit: CircuitLayout | Circuit,
    values: Mapping[str | int, object],
    *,
    state: int = 0,
) -> int:
    """Assign named registers (or individual integer wires) in ``state``.

    Register values may be integers or LSB-first bit iterables.  Integer keys
    are treated as individual wires and therefore accept only 0 or 1.
    """

    layout = (
        _layout_of(layout_or_circuit)
        if isinstance(layout_or_circuit, Circuit)
        else layout_or_circuit
    )
    if not isinstance(state, int) or state < 0 or state >= (1 << layout.num_qubits):
        raise ValueError("state does not fit in the layout width")
    result = state
    for name, value in values.items():
        if isinstance(name, str):
            result = assign_register(result, layout.register(name), value)
        else:
            qubit = _as_index(name)
            if qubit >= layout.num_qubits:
                raise ValueError(f"wire q[{qubit}] is outside the layout width")
            if value not in (0, 1, False, True):
                raise ValueError(f"wire q[{qubit}] accepts only a classical bit")
            result &= ~(1 << qubit)
            if int(value):
                result |= 1 << qubit
    return result


assign_registers = assign_inputs


def read_registers(
    state: int, layout_or_circuit: CircuitLayout | Circuit
) -> dict[str, int]:
    """Read every named register from a layout."""

    layout = _layout_of(layout_or_circuit)
    return {
        name: read_register(state, register)
        for name, register in layout.registers.items()
    }


def register_values(
    state: int, layout_or_circuit: CircuitLayout | Circuit
) -> dict[str, int]:
    return read_registers(state, layout_or_circuit)


def _apply_gate(state: int, gate: Gate) -> int:
    if gate.op == "X":
        return state ^ (1 << int(gate.target))
    if gate.op == "CX":
        control, target = (int(qubit) for qubit in gate.qubits)
        if (state >> control) & 1:
            state ^= 1 << target
        return state
    if gate.op in {"CCX", "MCX"}:
        controls = tuple(int(qubit) for qubit in gate.controls)
        if all((state >> control) & 1 for control in controls):
            state ^= 1 << int(gate.target)
        return state
    raise NotImplementedError(f"classical simulator does not support {gate.op}")


def apply_gate(state: int, gate: Gate) -> int:
    """Apply one supported gate to an integer state mask."""

    if not isinstance(gate, Gate):
        raise TypeError("apply_gate expects a Gate")
    if not isinstance(state, int) or state < 0:
        raise ValueError("state must be a non-negative integer bitmask")
    return _apply_gate(state, gate)


def _resolve_ancillas(
    circuit: Circuit,
    ancillas: Iterable[QubitLike] | str | None,
) -> tuple[QubitLike, ...]:
    if ancillas is None:
        return circuit.ancillas
    if isinstance(ancillas, str):
        if circuit.layout is None:
            raise ValueError("an ancilla register name requires a circuit layout")
        return circuit.layout.register(ancillas)
    return tuple(ancillas)


def simulate(
    circuit: Circuit,
    inputs: Mapping[str | int, object] | int | None = None,
    *,
    initial: int | None = None,
    initial_state: int | None = None,
    registers: Mapping[str | int, object] | None = None,
    trace: bool | list[TraceEntry] | TraceSink = False,
    check_ancillas: bool = True,
    ancillas: Iterable[QubitLike] | str | None = None,
) -> int | tuple[int, tuple[TraceEntry, ...]]:
    """Run ``circuit`` using an integer bitmask.

    With ``trace=False`` (the default), return the final integer state.  With
    ``trace=True``, return ``(state, entries)`` where each entry records the
    gate, pre-state, and post-state.  A list or callback can be supplied as a
    lightweight sink and then the final integer alone is returned.
    """

    if not isinstance(circuit, Circuit):
        raise TypeError("simulate expects a Circuit")
    if initial is not None and initial_state is not None:
        raise ValueError("initial and initial_state were both supplied")
    if isinstance(inputs, int):
        if initial is not None or initial_state is not None:
            raise ValueError(
                "an integer inputs value cannot be combined with initial state"
            )
        initial_state = inputs
        inputs = None
    if registers is not None:
        if inputs is not None:
            raise ValueError("inputs and registers were both supplied")
        inputs = registers

    state = (
        0
        if initial is None and initial_state is None
        else (initial if initial is not None else initial_state)
    )
    assert state is not None
    if not isinstance(state, int) or state < 0 or state >= (1 << circuit.num_qubits):
        raise ValueError("initial state does not fit in the circuit width")
    if inputs is not None:
        if not isinstance(inputs, Mapping):
            raise TypeError("inputs must be a register mapping or an integer state")
        if circuit.layout is None and any(isinstance(key, str) for key in inputs):
            raise ValueError("named inputs require a circuit layout")
        if circuit.layout is not None:
            state = assign_inputs(circuit, inputs, state=state)
        else:
            # The integer-wire form remains useful for anonymous fragments.
            state = assign_inputs(
                CircuitLayout({}, num_qubits=circuit.num_qubits), inputs, state=state
            )

    entries: list[TraceEntry] = []
    if trace is False:
        state = circuit.fast_simulate(state)
    else:
        for step, gate in enumerate(circuit.gates):
            before = state
            state = _apply_gate(state, gate)
            if trace is True:
                entries.append(TraceEntry(step, gate, before, state))
            elif isinstance(trace, list):
                entry = TraceEntry(step, gate, before, state)
                trace.append(entry)
            elif callable(trace):
                trace(TraceEntry(step, gate, before, state))

    declared_ancillas = _resolve_ancillas(circuit, ancillas)
    if check_ancillas:
        dirty = tuple(qubit for qubit in declared_ancillas if (state >> int(qubit)) & 1)
        if dirty:
            raise AncillaDirtyError(declared_ancillas, state)
    if trace is True:
        return state, tuple(entries)
    return state


def simulate_with_trace(
    circuit: Circuit,
    inputs: Mapping[str | int, object] | int | None = None,
    **kwargs: Any,
) -> tuple[int, tuple[TraceEntry, ...]]:
    """Convenience wrapper that always returns a trace tuple."""

    kwargs["trace"] = True
    result = simulate(circuit, inputs, **kwargs)
    assert isinstance(result, tuple)
    return result


class ClassicalSimulator:
    """Reusable façade for callers that run many assignments on one circuit."""

    def __init__(
        self,
        circuit: Circuit,
        *,
        check_ancillas: bool = True,
        ancillas: Iterable[QubitLike] | str | None = None,
    ) -> None:
        if not isinstance(circuit, Circuit):
            raise TypeError("ClassicalSimulator expects a Circuit")
        self.circuit = circuit
        self.check_ancillas = check_ancillas
        self.ancillas = ancillas

    def run(
        self,
        inputs: Mapping[str | int, object] | int | None = None,
        **kwargs: Any,
    ) -> int | tuple[int, tuple[TraceEntry, ...]]:
        kwargs.setdefault("check_ancillas", self.check_ancillas)
        if self.ancillas is not None:
            kwargs.setdefault("ancillas", self.ancillas)
        return simulate(self.circuit, inputs, **kwargs)

    simulate = run


__all__ = [
    "AncillaDirtyError",
    "ClassicalSimulator",
    "TraceEntry",
    "apply_gate",
    "assign_inputs",
    "assign_register",
    "assign_registers",
    "pack_register",
    "read_register",
    "read_registers",
    "register_values",
    "simulate",
    "simulate_with_trace",
]
