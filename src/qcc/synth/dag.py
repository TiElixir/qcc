"""Turn an SSA function into one pure dataflow graph.

The supported subset has no side effects, no traps, and no loops, so a whole
function is just an expression over its arguments.  This module makes that
literal: every value becomes a node of a hash-consed graph, and control flow
disappears.  A ``phi`` turns into a tree of ``select`` nodes that mirrors the
branches leading to it ("if-conversion"), so both arms of an ``if`` are simply
two subexpressions and each branch condition is used exactly where the source
tested it.

Building the graph also normalizes it.  Constants fold, algebraic identities
collapse, operands of commutative operations are ordered, and the ten ``icmp``
predicates reduce to two (``eq`` and unsigned ``ult``) plus free bit flips, so
equal computations share one node however the source spelled them.
"""

from __future__ import annotations

from dataclasses import dataclass

from qcc.errors import SourceLocation, UnsupportedConstructError
from qcc.ir.ssa import Function, Instruction, IntConstant, IntType, Operand, SSAValue

_COMMUTATIVE = frozenset({"add", "mul", "and", "or", "xor", "eq"})
_EXIT = "<exit>"


@dataclass(frozen=True, slots=True)
class Node:
    """One value of the graph.

    ``value`` holds a constant's bits, a shift amount, or an argument's
    position; ``name`` holds an argument's name.
    """

    op: str
    width: int
    args: tuple[int, ...] = ()
    value: int = 0
    name: str = ""


class Graph:
    """Hash-consed expression graph; operands always precede their users."""

    def __init__(self) -> None:
        self.nodes: list[Node] = []
        self._ids: dict[Node, int] = {}

    def _intern(self, node: Node) -> int:
        existing = self._ids.get(node)
        if existing is not None:
            return existing
        identifier = len(self.nodes)
        self.nodes.append(node)
        self._ids[node] = identifier
        return identifier

    def const(self, value: int, width: int) -> int:
        return self._intern(Node("const", width, value=value & ((1 << width) - 1)))

    def arg(self, name: str, width: int, index: int) -> int:
        return self._intern(Node("arg", width, value=index, name=name))

    def constant_value(self, node: int) -> int | None:
        entry = self.nodes[node]
        return entry.value if entry.op == "const" else None

    def width(self, node: int) -> int:
        return self.nodes[node].width

    def invert(self, node: int) -> int:
        """Bitwise NOT, kept as XOR with all ones so it lowers to bit flips."""

        width = self.width(node)
        return self.binary("xor", node, self.const((1 << width) - 1, width))

    def _inverted(self, node: int) -> int | None:
        """Return ``x`` when ``node`` is ``invert(x)``."""

        entry = self.nodes[node]
        if entry.op != "xor":
            return None
        mask = (1 << entry.width) - 1
        if self.constant_value(entry.args[1]) == mask:
            return entry.args[0]
        return None

    def binary(
        self, op: str, first: int, second: int, location: SourceLocation | None = None
    ) -> int:
        width = self.width(first)
        mask = (1 << width) - 1
        left = self.constant_value(first)
        right = self.constant_value(second)
        if left is not None and right is not None:
            if op in {"udiv", "urem"} and right == 0:
                raise UnsupportedConstructError(
                    "division by zero",
                    "division or remainder by zero is undefined and cannot be "
                    "compiled; a zero divisor has no result to compute",
                    location,
                )
            folded = {
                "add": lambda: left + right,
                "sub": lambda: left - right,
                "mul": lambda: left * right,
                "and": lambda: left & right,
                "or": lambda: left | right,
                "xor": lambda: left ^ right,
                "udiv": lambda: left // right,
                "urem": lambda: left % right,
            }[op]()
            return self.const(folded, width)
        if op in _COMMUTATIVE and (
            left is not None or (right is None and first > second)
        ):
            first, second = second, first
            left, right = right, left

        same = first == second
        if op == "add":
            if right == 0:
                return first
        elif op == "sub":
            if right == 0:
                return first
            if same:
                return self.const(0, width)
            if right is not None:
                return self.binary("add", first, self.const(-right, width))
        elif op == "mul":
            if right == 0:
                return self.const(0, width)
            if right == 1:
                return first
            if right is not None and right & (right - 1) == 0:
                return self.shift("shl", first, right.bit_length() - 1)
        elif op == "and":
            if right == 0:
                return self.const(0, width)
            if right == mask or same:
                return first
            if self._inverted(first) == second or self._inverted(second) == first:
                return self.const(0, width)
        elif op == "or":
            if right == 0 or same:
                return first
            if right == mask:
                return self.const(mask, width)
            if self._inverted(first) == second or self._inverted(second) == first:
                return self.const(mask, width)
            resolved = self._resolve(first, second)
            if resolved is not None:
                return resolved
        elif op == "xor":
            if right == 0:
                return first
            if same:
                return self.const(0, width)
            inner = self._inverted(first)
            if right == mask and inner is not None:
                return inner
        elif op in {"udiv", "urem"}:
            if right == 0:
                raise UnsupportedConstructError(
                    "division by zero",
                    "division or remainder by zero is undefined and cannot be "
                    "compiled; a zero divisor has no result to compute",
                    location,
                )
            if right is None:
                raise UnsupportedConstructError(
                    "runtime divisor",
                    f"{op} requires a nonzero compile-time constant divisor; "
                    "general unsigned division by a runtime value is unsupported",
                    location,
                )
            if right & (right - 1) == 0:
                shift = right.bit_length() - 1
                if op == "udiv":
                    return self.shift("lshr", first, shift)
                return self.binary("and", first, self.const(right - 1, width))
        return self._intern(Node(op, width, (first, second)))

    def _resolve(self, first: int, second: int) -> int | None:
        """Simplify ``(p & c) | (p & ~c)`` to ``p`` for path conditions."""

        left, right = self.nodes[first], self.nodes[second]
        if left.op != "and" or right.op != "and":
            return None
        for shared, rest in ((0, 1), (1, 0)):
            for other_shared, other_rest in ((0, 1), (1, 0)):
                if left.args[shared] != right.args[other_shared]:
                    continue
                a, b = left.args[rest], right.args[other_rest]
                if self._inverted(a) == b or self._inverted(b) == a:
                    return left.args[shared]
        return None

    def shift(self, op: str, node: int, amount: int) -> int:
        width = self.width(node)
        if amount == 0:
            return node
        if amount >= width:
            # Shifting by the width or more is undefined in C; any value is a
            # valid refinement, so use the natural limit of the shift.
            if op != "ashr":
                return self.const(0, width)
            amount = width - 1
        value = self.constant_value(node)
        if value is not None:
            if op == "shl":
                return self.const(value << amount, width)
            if op == "lshr":
                return self.const(value >> amount, width)
            signed = value - (1 << width) if value >> (width - 1) else value
            return self.const(signed >> amount, width)
        return self._intern(Node(op, width, (node,), value=amount))

    def cast(self, op: str, node: int, width: int) -> int:
        source = self.width(node)
        if width == source:
            return node
        value = self.constant_value(node)
        if value is not None:
            if op == "sext" and value >> (source - 1):
                value -= 1 << source
            return self.const(value, width)
        entry = self.nodes[node]
        if op == "trunc" and entry.op in {"zext", "sext"}:
            inner = entry.args[0]
            inner_width = self.width(inner)
            if width == inner_width:
                return inner
            if width < inner_width:
                return self.cast("trunc", inner, width)
        if op == entry.op and op in {"zext", "trunc"}:
            return self.cast(op, entry.args[0], width)
        return self._intern(Node(op, width, (node,)))

    def compare(self, predicate: str, first: int, second: int) -> int:
        """Build an ``icmp`` from ``eq``/``ult`` nodes and free inversions."""

        width = self.width(first)
        if predicate == "ne":
            return self.invert(self.compare("eq", first, second))
        if predicate == "eq":
            left, right = self.constant_value(first), self.constant_value(second)
            if left is not None and right is not None:
                return self.const(int(left == right), 1)
            if first == second:
                return self.const(1, 1)
            return self.binary_compare("eq", first, second)
        if predicate.startswith("s"):
            sign = self.const(1 << (width - 1), width)
            first = self.binary("xor", first, sign)
            second = self.binary("xor", second, sign)
        relation = predicate[1:]
        if relation in {"gt", "le"}:
            first, second = second, first
        if relation not in {"lt", "gt", "le", "ge"}:
            raise UnsupportedConstructError(
                f"icmp predicate {predicate!r}",
                f"icmp predicate {predicate!r} is not supported",
            )
        less = self._less(first, second)
        return less if relation in {"lt", "gt"} else self.invert(less)

    def binary_compare(self, op: str, first: int, second: int) -> int:
        if op in _COMMUTATIVE and (
            self.constant_value(first) is not None
            or (self.constant_value(second) is None and first > second)
        ):
            first, second = second, first
        return self._intern(Node(op, 1, (first, second)))

    def _less(self, first: int, second: int) -> int:
        left, right = self.constant_value(first), self.constant_value(second)
        if left is not None and right is not None:
            return self.const(int(left < right), 1)
        if first == second or right == 0:
            return self.const(0, 1)
        if left == (1 << self.width(first)) - 1:
            return self.const(0, 1)
        return self._intern(Node("ult", 1, (first, second)))

    def select(self, condition: int, first: int, second: int) -> int:
        chosen = self.constant_value(condition)
        if chosen is not None:
            return first if chosen & 1 else second
        if first == second:
            return first
        inner = self._inverted(condition)
        if inner is not None:
            condition, first, second = inner, second, first
        width = self.width(first)
        if width == 1:
            left, right = self.constant_value(first), self.constant_value(second)
            if left == 1 and right == 0:
                return condition
            if left == 0 and right == 1:
                return self.invert(condition)
        return self._intern(Node("select", width, (condition, first, second)))


@dataclass(frozen=True, slots=True)
class Dag:
    """A function as one expression: ``root`` computes the return value."""

    name: str
    graph: Graph
    root: int
    parameters: tuple[tuple[str, int], ...]
    return_width: int


def _unsupported(instruction: Instruction, detail: str) -> UnsupportedConstructError:
    return UnsupportedConstructError(
        f"SSA operation {instruction.opcode!r}", detail, instruction.source_location
    )


class _Builder:
    def __init__(self, function: Function) -> None:
        self.function = function
        self.graph = Graph()
        self.values: dict[str, int] = {}
        self.blocks = {block.name: block for block in function.blocks}
        self.order = self._topological_order()
        self.position = {name: index for index, name in enumerate(self.order)}
        self.predecessors: dict[str, list[str]] = {name: [] for name in self.order}
        for name in self.order:
            for target in self.blocks[name].successors:
                self.predecessors[target].append(name)
        self.dominator = self._dominators()
        # Per translated block: its branch targets and the condition node
        # that picks the first one (``None`` for an unconditional branch).
        self.terminators: dict[str, tuple[tuple[str, ...], int | None]] = {}

    def _topological_order(self) -> list[str]:
        function = self.function
        pending = {block.name: 0 for block in function.blocks}
        for block in function.blocks:
            for target in block.successors:
                if target not in pending:
                    raise UnsupportedConstructError(
                        "malformed CFG",
                        f"block %{block.name} branches to unknown block %{target}",
                    )
                pending[target] += 1
        ready = [function.entry.name]
        order: list[str] = []
        while ready:
            name = ready.pop(0)
            order.append(name)
            for target in self.blocks[name].successors:
                pending[target] -= 1
                if pending[target] == 0:
                    ready.append(target)
        if len(order) != len(function.blocks):
            raise UnsupportedConstructError(
                "cyclic control flow",
                "cyclic control flow (a data-dependent loop) is not supported; "
                "every loop must unroll to a fixed number of iterations",
                function.source_location,
            )
        return order

    def _intersect(self, first: str, second: str) -> str:
        while first != second:
            while self.position[first] > self.position[second]:
                first = self.dominator[first]
            while self.position[second] > self.position[first]:
                second = self.dominator[second]
        return first

    def _dominators(self) -> dict[str, str]:
        entry = self.order[0]
        self.dominator = {entry: entry}
        for name in self.order[1:]:
            candidates = self.predecessors[name]
            chosen = candidates[0]
            for other in candidates[1:]:
                chosen = self._intersect(chosen, other)
            self.dominator[name] = chosen
        return self.dominator

    def _merge(self, dominator: str, merge: str, incoming: dict[str, int]) -> int:
        """Return the value that reaches ``merge``, as a tree of selects.

        ``incoming`` maps each block with an edge into ``merge`` to the value
        that edge carries.  Every path to ``merge`` runs through
        ``dominator``, so the answer is "the value that control leaving
        ``dominator`` ends up carrying".  That is computed backwards over the
        blocks in between: a conditional branch becomes a select between what
        its two targets carry, and a path that never reaches ``merge`` carries
        nothing and simply drops out of the select.
        """

        graph = self.graph
        stop = len(self.order) if merge == _EXIT else self.position[merge]
        leaving: dict[str, int | None] = {}
        for name in reversed(self.order[self.position[dominator] : stop]):
            targets, condition = self.terminators[name]
            carried = [
                incoming.get(name) if target == merge else leaving.get(target)
                for target in targets
            ]
            if not targets:
                leaving[name] = incoming.get(name) if merge == _EXIT else None
            elif condition is None:
                leaving[name] = carried[0]
            elif carried[0] is None or carried[1] is None:
                leaving[name] = carried[1] if carried[0] is None else carried[0]
            else:
                leaving[name] = graph.select(condition, carried[0], carried[1])
        result = leaving[dominator]
        if result is None:
            raise UnsupportedConstructError(
                "malformed CFG",
                f"no path from %{dominator} reaches the merge at %{merge}",
            )
        return result

    def _operand(self, operand: Operand) -> int:
        if isinstance(operand, IntConstant):
            return self.graph.const(operand.value, operand.type.width)
        assert isinstance(operand, SSAValue)
        return self.values[operand.name]

    def _constant_amount(self, instruction: Instruction) -> int:
        amount = instruction.operands[1]
        if not isinstance(amount, IntConstant):
            raise _unsupported(
                instruction,
                f"{instruction.opcode} by a runtime amount must be legalized "
                "into a barrel shifter before synthesis",
            )
        return amount.value

    def _instruction(self, instruction: Instruction) -> int:
        graph = self.graph
        opcode = instruction.opcode
        operands = [self._operand(operand) for operand in instruction.operands]
        if opcode in {"add", "sub", "mul", "and", "or", "xor", "udiv", "urem"}:
            return graph.binary(
                opcode, operands[0], operands[1], instruction.source_location
            )
        if opcode in {"shl", "lshr", "ashr"}:
            return graph.shift(opcode, operands[0], self._constant_amount(instruction))
        if opcode in {"zext", "sext", "trunc"}:
            assert isinstance(instruction.type, IntType)
            return graph.cast(opcode, operands[0], instruction.type.width)
        if opcode == "icmp":
            return graph.compare(instruction.predicate or "", operands[0], operands[1])
        if opcode == "select":
            return graph.select(operands[0], operands[1], operands[2])
        if opcode == "not":
            return graph.invert(operands[0])
        if opcode in {"copy", "const", "constant", "mov", "identity", "literal"}:
            return operands[0]
        if opcode in {"sdiv", "srem"}:
            raise _unsupported(
                instruction,
                "signed division and remainder are unsupported; use unsigned "
                "operations with a constant divisor",
            )
        raise _unsupported(instruction, f"{opcode!r} has no reversible lowering in qcc")

    def build(self) -> Dag:
        function = self.function
        graph = self.graph
        if not isinstance(function.return_type, IntType):
            raise UnsupportedConstructError(
                "void function",
                f"@{function.name} returns void; a circuit needs one integer result",
                function.source_location,
            )
        for index, parameter in enumerate(function.parameters):
            self.values[parameter.name] = graph.arg(
                parameter.name, parameter.type.width, index
            )
        returns: dict[str, int] = {}
        for name in self.order:
            block = self.blocks[name]
            # Phis read their inputs simultaneously on block entry.
            merged: dict[str, int] = {}
            for instruction in block.instructions:
                if instruction.opcode != "phi":
                    break
                assert instruction.result is not None
                merged[instruction.result.name] = self._merge(
                    self.dominator[name],
                    name,
                    {
                        edge.block: self._operand(edge.value)
                        for edge in instruction.incoming
                    },
                )
            self.values.update(merged)
            for instruction in block.instructions:
                opcode = instruction.opcode
                if opcode == "phi":
                    continue
                if opcode == "ret":
                    returns[name] = self._operand(instruction.operands[0])
                    self.terminators[name] = ((), None)
                elif opcode == "br":
                    self.terminators[name] = (
                        instruction.targets,
                        self._operand(instruction.operands[0])
                        if len(instruction.targets) == 2
                        else None,
                    )
                else:
                    assert instruction.result is not None
                    self.values[instruction.result.name] = self._instruction(
                        instruction
                    )
        if not returns:
            raise UnsupportedConstructError(
                "missing return", f"@{function.name} never returns a value"
            )
        sources = list(returns)
        if len(sources) == 1:
            root = returns[sources[0]]
        else:
            dominator = sources[0]
            for source in sources[1:]:
                dominator = self._intersect(dominator, source)
            root = self._merge(dominator, _EXIT, returns)
        return Dag(
            function.name,
            graph,
            root,
            tuple((item.name, item.type.width) for item in function.parameters),
            function.return_type.width,
        )


def build_dag(function: Function) -> Dag:
    """Convert ``function`` to a single dataflow expression."""

    return _Builder(function).build()


__all__ = ["Dag", "Graph", "Node", "build_dag"]
