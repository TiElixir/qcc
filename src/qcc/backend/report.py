"""Small resource reports for backend-neutral or netlist circuits."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceReport:
    """Gate and qubit counts for a reversible circuit."""

    qubits: int
    ancillas: int
    gates: int
    x: int
    cnot: int
    toffoli: int
    depth: int

    @property
    def num_qubits(self) -> int:
        return self.qubits

    @property
    def gate_count(self) -> int:
        return self.gates

    @property
    def ancilla_count(self) -> int:
        return self.ancillas

    @property
    def x_count(self) -> int:
        return self.x

    @property
    def cx(self) -> int:
        return self.cnot

    @property
    def cx_count(self) -> int:
        return self.cnot

    @property
    def ccx(self) -> int:
        return self.toffoli

    @property
    def ccx_count(self) -> int:
        return self.toffoli

    @property
    def counts(self) -> Mapping[str, int]:
        return {"x": self.x, "cx": self.cnot, "ccx": self.toffoli}

    @property
    def gate_counts(self) -> Mapping[str, int]:
        return self.counts

    def as_dict(self) -> dict[str, int]:
        return {
            "qubits": self.qubits,
            "ancillas": self.ancillas,
            "gates": self.gates,
            "x": self.x,
            "cx": self.cnot,
            "ccx": self.toffoli,
            "depth": self.depth,
        }


def _attribute(value: Any, names: tuple[str, ...], default: Any = None) -> Any:
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


def _gates(circuit: Any) -> tuple[Any, ...]:
    gates = _attribute(circuit, ("gates", "operations", "ops"), ())
    if callable(gates):
        gates = gates()
    return tuple(gates or ())


def _gate_name(gate: Any) -> str:
    name = _attribute(gate, ("name", "opcode", "operation", "kind", "gate"), gate)
    if isinstance(name, str):
        normalized = name.lower().replace("cnot", "cx").replace("toffoli", "ccx")
        return normalized
    return str(name).lower()


def _gate_qubits(gate: Any) -> tuple[int, ...]:
    qubits = _attribute(gate, ("qubits", "wires", "bits"), None)
    if qubits is not None:
        return tuple(int(qubit) for qubit in qubits)
    controls = _attribute(gate, ("controls",), ())
    target = _attribute(gate, ("target",), None)
    return tuple(int(qubit) for qubit in controls) + (
        (int(target),) if target is not None else ()
    )


def _num_qubits(circuit: Any, gates: tuple[Any, ...]) -> int:
    value = _attribute(
        circuit, ("num_qubits", "qubit_count", "n_qubits", "width"), None
    )
    if callable(value):
        value = value()
    if value is not None:
        return int(value)
    return (
        max((qubit for gate in gates for qubit in _gate_qubits(gate)), default=-1) + 1
    )


def _ancilla_count(circuit: Any) -> int:
    layout = _attribute(circuit, ("layout",), None)
    ancillas = _attribute(layout, ("ancillas", "ancilla_bits", "workspace"), ())
    return len(tuple(ancillas or ()))


def _depth(gates: Iterable[Any]) -> int:
    last_layer: dict[int, int] = {}
    depth = 0
    for gate in gates:
        qubits = _gate_qubits(gate)
        layer = max((last_layer.get(qubit, 0) for qubit in qubits), default=0) + 1
        for qubit in qubits:
            last_layer[qubit] = layer
        depth = max(depth, layer)
    return depth


def resource_report(circuit: Any) -> ResourceReport:
    """Return resource counts without requiring a specific netlist class."""

    gates = _gates(circuit)
    counts = {"x": 0, "cx": 0, "ccx": 0}
    for gate in gates:
        name = _gate_name(gate)
        if name in counts:
            counts[name] += 1
    return ResourceReport(
        qubits=_num_qubits(circuit, gates),
        ancillas=_ancilla_count(circuit),
        gates=len(gates),
        x=counts["x"],
        cnot=counts["cx"],
        toffoli=counts["ccx"],
        depth=_depth(gates),
    )


report_resources = resource_report
report = resource_report


__all__ = ["ResourceReport", "report", "report_resources", "resource_report"]
