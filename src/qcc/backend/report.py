"""Small resource reports for backend-neutral or netlist circuits."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResourceReport:
    """Stable resource counts and Clifford+T estimates for a circuit.

    ``gates`` and the primitive gate counters describe the source netlist and
    therefore retain the v0.2 API.  ``toffoli_estimate`` and
    ``primitive_gates`` additionally account for first-class MCX operations
    using the backend's clean-ancilla ladder: a gate with ``k`` controls costs
    one CX for ``k == 1``, one CCX for ``k == 2``, and ``2*k - 3`` CCX gates
    otherwise.  A standard exact CCX decomposition contributes seven T gates,
    so ``t_count`` is seven times that Toffoli estimate.
    """

    qubits: int
    ancillas: int
    gates: int
    x: int
    cnot: int
    toffoli: int
    depth: int
    t_count: int = -1
    toffoli_estimate: int = -1
    mcx: int = 0
    mcx_cnot: int = 0

    def __post_init__(self) -> None:
        estimate = self.toffoli if self.toffoli_estimate < 0 else self.toffoli_estimate
        t_count = 7 * estimate if self.t_count < 0 else self.t_count
        if estimate < 0 or t_count < 0:
            raise ValueError("resource estimates cannot be negative")
        object.__setattr__(self, "toffoli_estimate", estimate)
        object.__setattr__(self, "t_count", t_count)

    @property
    def num_qubits(self) -> int:
        return self.qubits

    @property
    def qubit_count(self) -> int:
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
    def toffoli_count(self) -> int:
        return self.toffoli

    @property
    def mcx_count(self) -> int:
        return self.mcx

    @property
    def mcx_cx_count(self) -> int:
        return self.mcx_cnot

    @property
    def primitive_gate_count(self) -> int:
        """Gate count after clean-ancilla MCX expansion."""

        return self.x + self.cnot + self.toffoli_estimate + self.mcx_cnot

    @property
    def estimated_t_count(self) -> int:
        return self.t_count

    @property
    def t(self) -> int:
        return self.t_count

    @property
    def counts(self) -> Mapping[str, int]:
        counts = {"x": self.x, "cx": self.cnot, "ccx": self.toffoli}
        if self.mcx:
            counts["mcx"] = self.mcx
        return counts

    @property
    def gate_counts(self) -> Mapping[str, int]:
        return self.counts

    @property
    def estimates(self) -> Mapping[str, int]:
        return {
            "qubits": self.qubits,
            "gates": self.gates,
            "primitive_gates": self.primitive_gate_count,
            "toffoli": self.toffoli_estimate,
            "depth": self.depth,
            "t_count": self.t_count,
        }

    def as_dict(self) -> dict[str, int]:
        legacy = {
            "qubits": self.qubits,
            "ancillas": self.ancillas,
            "gates": self.gates,
            "x": self.x,
            "cx": self.cnot,
            "ccx": self.toffoli,
            "depth": self.depth,
        }
        if self.mcx:
            legacy.update(
                {
                    "mcx": self.mcx,
                    "mcx_cx": self.mcx_cnot,
                    "primitive_gates": self.primitive_gate_count,
                    "toffoli_estimate": self.toffoli_estimate,
                    "t_count": self.t_count,
                }
            )
        return legacy


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


def _mcx_toffoli_cost(gate: Any) -> int:
    controls = len(_gate_qubits(gate)) - 1
    if controls <= 2:
        return 1 if controls == 2 else 0
    return 2 * controls - 3


def _num_qubits(circuit: Any, gates: tuple[Any, ...]) -> int:
    value = _attribute(
        circuit, ("num_qubits", "qubit_count", "n_qubits", "width"), None
    )
    if callable(value):
        value = value()
    if value is not None:
        return int(value)
    layout = _attribute(circuit, ("layout", "circuit_layout"), None)
    layout_width = _attribute(
        layout, ("num_qubits", "n_qubits", "total_qubits", "width"), None
    )
    if layout_width is not None:
        return int(layout_width)
    registers = _attribute(layout, ("registers", "quantum_registers", "qregs"), None)
    if isinstance(registers, Mapping):
        numeric: list[int] = []
        all_sizes = True
        for item in registers.values():
            if isinstance(item, int) and not isinstance(item, bool):
                numeric.append(item)
                continue
            all_sizes = False
            bits = _attribute(item, ("bits", "qubits", "values"), item)
            try:
                numeric.extend(int(bit) for bit in bits)
            except (TypeError, ValueError):
                continue
        if numeric:
            return sum(numeric) if all_sizes else max(numeric) + 1
    return (
        max((qubit for gate in gates for qubit in _gate_qubits(gate)), default=-1) + 1
    )


def _ancilla_count(circuit: Any) -> int:
    layout = _attribute(circuit, ("layout",), None)
    ancillas = _attribute(layout, ("ancillas", "ancilla_bits", "workspace"), ())
    values = tuple(ancillas or ())
    if values:
        return len(values)
    registers = _attribute(layout, ("registers", "quantum_registers", "qregs"), None)
    if isinstance(registers, Mapping):
        count = 0
        for name, item in registers.items():
            normalized = str(name).lower().replace("-", "_")
            if not normalized.startswith(("ancilla", "workspace", "scratch")):
                continue
            if isinstance(item, int) and not isinstance(item, bool):
                count += item
                continue
            bits = _attribute(item, ("bits", "qubits", "values"), item)
            try:
                count += len(tuple(bits))
            except TypeError:
                continue
        return count
    return 0


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
    mcx_count = 0
    mcx_toffolis = 0
    mcx_cnot = 0
    for gate in gates:
        name = _gate_name(gate)
        if name in counts:
            counts[name] += 1
        elif name == "mcx":
            mcx_count += 1
            controls = len(_gate_qubits(gate)) - 1
            if controls == 1:
                mcx_cnot += 1
            else:
                mcx_toffolis += _mcx_toffoli_cost(gate)
    toffoli_estimate = counts["ccx"] + mcx_toffolis
    return ResourceReport(
        qubits=_num_qubits(circuit, gates),
        ancillas=_ancilla_count(circuit),
        gates=len(gates),
        x=counts["x"],
        cnot=counts["cx"],
        toffoli=counts["ccx"],
        depth=_depth(gates),
        t_count=7 * toffoli_estimate,
        toffoli_estimate=toffoli_estimate,
        mcx=mcx_count,
        mcx_cnot=mcx_cnot,
    )


report_resources = resource_report
report = resource_report


__all__ = ["ResourceReport", "report", "report_resources", "resource_report"]
