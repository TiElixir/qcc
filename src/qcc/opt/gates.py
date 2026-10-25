"""Deterministic, semantics-preserving peephole optimization for gate streams.

The v0.4 gate pass deliberately does not rename wires, allocate workspace, or
expand gates.  Every supported primitive is self-inverse, so a pair can be
removed when the gates between the pair are known to commute with it.  MCX is
kept as a hard barrier: a later backend or decomposition pass may attach
additional workspace dependencies to it, and this pass never guesses across
that boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from operator import index as _index
from typing import Any

from qcc.ir.netlist import Circuit, Gate


@dataclass(frozen=True, slots=True)
class GateOptimizationStats:
    """Counts produced by one deterministic gate optimization run."""

    level: int
    input_gates: int
    output_gates: int

    @property
    def removed_gates(self) -> int:
        """Number of source gates removed by cancellation."""

        return self.input_gates - self.output_gates

    @property
    def cancellations(self) -> int:
        """Number of canceled self-inverse pairs."""

        return self.removed_gates // 2


def normalize_level(level: int) -> int:
    """Validate and normalize a public qcc optimization level.

    Levels zero and one retain the v0.3 circuit stream.  Level two is the
    v0.4 optimizer.  Levels above two are intentionally rejected because the
    v0.4 contract has no semantics for them.
    """

    if isinstance(level, bool):
        raise TypeError("optimization level must be an integer")
    try:
        normalized = _index(level)
    except TypeError as exc:
        raise TypeError("optimization level must be an integer") from exc
    if normalized < 0 or normalized > 2:
        raise ValueError("optimization level must be between 0 and 2")
    return normalized


def optimizer_enabled(level: int) -> bool:
    """Return whether ``level`` enables the v0.4 optimizer."""

    return normalize_level(level) >= 2


def _is_mcx(gate: Gate) -> bool:
    return gate.op == "MCX"


def _commutes(first: Gate, second: Gate) -> bool:
    """Return whether two non-MCX controlled-X gates safely commute.

    A controlled-X only changes its target.  Thus two such gates commute when
    neither target is a control of the other.  This includes the useful cases
    of two gates with the same target and gates sharing controls.  MCX is
    intentionally excluded even when this local test would be sufficient: it
    is an unsafe dependency barrier for this pass.
    """

    if _is_mcx(first) or _is_mcx(second):
        return False
    return int(first.target) not in {int(qubit) for qubit in second.controls} and int(
        second.target
    ) not in {int(qubit) for qubit in first.controls}


def _cancellation_index(stream: list[Gate], gate: Gate) -> int | None:
    """Find the nearest safely commuting copy of ``gate`` in ``stream``."""

    if _is_mcx(gate):
        return None
    for index in range(len(stream) - 1, -1, -1):
        candidate = stream[index]
        if _is_mcx(candidate):
            return None
        if candidate == gate:
            between = stream[index + 1 :]
            if all(_commutes(gate, item) for item in between):
                return index
            # The nearest identical gate cannot be crossed.  An earlier copy
            # is no safer because it would have to cross this one as well.
            return None
        if not _commutes(gate, candidate):
            return None
    return None


def _known_control_state(gate: Gate, known: dict[int, bool | None]) -> bool | None:
    values = tuple(known.get(int(control)) for control in gate.controls)
    if any(value is False for value in values):
        return False
    if values and all(value is True for value in values):
        return True
    return True if not values else None


def _reduced_controlled_gate(gate: Gate, known: dict[int, bool | None]) -> Gate | None:
    """Simplify a primitive controlled-X using declared clean constants."""

    if gate.op == "MCX":
        return gate
    control_state = _known_control_state(gate, known)
    if control_state is False:
        return None
    if control_state is None:
        return gate
    dynamic_controls = tuple(
        control for control in gate.controls if known.get(int(control)) is not True
    )
    if not dynamic_controls:
        return Gate("X", (gate.target,), domain=gate.domain)
    name = {1: "CX", 2: "CCX"}.get(len(dynamic_controls))
    if name is None:
        return gate
    return Gate(name, (*dynamic_controls, gate.target), domain=gate.domain)


def _advance_known_state(gate: Gate, known: dict[int, bool | None]) -> None:
    target = int(gate.target)
    control_state = _known_control_state(gate, known)
    if control_state is False:
        return
    previous = known.get(target)
    if control_state is True and previous is not None:
        known[target] = not previous
    else:
        known[target] = None


def _constant_propagate(
    gates: Iterable[Gate], initial_zero: Iterable[int]
) -> tuple[Gate, ...]:
    """Use only declared clean wires to remove known no-op controls."""

    known: dict[int, bool | None] = {int(qubit): False for qubit in initial_zero}
    result: list[Gate] = []
    for gate in gates:
        if gate.op == "MCX":
            # MCX is a dependency barrier even when its current controls look
            # constant.  Do not carry a proof through an operation whose
            # backend decomposition may consume clean workspace.
            result.append(gate)
            known[int(gate.target)] = None
            continue
        reduced = _reduced_controlled_gate(gate, known)
        if reduced is None:
            continue
        result.append(reduced)
        _advance_known_state(reduced, known)
    return tuple(result)


def _optimize_once(gates: Iterable[Gate]) -> tuple[Gate, ...]:
    result: list[Gate] = []
    for gate in gates:
        cancellation = _cancellation_index(result, gate)
        if cancellation is None:
            result.append(gate)
        else:
            del result[cancellation]
    return tuple(result)


def optimize_gates(
    gates: Iterable[Gate],
    level: int = 2,
    *,
    initial_zero: Iterable[int] = (),
    opt: int | None = None,
) -> tuple[Gate, ...]:
    """Optimize a gate iterable without changing its wire layout.

    The pass only removes pairs or simplifies gates controlled by a declared
    clean wire; it never reorders the surviving gates.  A fixed point is
    reached after a finite number of passes because every successful rewrite
    removes two gates or leaves the stream shorter.  Level zero and one are
    exact identity modes for compatibility with the v0.3 API.
    """

    normalized = normalize_level(level if opt is None else opt)
    original = tuple(gates)
    if normalized < 2:
        return original

    current = _constant_propagate(original, initial_zero)
    while True:
        optimized = _optimize_once(current)
        if len(optimized) == len(current):
            return optimized
        current = optimized


def optimize_circuit(
    circuit: Circuit, level: int = 2, *, opt: int | None = None
) -> Circuit:
    """Return an optimized circuit with the original layout and wire domain."""

    if not isinstance(circuit, Circuit):
        raise TypeError("optimize_circuit expects a qcc Circuit")
    normalized = normalize_level(level if opt is None else opt)
    if normalized < 2:
        return circuit
    gates = optimize_gates(
        circuit.gates,
        normalized,
        initial_zero=(int(qubit) for qubit in circuit.ancillas),
    )
    return Circuit(
        gates,
        num_qubits=circuit.num_qubits,
        layout=circuit.layout,
        domain=circuit.domain,
        name=circuit.name,
        optimization_level=normalized,
        requested_optimization_level=normalized,
        baseline=circuit,
    )


def optimization_stats(
    circuit: Circuit, level: int = 2, *, opt: int | None = None
) -> GateOptimizationStats:
    """Return stable before/after counts for a circuit optimization."""

    if not isinstance(circuit, Circuit):
        raise TypeError("optimization_stats expects a qcc Circuit")
    normalized = normalize_level(level if opt is None else opt)
    optimized = optimize_circuit(circuit, normalized)
    return GateOptimizationStats(normalized, len(circuit.gates), len(optimized.gates))


def optimize(
    value: Circuit | Iterable[Gate],
    opt: int = 2,
    *,
    level: int | None = None,
) -> Any:
    """Optimize either a qcc circuit or a standalone gate iterable.

    ``level`` is an explicit keyword alias for callers that use the term
    ``level``; when supplied it takes precedence over ``opt``.
    """

    selected = opt if level is None else level
    if isinstance(value, Circuit):
        return optimize_circuit(value, selected)
    return optimize_gates(value, selected)


# Descriptive aliases used by small integrations.
peephole_optimize = optimize_gates
optimize_gate_stream = optimize_gates
optimize_netlist = optimize_circuit
remove_noop_pairs = optimize_gates


__all__ = [
    "GateOptimizationStats",
    "normalize_level",
    "optimizer_enabled",
    "optimization_stats",
    "optimize",
    "optimize_circuit",
    "optimize_gate_stream",
    "optimize_gates",
    "optimize_netlist",
    "peephole_optimize",
    "remove_noop_pairs",
]
