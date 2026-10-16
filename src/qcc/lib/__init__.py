"""Reversible arithmetic circuit fragments."""

from qcc.lib.adder import (
    add_fragment_with_carry,
    add_modular,
    add_modular_into,
    add_with_carry,
    controlled_xor,
    copy_register,
    cuccaro_adder,
    majority,
    modular_adder,
    modular_adder_into,
    resource_formula,
    unmajority_add,
)
from qcc.lib.subtract import (
    complement_register,
    modular_subtractor,
    subtract_into,
    subtract_modular,
    subtract_with_borrow,
)

__all__ = [
    "add_fragment_with_carry",
    "add_modular",
    "add_modular_into",
    "add_with_carry",
    "complement_register",
    "controlled_xor",
    "copy_register",
    "cuccaro_adder",
    "majority",
    "modular_adder",
    "modular_adder_into",
    "modular_subtractor",
    "resource_formula",
    "subtract_into",
    "subtract_modular",
    "subtract_with_borrow",
    "unmajority_add",
]
