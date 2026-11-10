"""Reference semantics for SSA functions and circuit equivalence checking.

Two independent evaluators define what a function means:

* :func:`evaluate` interprets the control-flow graph for one input, the way a
  CPU would run it.
* :func:`evaluate_columns` evaluates every input of a batch at once on
  bit-sliced values, following all paths and merging by path condition.

Neither shares code with the circuit synthesizer.  :func:`verify` runs a
compiled circuit through the bit-sliced simulator and compares it with the
reference on every input when the input space is small enough, and on a
deterministic sample otherwise.  It checks the whole reversible contract: the
output register holds the function value, every input register is unchanged,
and every other wire is back at zero.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from qcc.errors import UnsupportedConstructError
from qcc.ir.netlist import Circuit
from qcc.ir.ssa import (
    BasicBlock,
    Function,
    Instruction,
    IntConstant,
    IntType,
    Operand,
    SSAValue,
)
from qcc.sim.bitsliced import (
    InputChunk,
    exhaustive_columns,
    first_set_bit,
    sample_columns,
    simulate_columns,
)

Columns = list[int]


class VerificationError(AssertionError):
    """A circuit does not implement its function's reversible contract."""

    def __init__(self, result: VerificationResult) -> None:
        self.result = result
        super().__init__(result.describe())


@dataclass(frozen=True, slots=True)
class Counterexample:
    """One input on which a circuit breaks its contract."""

    inputs: Mapping[str, int]
    kind: str
    expected: int | None = None
    actual: int | None = None
    detail: str = ""

    def describe(self) -> str:
        rendered = ", ".join(f"{name}={value}" for name, value in self.inputs.items())
        if self.kind == "output":
            return (
                f"wrong output for ({rendered}): expected {self.expected}, "
                f"got {self.actual}"
            )
        return f"{self.detail} for ({rendered})"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of checking a circuit against its reference function."""

    function: str
    vectors: int
    exhaustive: bool
    input_bits: int
    counterexample: Counterexample | None = None

    @property
    def ok(self) -> bool:
        return self.counterexample is None

    def __bool__(self) -> bool:
        return self.ok

    def describe(self) -> str:
        coverage = (
            f"all {self.vectors} inputs"
            if self.exhaustive
            else f"{self.vectors} sampled inputs of 2**{self.input_bits}"
        )
        if self.counterexample is None:
            return f"{self.function}: verified on {coverage}"
        return f"{self.function}: {self.counterexample.describe()} ({coverage})"


def _signed(value: int, width: int) -> int:
    return value - (1 << width) if value >> (width - 1) else value


def _compare(predicate: str, first: int, second: int, width: int) -> bool:
    if predicate == "eq":
        return first == second
    if predicate == "ne":
        return first != second
    if predicate.startswith("s"):
        first, second = _signed(first, width), _signed(second, width)
    relation = predicate[1:]
    if relation == "lt":
        return first < second
    if relation == "le":
        return first <= second
    if relation == "gt":
        return first > second
    if relation == "ge":
        return first >= second
    raise UnsupportedConstructError(
        f"icmp predicate {predicate!r}", f"unknown icmp predicate {predicate!r}"
    )


def _result_width(instruction: Instruction) -> int:
    if isinstance(instruction.type, IntType):
        return instruction.type.width
    if instruction.result is not None:
        return instruction.result.type.width
    raise UnsupportedConstructError(
        f"SSA operation {instruction.opcode!r}",
        f"{instruction.opcode} has no integer result type",
        instruction.source_location,
    )


def _constant_amount(instruction: Instruction) -> int:
    amount = instruction.operands[1]
    if not isinstance(amount, IntConstant):
        raise UnsupportedConstructError(
            "variable shift",
            f"{instruction.opcode} by a runtime amount must be legalized first",
            instruction.source_location,
        )
    return amount.value


def _unknown(instruction: Instruction) -> UnsupportedConstructError:
    return UnsupportedConstructError(
        f"SSA operation {instruction.opcode!r}",
        f"the reference evaluator has no semantics for {instruction.opcode!r}",
        instruction.source_location,
    )


def _apply_scalar(instruction: Instruction, operands: Sequence[int]) -> int:
    """Evaluate one value-producing instruction on unsigned bit patterns."""

    opcode = instruction.opcode
    width = _result_width(instruction)
    mask = (1 << width) - 1
    if opcode == "add":
        return (operands[0] + operands[1]) & mask
    if opcode == "sub":
        return (operands[0] - operands[1]) & mask
    if opcode == "mul":
        return (operands[0] * operands[1]) & mask
    if opcode in {"udiv", "urem"}:
        if operands[1] == 0:
            raise ZeroDivisionError(f"{opcode} by zero is undefined")
        quotient, remainder = divmod(operands[0], operands[1])
        return quotient if opcode == "udiv" else remainder
    if opcode == "and":
        return operands[0] & operands[1]
    if opcode == "or":
        return operands[0] | operands[1]
    if opcode == "xor":
        return operands[0] ^ operands[1]
    if opcode == "not":
        return ~operands[0] & mask
    if opcode in {"shl", "lshr", "ashr"}:
        amount = _constant_amount(instruction)
        if amount >= width:
            return 0
        if opcode == "shl":
            return (operands[0] << amount) & mask
        if opcode == "lshr":
            return operands[0] >> amount
        return (_signed(operands[0], width) >> amount) & mask
    if opcode in {"zext", "trunc"}:
        return operands[0] & mask
    if opcode == "sext":
        source = instruction.operands[0].type.width
        return _signed(operands[0], source) & mask
    if opcode == "icmp":
        compared = instruction.operands[0].type.width
        return int(
            _compare(instruction.predicate or "", operands[0], operands[1], compared)
        )
    if opcode == "select":
        return operands[1] if operands[0] & 1 else operands[2]
    if opcode in {"copy", "const", "constant", "mov", "identity", "literal"}:
        return operands[0] & mask
    raise _unknown(instruction)


def _bind_arguments(
    function: Function, arguments: Mapping[str, int] | Sequence[int]
) -> dict[str, int]:
    parameters = function.parameters
    if isinstance(arguments, Mapping):
        missing = [item.name for item in parameters if item.name not in arguments]
        if missing or len(arguments) != len(parameters):
            raise ValueError(
                f"@{function.name} takes ({', '.join(p.name for p in parameters)})"
            )
        values = [arguments[item.name] for item in parameters]
    else:
        values = list(arguments)
        if len(values) != len(parameters):
            raise ValueError(
                f"@{function.name} takes {len(parameters)} argument(s), "
                f"got {len(values)}"
            )
    return {
        parameter.name: parameter.type.normalize(int(value))
        for parameter, value in zip(parameters, values, strict=True)
    }


def evaluate(function: Function, arguments: Mapping[str, int] | Sequence[int]) -> int:
    """Run ``function`` on one input and return its unsigned result pattern."""

    environment = _bind_arguments(function, arguments)

    def read(operand: Operand) -> int:
        if isinstance(operand, IntConstant):
            return operand.value
        assert isinstance(operand, SSAValue)
        return environment[operand.name]

    block: BasicBlock = function.entry
    previous: str | None = None
    while True:
        # Phis read their inputs simultaneously, before any is overwritten.
        merged: dict[str, int] = {}
        for instruction in block.instructions:
            if instruction.opcode != "phi":
                break
            assert instruction.result is not None
            edge = next(
                (item for item in instruction.incoming if item.block == previous), None
            )
            if edge is None:
                raise UnsupportedConstructError(
                    "malformed phi",
                    f"phi in %{block.name} has no value for predecessor %{previous}",
                    instruction.source_location,
                )
            merged[instruction.result.name] = read(edge.value)
        environment.update(merged)
        for instruction in block.instructions:
            opcode = instruction.opcode
            if opcode == "phi":
                continue
            if opcode == "ret":
                return read(instruction.operands[0])
            if opcode == "br":
                if len(instruction.targets) == 1:
                    target = instruction.targets[0]
                else:
                    taken = read(instruction.operands[0]) & 1
                    target = instruction.targets[0 if taken else 1]
                previous, block = block.name, function.get_block(target)
                break
            assert instruction.result is not None
            environment[instruction.result.name] = _apply_scalar(
                instruction, [read(operand) for operand in instruction.operands]
            )
        else:
            raise UnsupportedConstructError(
                "missing terminator", f"block %{block.name} does not end in ret or br"
            )


def _add_columns(first: Columns, second: Columns, carry: int) -> Columns:
    total: Columns = []
    for left, right in zip(first, second, strict=True):
        partial = left ^ right
        total.append(partial ^ carry)
        carry = (left & right) | (carry & partial)
    return total


def _borrow_columns(first: Columns, second: Columns, mask: int) -> int:
    """Return the slice of inputs where ``first < second`` (unsigned)."""

    borrow = 0
    for left, right in zip(first, second, strict=True):
        borrow = ((left ^ mask) & right) | (borrow & ((left ^ right) ^ mask))
    return borrow


def _select_columns(condition: int, first: Columns, second: Columns) -> Columns:
    return [
        second_bit ^ (condition & (first_bit ^ second_bit))
        for first_bit, second_bit in zip(first, second, strict=True)
    ]


def _divide_columns(
    dividend: Columns, divisor: Columns, mask: int
) -> tuple[Columns, Columns]:
    """Restoring division on slices; returns ``(quotient, remainder)``."""

    width = len(dividend)
    wide_divisor = [*divisor, 0]
    inverted = [bit ^ mask for bit in wide_divisor]
    remainder: Columns = [0] * (width + 1)
    quotient: Columns = [0] * width
    for index in range(width - 1, -1, -1):
        remainder = [dividend[index], *remainder[:-1]]
        fits = _borrow_columns(remainder, wide_divisor, mask) ^ mask
        difference = _add_columns(remainder, inverted, mask)
        remainder = _select_columns(fits, difference, remainder)
        quotient[index] = fits
    return quotient, remainder[:width]


def _apply_columns(
    instruction: Instruction, operands: Sequence[Columns], mask: int
) -> Columns:
    """Evaluate one value-producing instruction on bit-sliced operands."""

    opcode = instruction.opcode
    width = _result_width(instruction)
    if opcode == "add":
        return _add_columns(operands[0], operands[1], 0)
    if opcode == "sub":
        return _add_columns(operands[0], [bit ^ mask for bit in operands[1]], mask)
    if opcode == "mul":
        product: Columns = [0] * width
        for shift, selector in enumerate(operands[1]):
            if selector:
                partial = [0] * shift + [
                    bit & selector for bit in operands[0][: width - shift]
                ]
                product = _add_columns(product, partial, 0)
        return product
    if opcode in {"udiv", "urem"}:
        quotient, remainder = _divide_columns(operands[0], operands[1], mask)
        return quotient if opcode == "udiv" else remainder
    if opcode == "and":
        return [a & b for a, b in zip(operands[0], operands[1], strict=True)]
    if opcode == "or":
        return [a | b for a, b in zip(operands[0], operands[1], strict=True)]
    if opcode == "xor":
        return [a ^ b for a, b in zip(operands[0], operands[1], strict=True)]
    if opcode == "not":
        return [bit ^ mask for bit in operands[0]]
    if opcode in {"shl", "lshr", "ashr"}:
        amount = _constant_amount(instruction)
        value = operands[0]
        if amount >= width:
            return [0] * width
        if opcode == "shl":
            return [0] * amount + value[: width - amount]
        fill = value[-1] if opcode == "ashr" else 0
        return value[amount:] + [fill] * amount
    if opcode == "zext":
        return operands[0] + [0] * (width - len(operands[0]))
    if opcode == "sext":
        return operands[0] + [operands[0][-1]] * (width - len(operands[0]))
    if opcode == "trunc":
        return operands[0][:width]
    if opcode == "icmp":
        first, second = operands[0], operands[1]
        predicate = instruction.predicate or ""
        if predicate in {"eq", "ne"}:
            differs = 0
            for left, right in zip(first, second, strict=True):
                differs |= left ^ right
            return [differs if predicate == "ne" else differs ^ mask]
        if predicate.startswith("s"):
            first = [*first[:-1], first[-1] ^ mask]
            second = [*second[:-1], second[-1] ^ mask]
        relation = predicate[1:]
        if relation in {"gt", "le"}:
            first, second = second, first
        less = _borrow_columns(first, second, mask)
        if relation in {"lt", "gt"}:
            return [less]
        if relation in {"le", "ge"}:
            return [less ^ mask]
        raise UnsupportedConstructError(
            f"icmp predicate {predicate!r}", f"unknown icmp predicate {predicate!r}"
        )
    if opcode == "select":
        return _select_columns(operands[0][0], operands[1], operands[2])
    if opcode in {"copy", "const", "constant", "mov", "identity", "literal"}:
        return list(operands[0])
    raise _unknown(instruction)


def _topological_blocks(function: Function) -> list[BasicBlock]:
    pending = {block.name: 0 for block in function.blocks}
    for block in function.blocks:
        for target in dict.fromkeys(block.successors):
            pending[target] += 1
    ready = [function.entry.name]
    order: list[BasicBlock] = []
    while ready:
        block = function.get_block(ready.pop())
        order.append(block)
        for target in dict.fromkeys(block.successors):
            pending[target] -= 1
            if pending[target] == 0:
                ready.append(target)
    return order


def evaluate_columns(
    function: Function, arguments: Mapping[str, Sequence[int]], count: int
) -> Columns:
    """Evaluate ``count`` inputs at once and return the result's bit slices.

    ``arguments[name][i]`` is the slice of bit ``i`` of parameter ``name``.
    Every block is evaluated for every input; a per-block slice records which
    inputs actually reach it, and phis and returns merge by those slices.
    """

    mask = (1 << count) - 1
    environment: dict[str, Columns] = {}
    for parameter in function.parameters:
        columns = list(arguments[parameter.name])
        if len(columns) != parameter.type.width:
            raise ValueError(f"parameter {parameter.name!r} has the wrong width")
        environment[parameter.name] = columns

    def read(operand: Operand) -> Columns:
        if isinstance(operand, IntConstant):
            return [
                mask if (operand.value >> bit) & 1 else 0
                for bit in range(operand.type.width)
            ]
        assert isinstance(operand, SSAValue)
        return environment[operand.name]

    if not isinstance(function.return_type, IntType):
        raise UnsupportedConstructError(
            "void function", f"@{function.name} does not return an integer"
        )
    reached: dict[str, int] = {function.entry.name: mask}
    edges: dict[tuple[str, str], int] = {}
    result: Columns = [0] * function.return_type.width
    for block in _topological_blocks(function):
        active = reached.get(block.name, 0)
        merged: dict[str, Columns] = {}
        for instruction in block.instructions:
            if instruction.opcode != "phi":
                break
            assert instruction.result is not None
            value = [0] * instruction.result.type.width
            for edge in instruction.incoming:
                taken = edges.get((edge.block, block.name), 0)
                value = [
                    bit | (taken & incoming)
                    for bit, incoming in zip(value, read(edge.value), strict=True)
                ]
            merged[instruction.result.name] = value
        environment.update(merged)
        for instruction in block.instructions:
            opcode = instruction.opcode
            if opcode == "phi":
                continue
            if opcode == "ret":
                result = [
                    bit | (active & returned)
                    for bit, returned in zip(
                        result, read(instruction.operands[0]), strict=True
                    )
                ]
            elif opcode == "br":
                if len(instruction.targets) == 1:
                    outgoing: tuple[tuple[str, int], ...] = (
                        (instruction.targets[0], active),
                    )
                else:
                    condition = read(instruction.operands[0])[0]
                    outgoing = (
                        (instruction.targets[0], active & condition),
                        (instruction.targets[1], active & (condition ^ mask)),
                    )
                for target, taken in outgoing:
                    edges[(block.name, target)] = (
                        edges.get((block.name, target), 0) | taken
                    )
                    reached[target] = reached.get(target, 0) | taken
            else:
                assert instruction.result is not None
                environment[instruction.result.name] = _apply_columns(
                    instruction,
                    [read(operand) for operand in instruction.operands],
                    mask,
                )
    return result


def _register_wires(circuit: Circuit, name: str) -> tuple[int, ...]:
    layout = circuit.layout
    if layout is None or name not in layout.registers:
        raise ValueError(f"circuit has no register named {name!r}")
    return tuple(int(wire) for wire in layout.registers[name])


def _corner_values(function: Function) -> Iterator[int]:
    """Combined input values that exercise boundary cases of every argument."""

    widths = [parameter.type.width for parameter in function.parameters]
    offsets = [sum(widths[:index]) for index in range(len(widths))]
    total = sum(widths)
    patterns = (
        lambda width: 0,
        lambda width: (1 << width) - 1,
        lambda width: 1,
        lambda width: 1 << (width - 1),
        lambda width: (1 << (width - 1)) - 1,
    )
    yield 0
    yield (1 << total) - 1
    for index, (width, offset) in enumerate(zip(widths, offsets, strict=True)):
        for pattern in patterns:
            value = pattern(width) << offset
            yield value
            others = ((1 << total) - 1) & ~(((1 << width) - 1) << offset)
            yield value | others
        del index


def _chunks(
    function: Function,
    input_bits: int,
    *,
    exhaustive_limit: int,
    samples: int,
    seed: int,
    chunk_bits: int,
) -> tuple[bool, Iterable[InputChunk]]:
    if input_bits <= exhaustive_limit:
        return True, exhaustive_columns(input_bits, chunk_bits=chunk_bits)
    generator = random.Random(seed)
    chosen = list(dict.fromkeys(_corner_values(function)))
    seen = set(chosen)
    while len(chosen) < samples:
        candidate = generator.getrandbits(input_bits)
        if candidate not in seen:
            seen.add(candidate)
            chosen.append(candidate)
    size = 1 << chunk_bits
    return False, (
        sample_columns(chosen[start : start + size], input_bits)
        for start in range(0, len(chosen), size)
    )


def verify(
    circuit: Circuit,
    function: Function,
    *,
    exhaustive_limit: int = 20,
    samples: int = 4096,
    seed: int = 0,
    chunk_bits: int = 16,
) -> VerificationResult:
    """Check ``circuit`` against ``function`` and report the first failure.

    Every input is tried when the function takes at most ``exhaustive_limit``
    input bits; otherwise ``samples`` distinct inputs are drawn from a
    generator seeded with ``seed``, always including boundary values.
    """

    if circuit.layout is None:
        raise ValueError("verification needs a circuit with a register layout")
    parameters = function.parameters
    input_wires = {
        item.name: _register_wires(circuit, item.name) for item in parameters
    }
    for parameter in parameters:
        if len(input_wires[parameter.name]) != parameter.type.width:
            raise ValueError(
                f"register {parameter.name!r} does not match the parameter width"
            )
    output_wires = _register_wires(circuit, "output")
    swept = [wire for item in parameters for wire in input_wires[item.name]]
    input_bits = len(swept)
    preserved = set(swept)
    written = set(output_wires)
    scratch = [
        wire
        for wire in range(circuit.num_qubits)
        if wire not in preserved and wire not in written
    ]

    def inputs_of(chunk: InputChunk, index: int) -> dict[str, int]:
        combined = chunk.value(index)
        values: dict[str, int] = {}
        offset = 0
        for parameter in parameters:
            width = parameter.type.width
            values[parameter.name] = (combined >> offset) & ((1 << width) - 1)
            offset += width
        return values

    exhaustive, chunks = _chunks(
        function,
        input_bits,
        exhaustive_limit=exhaustive_limit,
        samples=samples,
        seed=seed,
        chunk_bits=chunk_bits,
    )
    vectors = 0
    for chunk in chunks:
        vectors += chunk.count
        initial = dict(zip(swept, chunk.columns, strict=True))
        final = simulate_columns(circuit, initial, chunk.count)
        arguments: dict[str, Sequence[int]] = {}
        offset = 0
        for parameter in parameters:
            width = parameter.type.width
            arguments[parameter.name] = chunk.columns[offset : offset + width]
            offset += width
        expected = evaluate_columns(function, arguments, chunk.count)

        failure: tuple[int, str, str] | None = None
        for bit, wire in enumerate(output_wires):
            difference = final[wire] ^ expected[bit]
            if difference:
                failure = (first_set_bit(difference), "output", "")
                break
        if failure is None:
            for wire in swept:
                difference = final[wire] ^ initial[wire]
                if difference:
                    failure = (
                        first_set_bit(difference),
                        "input",
                        f"input wire q[{wire}] was not restored",
                    )
                    break
        if failure is None:
            for wire in scratch:
                if final[wire]:
                    failure = (
                        first_set_bit(final[wire]),
                        "ancilla",
                        f"work wire q[{wire}] was left dirty",
                    )
                    break
        if failure is not None:
            index, kind, detail = failure
            inputs = inputs_of(chunk, index)
            actual = sum(
                ((final[wire] >> index) & 1) << bit
                for bit, wire in enumerate(output_wires)
            )
            return VerificationResult(
                function.name,
                vectors,
                exhaustive,
                input_bits,
                Counterexample(
                    inputs,
                    kind,
                    expected=evaluate(function, inputs),
                    actual=actual,
                    detail=detail,
                ),
            )
    return VerificationResult(function.name, vectors, exhaustive, input_bits)


def assert_verified(
    circuit: Circuit, function: Function, **options: int
) -> VerificationResult:
    """Run :func:`verify` and raise :class:`VerificationError` on a failure."""

    result = verify(circuit, function, **options)
    if not result.ok:
        raise VerificationError(result)
    return result


__all__ = [
    "Counterexample",
    "VerificationError",
    "VerificationResult",
    "assert_verified",
    "evaluate",
    "evaluate_columns",
    "verify",
]
