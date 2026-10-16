from itertools import product

from qcc import CircuitBuilder
from qcc.lib.adder import add_modular, add_with_carry, resource_formula
from qcc.lib.subtract import subtract_modular, subtract_with_borrow
from qcc.sim.classical import read_register, simulate


def _bits(width):
    return tuple(range(width)), tuple(range(width, 2 * width))


def test_cuccaro_counts_match_documented_construction():
    for width in range(1, 7):
        a, b = _bits(width)
        modular = CircuitBuilder(2 * width + 1)
        add_modular(modular, a, b, 2 * width)
        assert modular.build().resources()["cx"] == resource_formula(width)["cx"]
        assert modular.build().resources()["ccx"] == resource_formula(width)["ccx"]

        full = CircuitBuilder(2 * width + 2)
        add_with_carry(full, a, b, 2 * width, 2 * width + 1)
        assert full.build().resources()["cx"] == resource_formula(width, "carry")["cx"]
        assert (
            full.build().resources()["ccx"] == resource_formula(width, "carry")["ccx"]
        )


def test_modular_addition_is_exhaustive_for_widths_one_through_six():
    for width in range(1, 7):
        mask = (1 << width) - 1
        a, b = _bits(width)
        carry = 2 * width
        builder = CircuitBuilder(carry + 1)
        add_modular(builder, a, b, carry)
        circuit = builder.build()
        for av, bv in product(range(1 << width), repeat=2):
            state = av | (bv << width)
            result = simulate(circuit, state)
            assert read_register(result, a) == av
            assert read_register(result, b) == ((av + bv) & mask)
            assert ((result >> carry) & 1) == 0


def test_carry_and_borrow_variants_are_exhaustive_for_widths_one_through_six():
    for width in range(1, 7):
        mask = (1 << width) - 1
        a, b = _bits(width)
        carry_in, carry_out = 2 * width, 2 * width + 1

        add_builder = CircuitBuilder(carry_out + 1)
        add_with_carry(add_builder, a, b, carry_in, carry_out)
        add_circuit = add_builder.build()

        sub_builder = CircuitBuilder(carry_out + 1)
        subtract_with_borrow(sub_builder, a, b, carry_in, carry_out)
        sub_circuit = sub_builder.build()

        for av, bv, incoming, old_output in product(
            range(1 << width), range(1 << width), range(2), range(2)
        ):
            state = (
                av | (bv << width) | (incoming << carry_in) | (old_output << carry_out)
            )
            added = av + bv + incoming
            add_result = simulate(add_circuit, state)
            assert read_register(add_result, a) == av
            assert read_register(add_result, b) == (added & mask)
            assert ((add_result >> carry_in) & 1) == incoming
            assert ((add_result >> carry_out) & 1) == (old_output ^ (added >> width))

            difference = bv - av - incoming
            sub_result = simulate(sub_circuit, state)
            assert read_register(sub_result, a) == av
            assert read_register(sub_result, b) == (difference & mask)
            assert ((sub_result >> carry_in) & 1) == incoming
            assert ((sub_result >> carry_out) & 1) == (old_output ^ int(difference < 0))


def test_modular_subtraction_preserves_subtrahend_exhaustively():
    for width in range(1, 7):
        mask = (1 << width) - 1
        a, b = _bits(width)
        carry = 2 * width
        builder = CircuitBuilder(carry + 1)
        subtract_modular(builder, a, b, carry)
        circuit = builder.build()
        for subtrahend, target in product(range(1 << width), repeat=2):
            state = subtrahend | (target << width)
            result = simulate(circuit, state)
            assert read_register(result, a) == subtrahend
            assert read_register(result, b) == ((target - subtrahend) & mask)
            assert ((result >> carry) & 1) == 0
