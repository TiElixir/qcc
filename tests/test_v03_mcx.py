"""v0.3 multi-controlled-X simulation and primitive decomposition tests."""

from __future__ import annotations

from qcc import CircuitBuilder, CircuitLayout
from qcc.ir.netlist import Circuit
from qcc.sim.classical import simulate


def test_mcx_simulation_is_exhaustive_for_one_through_six_controls() -> None:
    for control_count in range(1, 7):
        target = control_count
        circuit = (
            CircuitBuilder(control_count + 1)
            .mcx(tuple(range(control_count)), target)
            .build()
        )
        for initial in range(1 << (control_count + 1)):
            controls_set = (initial & ((1 << control_count) - 1)) == (
                (1 << control_count) - 1
            )
            expected = initial ^ ((1 << target) if controls_set else 0)
            assert simulate(circuit, initial) == expected


def _decompose_mcx(circuit: Circuit) -> Circuit:
    decompose = getattr(circuit, "decompose_mcx", None)
    if not callable(decompose):
        raise AssertionError("v0.3 requires the public Circuit.decompose_mcx() API")
    result = decompose()
    assert isinstance(result, Circuit)
    return result


def test_mcx_decomposition_uses_only_primitive_gates_and_preserves_semantics() -> None:
    for control_count in range(3, 7):
        controls = tuple(range(control_count))
        target = control_count
        ancillas = tuple(range(target + 1, target + 1 + control_count - 2))
        layout = CircuitLayout(
            {"controls": controls, "target": (target,)},
            ancillas=ancillas,
            num_qubits=(target + 1 + len(ancillas)),
        )
        source = CircuitBuilder(layout).mcx(controls, target).build()
        decomposed = _decompose_mcx(source)

        assert decomposed.num_qubits == source.num_qubits
        assert decomposed.layout == source.layout
        assert all(gate.op in {"X", "CX", "CCX"} for gate in decomposed.gates)
        assert decomposed.resources()["mcx"] == 0
        assert len(decomposed) > 0

        for controls_value in range(1 << control_count):
            for target_value in (0, 1):
                initial = controls_value | (target_value << target)
                result = simulate(decomposed, initial)
                expected_target = target_value ^ int(
                    controls_value == (1 << control_count) - 1
                )
                assert (result >> target) & 1 == expected_target
                assert result & ((1 << control_count) - 1) == controls_value
                assert all(((result >> qubit) & 1) == 0 for qubit in ancillas)
