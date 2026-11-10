"""Dataflow-driven circuit synthesis (optimization level 3).

:func:`synthesize_function` is the whole pipeline: an SSA function becomes one
expression graph (:mod:`qcc.synth.dag`), and the graph becomes gates
(:mod:`qcc.synth.lower`).
"""

from __future__ import annotations

from qcc.ir.ssa import Function
from qcc.lower.ssa_to_rev import LoweredCircuit

from .dag import Dag, Graph, Node, build_dag
from .lower import synthesize


def synthesize_function(function: Function) -> LoweredCircuit:
    """Compile one legalized SSA function to an X/CX/CCX circuit."""

    return synthesize(build_dag(function))


__all__ = [
    "Dag",
    "Graph",
    "Node",
    "build_dag",
    "synthesize",
    "synthesize_function",
]
