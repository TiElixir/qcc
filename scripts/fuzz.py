"""Differential fuzzer for the qcc circuit synthesizer.

Each iteration builds a random SSA function (arithmetic, comparisons, casts,
constant division, and nested if/else with early returns) over a few narrow
integer arguments, compiles it, and checks the circuit against the reference
evaluator on *every* input: the output must match, the inputs must survive,
and every work wire must return to zero.  Narrow arguments keep the sweep
exhaustive, so a failure is a concrete counterexample rather than a hunch.

    python scripts/fuzz.py --count 2000 --seed 1

A failing function is printed in LLVM-like form together with the offending
input, and the process exits with status 1.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass

import qcc
from qcc.ir.netlist import Circuit
from qcc.ir.ssa import (
    BasicBlock,
    Function,
    Instruction,
    IntConstant,
    IntType,
    Operand,
    Parameter,
    PhiIncoming,
    SSAValue,
)
from qcc.verify import VerificationResult, evaluate, verify

_WIDTHS = (1, 2, 3, 4, 5, 6, 8)
_PREDICATES = ("eq", "ne", "ugt", "uge", "ult", "ule", "sgt", "sge", "slt", "sle")
_BINARY = ("add", "sub", "mul", "and", "or", "xor")
_KINDS = (
    *_BINARY,
    *_BINARY,
    "shift",
    "cast",
    "cast",
    "icmp",
    "icmp",
    "select",
    "select",
    "divide",
)


class _Generator:
    """Build one random function, keeping every value well typed.

    Control flow is structured: a branch opens a ``then`` block and usually an
    ``else`` block, either of which may return early, and the surviving paths
    meet in a merge block whose phis choose among values visible on each
    side.  A value defined inside a branch is never used outside it, so the
    result is valid SSA by construction.
    """

    def __init__(
        self, generator: random.Random, max_input_bits: int, branch_chance: float
    ) -> None:
        self.random = generator
        self.branch_chance = branch_chance
        self.counter = 0
        self.blocks: list[tuple[str, list[Instruction]]] = [("entry", [])]
        self.visible: list[SSAValue] = []
        self.parameters: list[Parameter] = []
        remaining = max_input_bits
        for index in range(generator.randint(1, 3)):
            width = generator.choice([w for w in _WIDTHS if w <= remaining] or [1])
            if width > remaining:
                break
            parameter = Parameter(f"p{index}", IntType(width))
            self.parameters.append(parameter)
            self.visible.append(parameter)
            remaining -= width
        self.return_type = IntType(generator.choice(_WIDTHS))

    @property
    def _block(self) -> str:
        return self.blocks[-1][0]

    def _open(self, prefix: str) -> str:
        self.counter += 1
        name = f"{prefix}{self.counter}"
        self.blocks.append((name, []))
        return name

    def _append(self, instruction: Instruction) -> None:
        self.blocks[-1][1].append(instruction)

    def _emit(
        self,
        opcode: str,
        width: int,
        operands: tuple[Operand, ...],
        *,
        predicate: str | None = None,
        operand_type: IntType | None = None,
    ) -> SSAValue:
        self.counter += 1
        result = SSAValue(f"v{self.counter}", IntType(width))
        self._append(
            Instruction(
                opcode,
                result=result,
                type=IntType(width),
                operands=operands,
                predicate=predicate,
                operand_type=operand_type,
            )
        )
        self.visible.append(result)
        return result

    def _convert(self, value: SSAValue, width: int) -> SSAValue:
        source = value.type.width
        if source == width:
            return value
        opcode = "trunc" if source > width else self.random.choice(("zext", "sext"))
        return self._emit(opcode, width, (value,), operand_type=value.type)

    def _value(self, width: int) -> SSAValue:
        """Return a visible value of ``width`` bits, converting one if needed."""

        matching = [value for value in self.visible if value.type.width == width]
        if matching and self.random.random() < 0.8:
            return self.random.choice(matching)
        return self._convert(self.random.choice(self.visible), width)

    def _operand(self, width: int) -> Operand:
        if self.random.random() < 0.2:
            return IntConstant(IntType(width), self.random.getrandbits(width))
        return self._value(width)

    def _step(self) -> None:
        kind = self.random.choice(_KINDS)
        width = self.random.choice(_WIDTHS)
        integer = IntType(width)
        if kind in _BINARY:
            self._emit(kind, width, (self._value(width), self._operand(width)))
        elif kind == "shift":
            opcode = self.random.choice(("shl", "lshr", "ashr"))
            amount = IntConstant(integer, self.random.randrange(width))
            self._emit(opcode, width, (self._value(width), amount))
        elif kind == "cast":
            self._convert(self.random.choice(self.visible), width)
        elif kind == "icmp":
            self._emit(
                "icmp",
                1,
                (self._value(width), self._operand(width)),
                predicate=self.random.choice(_PREDICATES),
                operand_type=integer,
            )
        elif kind == "select":
            self._emit(
                "select",
                width,
                (self._value(1), self._operand(width), self._operand(width)),
            )
        else:
            divisor = IntConstant(integer, self.random.randrange(1, 1 << width))
            opcode = self.random.choice(("udiv", "urem"))
            self._emit(opcode, width, (self._value(width), divisor))

    def _return(self) -> None:
        returned = self._value(self.return_type.width)
        self._append(Instruction("ret", type=self.return_type, operands=(returned,)))

    def _arm(
        self, name: str, outer: list[SSAValue], depth: int, merge: str
    ) -> tuple[str, list[SSAValue]] | None:
        """Fill one branch arm; ``None`` means every path in it returned."""

        self.blocks.append((name, []))
        self.visible = list(outer)
        alive = self._region(depth + 1, self.random.randint(0, 4))
        if alive and self.random.random() < 0.15:
            self._return()
            alive = False
        if not alive:
            return None
        end = self._block
        self._append(Instruction("br", targets=(merge,)))
        return end, self.visible

    def _phi_value(self, visible: list[SSAValue], integer: IntType) -> Operand:
        matching = [value for value in visible if value.type == integer]
        if matching and self.random.random() < 0.85:
            return self.random.choice(matching)
        return IntConstant(integer, self.random.getrandbits(integer.width))

    def _branch(self, depth: int) -> bool:
        """Emit an if/else; returns whether control continues afterwards."""

        condition = self._value(1)
        outer = list(self.visible)
        source = self._block
        self.counter += 1
        label = self.counter
        then_name, else_name, merge = f"then{label}", f"else{label}", f"merge{label}"
        has_else = self.random.random() < 0.7
        self._append(
            Instruction(
                "br",
                type=IntType(1),
                operands=(condition,),
                targets=(then_name, else_name if has_else else merge),
            )
        )
        arrivals: list[tuple[str, list[SSAValue]]] = []
        then_arm = self._arm(then_name, outer, depth, merge)
        if then_arm is not None:
            arrivals.append(then_arm)
        if has_else:
            else_arm = self._arm(else_name, outer, depth, merge)
            if else_arm is not None:
                arrivals.append(else_arm)
        else:
            arrivals.append((source, outer))
        if not arrivals:
            return False
        self.blocks.append((merge, []))
        self.visible = list(outer)
        if len(arrivals) == 2:
            for _ in range(self.random.randint(1, 3)):
                integer = IntType(self.random.choice(_WIDTHS))
                edges = tuple(
                    PhiIncoming(self._phi_value(visible, integer), block)
                    for block, visible in arrivals
                )
                self.counter += 1
                result = SSAValue(f"v{self.counter}", integer)
                self._append(
                    Instruction(
                        "phi",
                        result=result,
                        type=integer,
                        operands=tuple(edge.value for edge in edges),
                        incoming=edges,
                    )
                )
                self.visible.append(result)
        return True

    def _region(self, depth: int, steps: int) -> bool:
        for _ in range(steps):
            if depth < 2 and self.random.random() < self.branch_chance:
                if not self._branch(depth):
                    return False
            else:
                self._step()
        return True

    def build(self, name: str, steps: int) -> Function:
        if self._region(0, steps):
            self._return()
        return Function(
            name,
            self.return_type,
            tuple(self.parameters),
            tuple(
                BasicBlock(block, tuple(instructions))
                for block, instructions in self.blocks
            ),
        )


def random_function(
    seed: int,
    *,
    max_steps: int = 12,
    max_input_bits: int = 10,
    branch_chance: float = 0.15,
) -> Function:
    """Return the deterministic random function for ``seed``.

    ``branch_chance`` is the probability that a step opens an ``if``; zero
    produces straight-line functions only.
    """

    generator = random.Random(seed)
    builder = _Generator(generator, max_input_bits, branch_chance)
    return builder.build(f"fuzz{seed}", generator.randint(1, max_steps))


@dataclass(frozen=True, slots=True)
class FuzzFailure:
    """A seed whose circuit broke the contract, or failed to compile."""

    seed: int
    function: Function
    detail: str


def check_seed(
    seed: int,
    *,
    opt: int = 3,
    max_steps: int = 12,
    max_input_bits: int = 10,
    branch_chance: float = 0.15,
    compile_function: Callable[[Function, int], Circuit] | None = None,
) -> FuzzFailure | None:
    """Compile and exhaustively verify the function for one seed."""

    function = random_function(
        seed,
        max_steps=max_steps,
        max_input_bits=max_input_bits,
        branch_chance=branch_chance,
    )
    compiler = compile_function or (
        lambda target, level: qcc.compile_c(target, function=target.name, opt=level)
    )
    try:
        circuit = compiler(function, opt)
        result: VerificationResult = verify(circuit, function)
    except Exception as error:  # noqa: BLE001 - any crash is a finding
        return FuzzFailure(seed, function, f"{type(error).__name__}: {error}")
    if result.ok:
        return None
    assert result.counterexample is not None
    inputs = result.counterexample.inputs
    return FuzzFailure(
        seed,
        function,
        f"{result.describe()} (reference: {evaluate(function, inputs)})",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=500, help="functions to try")
    parser.add_argument("--seed", type=int, default=0, help="first seed")
    parser.add_argument("--opt", type=int, default=3, help="optimization level")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-input-bits", type=int, default=10)
    parser.add_argument(
        "--branch-chance",
        type=float,
        default=0.15,
        help="probability that a step opens an if/else (0 = straight-line)",
    )
    arguments = parser.parse_args(argv)

    for seed in range(arguments.seed, arguments.seed + arguments.count):
        failure = check_seed(
            seed,
            opt=arguments.opt,
            max_steps=arguments.max_steps,
            max_input_bits=arguments.max_input_bits,
            branch_chance=arguments.branch_chance,
        )
        if failure is not None:
            print(f"seed {failure.seed} failed: {failure.detail}", file=sys.stderr)
            print(failure.function, file=sys.stderr)
            return 1
    print(
        f"{arguments.count} random functions verified exhaustively at "
        f"O{arguments.opt} (seeds {arguments.seed}.."
        f"{arguments.seed + arguments.count - 1})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
