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
    optimizer: int | None = None
    requested_level: int | None = None
    effective_level: int | None = None
    fallback_reason: str | None = None
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        estimate = self.toffoli if self.toffoli_estimate < 0 else self.toffoli_estimate
        t_count = 7 * estimate if self.t_count < 0 else self.t_count
        if estimate < 0 or t_count < 0:
            raise ValueError("resource estimates cannot be negative")
        if self.optimizer is not None and (
            isinstance(self.optimizer, bool)
            or not isinstance(self.optimizer, int)
            or self.optimizer < 0
        ):
            raise ValueError("optimizer level must be a non-negative integer")
        for name, level in (
            ("requested_level", self.requested_level),
            ("effective_level", self.effective_level),
        ):
            if level is not None and (
                isinstance(level, bool) or not isinstance(level, int) or level < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.effective_level is None and self.optimizer is not None:
            object.__setattr__(self, "effective_level", self.optimizer)
        object.__setattr__(self, "toffoli_estimate", estimate)
        object.__setattr__(self, "t_count", t_count)

    @property
    def optimization_level(self) -> int | None:
        """The optimizer level recorded on the source circuit, if any."""

        return self.optimizer

    @property
    def requested_optimization_level(self) -> int | None:
        """The level requested by the compiler, if this is a compilation report."""

        return self.requested_level

    @property
    def effective_optimization_level(self) -> int | None:
        """The level represented by the selected/after circuit."""

        return self.effective_level

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

    def as_dict(self) -> dict[str, Any]:
        legacy: dict[str, Any] = {
            "qubits": self.qubits,
            "ancillas": self.ancillas,
            "gates": self.gates,
            "x": self.x,
            "cx": self.cnot,
            "ccx": self.toffoli,
            "depth": self.depth,
        }
        if self.mcx or self.optimizer is not None or self.requested_level is not None:
            legacy.update(
                {
                    "mcx": self.mcx,
                    "mcx_cx": self.mcx_cnot,
                    "primitive_gates": self.primitive_gate_count,
                    "toffoli_estimate": self.toffoli_estimate,
                    "t_count": self.t_count,
                }
            )
        if self.optimizer is not None:
            legacy["optimizer"] = self.optimizer
        if self.requested_level is not None:
            legacy["requested_level"] = self.requested_level
        if self.effective_level is not None:
            legacy["effective_level"] = self.effective_level
        if self.fallback_reason is not None:
            legacy["fallback_reason"] = self.fallback_reason
        if self.before is not None:
            legacy["before"] = dict(self.before)
        if self.after is not None:
            legacy["after"] = dict(self.after)
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


def _optimizer_level(circuit: Any) -> int | None:
    value = _attribute(circuit, ("optimization_level", "optimizer", "opt_level"), None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


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


def _requested_level(circuit: Any) -> int | None:
    value = _attribute(
        circuit,
        ("requested_optimization_level", "requested_level"),
        None,
    )
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _primitive_snapshot(circuit: Any, logical: ResourceReport) -> dict[str, Any]:
    primitive = circuit
    decompose = getattr(circuit, "decompose_mcx", None)
    if callable(decompose) and logical.mcx:
        try:
            primitive = decompose()
        except (TypeError, ValueError):
            primitive = None

    if primitive is not None and primitive is not circuit:
        gates = _gates(primitive)
        counts = {"x": 0, "cx": 0, "ccx": 0}
        for gate in gates:
            name = _gate_name(gate)
            if name in counts:
                counts[name] += 1
        primitive_depth = _depth(gates)
        primitive_gate_count = len(gates)
        primitive_t_count = 7 * counts["ccx"]
    else:
        counts = {
            "x": logical.x,
            "cx": logical.cnot + logical.mcx_cnot,
            "ccx": logical.toffoli_estimate,
        }
        primitive_depth = logical.depth
        primitive_gate_count = logical.primitive_gate_count
        primitive_t_count = logical.t_count

    return {
        "qubit_count": logical.qubits,
        "ancillas": logical.ancillas,
        "gate_count": primitive_gate_count,
        "gate_counts": counts,
        "depth": primitive_depth,
        "t_count": primitive_t_count,
    }


def resource_report(
    circuit: Any,
    *,
    optimizer: int | None = None,
    requested_level: int | None = None,
    before: Any | None = None,
    fallback_reason: str | None = None,
    _include_metadata: bool = True,
) -> ResourceReport:
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
    effective_level = _optimizer_level(circuit) if optimizer is None else optimizer
    requested = (
        _requested_level(circuit) if requested_level is None else requested_level
    )
    if requested is None and optimizer is not None:
        requested = optimizer
    result = ResourceReport(
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
        optimizer=effective_level,
        requested_level=requested,
        effective_level=effective_level,
        fallback_reason=fallback_reason,
    )
    if not _include_metadata:
        return result

    if requested is not None:
        baseline_circuit = circuit if before is None else before
        baseline = (
            result
            if before is None
            else resource_report(before, _include_metadata=False)
        )
        object.__setattr__(
            result, "before", _primitive_snapshot(baseline_circuit, baseline)
        )
        object.__setattr__(result, "after", _primitive_snapshot(circuit, result))
    return result


report_resources = resource_report
report = resource_report


__all__ = ["ResourceReport", "report", "report_resources", "resource_report"]
