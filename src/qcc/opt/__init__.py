"""Public v0.4 optimizer helpers."""

from .gates import (
    GateOptimizationStats,
    normalize_level,
    optimization_stats,
    optimize,
    optimize_circuit,
    optimize_gate_stream,
    optimize_gates,
    optimize_netlist,
    optimizer_enabled,
    peephole_optimize,
    remove_noop_pairs,
)
from .ssa import (
    constant_fold,
    dce,
    dead_value_eliminate,
    eliminate_dead_values,
    fold_constant_expressions,
    fold_constants,
    optimize_function,
    optimize_module,
    optimize_ssa,
)

__all__ = [
    "GateOptimizationStats",
    "constant_fold",
    "dce",
    "dead_value_eliminate",
    "eliminate_dead_values",
    "fold_constant_expressions",
    "fold_constants",
    "normalize_level",
    "optimization_stats",
    "optimizer_enabled",
    "optimize",
    "optimize_circuit",
    "optimize_function",
    "optimize_gate_stream",
    "optimize_gates",
    "optimize_module",
    "optimize_netlist",
    "optimize_ssa",
    "peephole_optimize",
    "remove_noop_pairs",
]
