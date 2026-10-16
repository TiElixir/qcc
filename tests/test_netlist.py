import pytest
from qcc import CircuitBuilder, CircuitLayout, Gate, QubitAllocator
from qcc.sim.classical import (
    AncillaDirtyError,
    assign_inputs,
    read_register,
    simulate,
)


def test_primitive_gates_and_mcx_simulate_as_bit_masks():
    builder = CircuitBuilder(5)
    builder.x(0).cx(0, 1).ccx(0, 1, 2).mcx((0, 1, 2), 3)
    circuit = builder.build()

    assert simulate(circuit, 0) == (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3)
    assert circuit.resources().as_dict() == {
        "gates": 4,
        "qubits": 5,
        "depth": 4,
        "ancillas": 0,
        "x": 1,
        "cx": 1,
        "ccx": 1,
        "mcx": 1,
    }


def test_inverse_is_reverse_gate_order_and_composition_cleans_state():
    forward = CircuitBuilder(4).x(0).cx(0, 1).ccx(0, 1, 2).build()
    inverse = forward.inverse()

    assert inverse.gates == tuple(reversed(forward.gates))
    assert simulate(forward.compose(inverse), 0) == 0
    assert simulate(forward.compose(inverse), 0b1111) == 0b1111
    assert forward.resource_counts()["gates"] == 3
    assert forward.resource_counts().gate_count == 3


def test_layout_is_lsb_first_and_ancillas_are_checked():
    layout = CircuitLayout({"value": (0, 1, 2)}, ancillas=(3,), num_qubits=4)
    state = assign_inputs(layout, {"value": 0b101})
    assert read_register(state, layout["value"]) == 0b101

    clean = CircuitBuilder(4, layout=layout).build()
    assert simulate(clean, {"value": 0b101}) == state

    dirty = CircuitBuilder(4, layout=layout).x(3).build()
    with pytest.raises(AncillaDirtyError) as error:
        simulate(dirty)
    assert error.value.dirty == (3,)


def test_invalid_operands_and_allocator_domains_are_rejected():
    with pytest.raises(ValueError):
        Gate.ccx(0, 0, 1)

    first = QubitAllocator()
    second = QubitAllocator(start=4)
    with pytest.raises(ValueError, match="different circuit domain"):
        Gate.cx(first.allocate(), second.allocate())

    with pytest.raises(ValueError):
        CircuitBuilder(2).x(2)
