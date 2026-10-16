"""Core reversible-circuit building blocks for qcc.

Frontend, lowering, command-line, and public compilation APIs live in other
subsystems.  This package currently exposes only the circuit IR, simulator,
and arithmetic helpers.
"""

from qcc.ir.netlist import (
    Circuit,
    CircuitBuilder,
    CircuitLayout,
    Gate,
    Qubit,
    QubitAllocator,
    ResourceCounts,
)

__all__ = [
    "Circuit",
    "CircuitBuilder",
    "CircuitLayout",
    "Gate",
    "Qubit",
    "QubitAllocator",
    "ResourceCounts",
]
