"""Synthesize a reversible circuit from a dataflow graph.

Every value is a list of *bit references*.  A reference is either a constant
or a wire together with an inversion flag, which makes a large class of
operations free: constants, shifts, casts, masks, and bitwise NOT only
rearrange or relabel references and emit no gate and no qubit.  Gates appear
only where two unknown bits genuinely interact.

Arithmetic blocks clean up after themselves.  An adder computes its carries
into scratch wires, writes the sum, and immediately reverses the carry
computation, so the scratch wires go back to the pool for the next block.
What remains after a block is exactly its result.

The circuit as a whole follows Bennett's compute/use/uncompute shape, with
one refinement.  Intermediate values are computed first; the returned value
is then produced *directly in the output register* by gates that are never
undone; finally the intermediate computation is replayed in reverse.  Only
the intermediates pay the customary factor of two.
"""

from __future__ import annotations

from collections.abc import Sequence

from qcc.errors import UnsupportedConstructError
from qcc.lower.ancilla import CircuitLayout
from qcc.lower.bennett import Gate
from qcc.lower.ssa_to_rev import LoweredCircuit

from .dag import Dag, Node

# A bit reference is an int: 0 and 1 are the constants, and a wire ``w`` is
# ``2 + 2*w`` or, inverted, ``2 + 2*w + 1``.  XOR with 1 negates any reference.
ZERO = 0
ONE = 1
_GateTuple = tuple[str, tuple[int, ...]]

# Operations whose result can be produced straight into the output register.
_DIRECT = frozenset({"add", "sub", "mul", "and", "or", "xor", "select", "eq", "ult"})


def _bit(wire: int) -> int:
    return 2 + 2 * wire


def _wire(bit: int) -> int:
    return (bit - 2) >> 1


def _significant(bits: Sequence[int]) -> int:
    """Number of low bits up to and including the highest non-zero one."""

    for index in range(len(bits) - 1, -1, -1):
        if bits[index] != ZERO:
            return index + 1
    return 0


class _Synthesizer:
    def __init__(self, dag: Dag) -> None:
        self.dag = dag
        self.graph = dag.graph
        self.gates: list[_GateTuple] = []
        self.wire_count = 0
        self.free: list[int] = []
        self.bits: dict[int, list[int]] = {}
        self.division: dict[tuple[int, int], tuple[list[int], list[int]]] = {}
        self.inputs: dict[str, tuple[int, ...]] = {}
        for name, width in dag.parameters:
            self.inputs[name] = tuple(self._new_wire() for _ in range(width))
        self.output = tuple(self._new_wire() for _ in range(dag.return_width))

    # ------------------------------------------------------------------ wires

    def _new_wire(self) -> int:
        wire = self.wire_count
        self.wire_count += 1
        return wire

    def _alloc(self) -> int:
        """Return a wire that currently holds zero."""

        return self.free.pop() if self.free else self._new_wire()

    def _release(self, wire: int) -> None:
        """Give back a wire that is known to hold zero again."""

        self.free.append(wire)

    # ------------------------------------------------------------------ gates

    def _x(self, target: int) -> None:
        self.gates.append(("x", (target,)))

    def _cx(self, control: int, target: int) -> None:
        """``target ^= control`` for any bit reference ``control``."""

        if control == ZERO:
            return
        if control == ONE:
            self._x(target)
            return
        self.gates.append(("cx", (_wire(control), target)))
        if control & 1:
            self._x(target)

    def _ccx(self, first: int, second: int, target: int) -> None:
        """``target ^= first & second`` for any two bit references."""

        if first == ZERO or second == ZERO:
            return
        if first == ONE:
            self._cx(second, target)
            return
        if second == ONE:
            self._cx(first, target)
            return
        first_wire, second_wire = _wire(first), _wire(second)
        if first_wire == second_wire:
            if first == second:
                self._cx(first, target)
            return
        # (a ^ p)(b ^ q) = ab ^ pb ^ qa ^ pq, so inverted controls cost only
        # Clifford gates and the control wires are never touched.
        self.gates.append(("ccx", (first_wire, second_wire, target)))
        if first & 1:
            self.gates.append(("cx", (second_wire, target)))
        if second & 1:
            self.gates.append(("cx", (first_wire, target)))
        if first & 1 and second & 1:
            self._x(target)

    def _undo(self, start: int, stop: int) -> None:
        """Replay ``gates[start:stop]`` backwards; every gate is self-inverse."""

        self.gates.extend(reversed(self.gates[start:stop]))

    def _toggle_all(self, controls: Sequence[int], target: int) -> None:
        """``target ^= AND(controls)`` using a ladder of scratch wires."""

        unique: list[int] = []
        for control in controls:
            if control == ZERO or control ^ 1 in unique:
                return
            if control != ONE and control not in unique:
                unique.append(control)
        if not unique:
            self._x(target)
            return
        if len(unique) == 1:
            self._cx(unique[0], target)
            return
        start = len(self.gates)
        ladder: list[int] = []
        partial = unique[0]
        for control in unique[1:-1]:
            wire = self._alloc()
            ladder.append(wire)
            self._ccx(partial, control, wire)
            partial = _bit(wire)
        stop = len(self.gates)
        self._ccx(partial, unique[-1], target)
        self._undo(start, stop)
        for wire in reversed(ladder):
            self._release(wire)

    # ------------------------------------------------------------- bit logic

    def _and_bit(self, first: int, second: int, wire: int | None) -> int:
        if first == ZERO or second == ZERO or first == second ^ 1:
            return ZERO
        if first in (ONE, second):
            return second
        if second == ONE:
            return first
        target = self._alloc() if wire is None else wire
        self._ccx(first, second, target)
        return _bit(target)

    def _xor_bit(self, first: int, second: int, wire: int | None) -> int:
        if first < 2:
            return second ^ first
        if second < 2:
            return first ^ second
        if _wire(first) == _wire(second):
            return (first ^ second) & 1
        target = self._alloc() if wire is None else wire
        self._cx(first, target)
        self._cx(second, target)
        return _bit(target)

    def _select_bit(
        self, condition: int, first: int, second: int, wire: int | None
    ) -> int:
        """``condition ? first : second`` for single bit references."""

        if first == second or condition == ONE:
            return first
        if condition == ZERO:
            return second
        if first < 2 and second < 2:
            return condition ^ second
        if second == ZERO:
            return self._and_bit(condition, first, wire)
        if first == ZERO:
            return self._and_bit(condition ^ 1, second, wire)
        if second == ONE:
            return self._and_bit(condition, first ^ 1, wire) ^ 1
        if first == ONE:
            return self._and_bit(condition ^ 1, second ^ 1, wire) ^ 1
        if first == second ^ 1:
            return self._xor_bit(condition, second, wire)
        target = self._alloc() if wire is None else wire
        difference = self._alloc()
        self._cx(second, target)
        self._cx(first, difference)
        self._cx(second, difference)
        self._ccx(condition, _bit(difference), target)
        self._cx(second, difference)
        self._cx(first, difference)
        self._release(difference)
        return _bit(target)

    # ------------------------------------------------------------ arithmetic

    def _write_sum(
        self, target: int, addend: int, carry: int, applied: bool, control: int | None
    ) -> None:
        """Finish one sum bit: ``target ^= addend ^ carry`` (if ``control``)."""

        if control is None:
            if not applied:
                self._cx(addend, target)
            self._cx(carry, target)
            return
        if applied:
            self._cx(addend, target)
        if addend < 2 and carry < 2:
            if addend ^ carry:
                self._cx(control, target)
        elif addend < 2:
            self._ccx(control, carry ^ addend, target)
        elif carry < 2:
            self._ccx(control, addend ^ carry, target)
        else:
            # Fold the addend into the carry wire for one gate, then restore.
            carry_wire = _wire(carry)
            self._cx(addend, carry_wire)
            self._ccx(control, carry, target)
            self._cx(addend, carry_wire)

    def _add_into(
        self,
        target: Sequence[int],
        addend: Sequence[int],
        carry: int = ZERO,
        control: int | None = None,
    ) -> None:
        """In-place ripple adder: ``target += addend + carry`` modulo its width.

        ``target`` is a list of wires this block may modify; ``addend`` is any
        list of bit references that does not mention those wires.  With a
        ``control`` reference the addition happens only where it is one.

        Carries are computed bottom-up.  A carry normally lives in the addend
        wire it was computed from (the Cuccaro arrangement); where that wire
        is a constant, inverted, or shared, it lives in a scratch wire
        instead.  The sum is then written top-down, and each carry is
        uncomputed just before the target bit it came from is overwritten.
        """

        if control == ZERO:
            return
        if control == ONE:
            control = None
        width = len(target)
        uses: dict[int, int] = {}
        for bit in addend:
            if bit >= 2:
                uses[_wire(bit)] = uses.get(_wire(bit), 0) + 1
        if control is not None:
            uses[_wire(control)] = uses.get(_wire(control), 0) + 1
        steps: list[tuple[int, int, tuple[int, int] | None, bool, int | None]] = []
        for index in range(width - 1):
            bit = addend[index]
            wire = target[index]
            current = _bit(wire)
            if bit < 2 and carry < 2:
                steps.append((bit, carry, None, False, None))
                # MAJ(k, t, k) is k, and MAJ(k, t, ~k) is t itself.
                carry = bit if bit == carry else current
                continue
            if bit >= 2 and carry >= 2 and not bit & 1 and uses[_wire(bit)] == 1:
                # Cuccaro's trick: the addend wire is used nowhere else in
                # this block, so it can hold the outgoing carry until the
                # backward pass restores it.  No scratch wire is needed.
                carry_wire, borrowed = _wire(carry), _wire(bit)
                self._cx(bit, wire)
                start = len(self.gates)
                self._cx(bit, carry_wire)
                self._ccx(carry, current, borrowed)
                steps.append((bit, carry, (start, len(self.gates)), True, None))
                carry = _bit(borrowed)
                continue
            scratch = self._alloc()
            if bit < 2 or carry < 2:
                known, other = (bit, carry) if bit < 2 else (carry, bit)
                start = len(self.gates)
                # MAJ(k, t, o) = k ^ (t ^ k)(o ^ k)
                self._ccx(current ^ known, other ^ known, scratch)
                steps.append((bit, carry, (start, len(self.gates)), False, scratch))
                carry = _bit(scratch) ^ known
                continue
            # MAJ(a, t, c) = a ^ (t ^ a)(c ^ a).  The target keeps t ^ a, which
            # is the first half of the sum; the carry wire is restored.
            carry_wire = _wire(carry)
            self._cx(bit, wire)
            start = len(self.gates)
            self._cx(bit, carry_wire)
            self._ccx(current, carry, scratch)
            self._cx(bit, scratch)
            self._cx(bit, carry_wire)
            steps.append((bit, carry, (start, len(self.gates)), True, scratch))
            carry = _bit(scratch)

        self._write_sum(target[-1], addend[width - 1], carry, False, control)
        for index in range(width - 2, -1, -1):
            bit, carry_in, span, applied, held = steps[index]
            if span is not None:
                self._undo(*span)
            if held is not None:
                self._release(held)
            self._write_sum(target[index], bit, carry_in, applied, control)

    def _carry_out(
        self, first: Sequence[int], second: Sequence[int], carry: int
    ) -> tuple[int, list[int]]:
        """Compute the carry out of ``first + second + carry``.

        Returns the carry reference and the scratch wires holding the chain.
        Operand wires may hold intermediate carries until the caller replays
        these gates backwards, which restores them; the caller must do that
        before anything else reads the operands.  Every wire the chain touched
        stays reserved until then, because the replay is only an inverse if no
        wire in it has been handed to someone else in the meantime.
        """

        uses: dict[int, int] = {}
        for bit in (*first, *second):
            if bit >= 2:
                uses[_wire(bit)] = uses.get(_wire(bit), 0) + 1
        scratch: list[int] = []
        transient: tuple[int, int] | None = None
        # Whether the carry sits on a wire this chain may modify: its own
        # scratch or a borrowed operand wire, but not an aliased operand bit.
        owned = False
        for left, right in zip(first, second, strict=True):
            known = [bit for bit in (left, right, carry) if bit < 2]
            if len(known) == 3:
                carry = int(sum(known) >= 2)
            elif len(known) == 2:
                if known[0] != known[1]:
                    owned = owned and carry >= 2
                    carry = next(bit for bit in (left, right, carry) if bit >= 2)
                else:
                    carry = known[0]
            elif len(known) == 1:
                constant = known[0]
                free = [bit for bit in (left, right, carry) if bit >= 2]
                wire = self._alloc()
                scratch.append(wire)
                self._ccx(free[0] ^ constant, free[1] ^ constant, wire)
                carry = _bit(wire) ^ constant
                owned = True
            elif (
                owned
                and not left & 1
                and uses[_wire(left)] == 1
                and uses[_wire(right)] == 1
            ):
                # Cuccaro's trick again: MAJ(a, b, c) = a ^ (b ^ a)(c ^ a) is
                # written into a's own wire, with b and c holding the XORs.
                self._cx(left, _wire(right))
                self._cx(left, _wire(carry))
                self._ccx(carry, right, _wire(left))
                carry = _bit(_wire(left))
            else:
                # MAJ(a, b, c) = a ^ (a ^ b)(a ^ c) with two transient wires.
                wire = self._alloc()
                scratch.append(wire)
                if transient is None:
                    transient = (self._alloc(), self._alloc())
                    scratch.extend(transient)
                left_xor, carry_xor = transient
                start = len(self.gates)
                self._cx(left, left_xor)
                self._cx(right, left_xor)
                self._cx(left, carry_xor)
                self._cx(carry, carry_xor)
                stop = len(self.gates)
                self._ccx(_bit(left_xor), _bit(carry_xor), wire)
                self._cx(left, wire)
                self._undo(start, stop)
                carry = _bit(wire)
                owned = True
        return carry, scratch

    def _add(
        self,
        first: Sequence[int],
        second: Sequence[int],
        out: Sequence[int] | None,
        carry: int = ZERO,
    ) -> list[int]:
        """Out-of-place ``first + second + carry`` on fresh result wires."""

        width = len(first)
        # Without known-zero high bits the sum wraps; otherwise one extra bit
        # holds the whole result and everything above stays constant zero.
        needed = min(width, max(_significant(first), _significant(second)) + 1)
        if sum(bit < 2 for bit in first) > sum(bit < 2 for bit in second):
            first, second = second, first
        target = [
            out[index] if out is not None else self._alloc() for index in range(needed)
        ]
        for index, wire in enumerate(target):
            self._cx(first[index], wire)
        if needed:
            self._add_into(target, second[:needed], carry)
        return [_bit(wire) for wire in target] + [ZERO] * (width - needed)

    def _less(
        self, first: Sequence[int], second: Sequence[int], out: int | None
    ) -> int:
        """Unsigned ``first < second`` as one bit reference."""

        start = len(self.gates)
        # first >= second exactly when first + ~second + 1 carries out.
        carry, scratch = self._carry_out(first, [bit ^ 1 for bit in second], ONE)
        stop = len(self.gates)
        result = carry ^ 1
        if carry >= 2:
            wire = self._alloc() if out is None else out
            self._cx(carry, wire)
            result = _bit(wire) ^ 1
        self._undo(start, stop)
        for used in reversed(scratch):
            self._release(used)
        return result

    def _equal(
        self, first: Sequence[int], second: Sequence[int], out: int | None
    ) -> int:
        start = len(self.gates)
        scratch: list[int] = []
        matches: list[int] = []
        differs = False
        for left, right in zip(first, second, strict=True):
            difference = self._xor_bit(left, right, None)
            if (
                difference >= 2
                and left >= 2
                and right >= 2
                and _wire(difference) not in (_wire(left), _wire(right))
            ):
                scratch.append(_wire(difference))
            if difference == ONE:
                differs = True
            elif difference != ZERO:
                matches.append(difference ^ 1)
        stop = len(self.gates)
        result = ZERO
        if not differs:
            if matches:
                wire = self._alloc() if out is None else out
                self._toggle_all(matches, wire)
                result = _bit(wire)
            else:
                result = ONE
        self._undo(start, stop)
        for used in reversed(scratch):
            self._release(used)
        return result

    def _multiply(
        self, first: Sequence[int], second: Sequence[int], out: Sequence[int] | None
    ) -> list[int]:
        """Shift-and-add product modulo ``2**width`` on fresh result wires."""

        width = len(first)
        # Fewer unknown multiplier bits means fewer controlled additions.
        if sum(bit >= 2 for bit in first) < sum(bit >= 2 for bit in second):
            first, second = second, first
        span, rows = _significant(first), _significant(second)
        needed = min(width, span + rows)
        if span == 0 or rows == 0:
            return [ZERO] * width
        product = [
            out[index] if out is not None else self._alloc() for index in range(needed)
        ]
        started = False
        for row in range(rows):
            control = second[row]
            if control == ZERO or row >= needed:
                continue
            # After this row the partial sum is below 2**(row + span + 1).
            top = min(needed, row + span + 1)
            window = product[row:top]
            addend = list(first[: len(window)])
            addend += [ZERO] * (len(window) - len(addend))
            if not started:
                # The accumulator is still zero, so the row is copied in.
                for wire, bit in zip(window, addend, strict=True):
                    self._ccx(control, bit, wire)
                started = True
            else:
                self._add_into(window, addend, ZERO, control)
        return [_bit(wire) for wire in product] + [ZERO] * (width - needed)

    def _divide(self, node: int, divisor: int) -> tuple[list[int], list[int]]:
        """Restoring division by a constant: ``(quotient, remainder)`` bits.

        The dividend is copied into a work register that the remainder then
        occupies in place, sliding down one bit per step.  Each step compares
        the running remainder with the divisor, records the quotient bit in a
        fresh wire, and subtracts the divisor where that bit is set.
        """

        cached = self.division.get((node, divisor))
        if cached is not None:
            return cached
        dividend = self.bits[node]
        width = len(dividend)
        size = divisor.bit_length()
        work = [self._alloc() for _ in range(width + 1)]
        for bit, wire in zip(dividend, work, strict=False):
            self._cx(bit, wire)
        quotient = [ZERO] * width
        modulus = 1 << (size + 1)
        negated = (-divisor) % modulus
        inverted = (~divisor) % modulus
        # The first size-1 steps cannot reach the divisor: fewer than size
        # dividend bits have been shifted in.
        for index in range(width - size, -1, -1):
            window = work[index : index + size + 1]
            window_bits = [_bit(wire) for wire in window]
            start = len(self.gates)
            fits, scratch = self._carry_out(
                window_bits, [(inverted >> bit) & 1 for bit in range(size + 1)], ONE
            )
            stop = len(self.gates)
            if fits < 2:
                self._undo(start, stop)
                for used in reversed(scratch):
                    self._release(used)
                if fits == ONE:
                    self._add_into(
                        window, [(negated >> bit) & 1 for bit in range(size + 1)]
                    )
                    quotient[index] = ONE
                continue
            flag = self._alloc()
            self._cx(fits, flag)
            self._undo(start, stop)
            for used in reversed(scratch):
                self._release(used)
            taken = _bit(flag)
            self._add_into(
                window,
                [taken if (negated >> bit) & 1 else ZERO for bit in range(size + 1)],
            )
            quotient[index] = taken
        # The remainder is below the divisor, so the wires above it are zero.
        kept = min(size, width)
        for wire in work[kept:]:
            self._release(wire)
        remainder = [_bit(wire) for wire in work[:kept]] + [ZERO] * (width - kept)
        result = (quotient, remainder)
        self.division[(node, divisor)] = result
        return result

    # ----------------------------------------------------------------- nodes

    def _lower(self, identifier: int, out: Sequence[int] | None = None) -> list[int]:
        """Return the bit references of a node, emitting gates as needed.

        ``out`` names wires the result must use wherever it needs a wire of
        its own; it is how the returned value lands in the output register.
        """

        node: Node = self.graph.nodes[identifier]
        op = node.op
        width = node.width
        operands = [self.bits[argument] for argument in node.args]

        def wire_for(index: int) -> int | None:
            return None if out is None else out[index]

        if op == "const":
            return [(node.value >> index) & 1 for index in range(width)]
        if op == "arg":
            return [_bit(wire) for wire in self.inputs[node.name]]
        if op == "and":
            return [
                self._and_bit(left, right, wire_for(index))
                for index, (left, right) in enumerate(zip(*operands, strict=True))
            ]
        if op == "or":
            return [
                self._and_bit(left ^ 1, right ^ 1, wire_for(index)) ^ 1
                for index, (left, right) in enumerate(zip(*operands, strict=True))
            ]
        if op == "xor":
            return [
                self._xor_bit(left, right, wire_for(index))
                for index, (left, right) in enumerate(zip(*operands, strict=True))
            ]
        if op == "add":
            return self._add(operands[0], operands[1], out)
        if op == "sub":
            return self._add(operands[0], [bit ^ 1 for bit in operands[1]], out, ONE)
        if op == "mul":
            return self._multiply(operands[0], operands[1], out)
        if op in {"udiv", "urem"}:
            divisor = self.graph.constant_value(node.args[1])
            if divisor is None or divisor == 0:
                raise UnsupportedConstructError(
                    "runtime divisor",
                    f"{op} requires a nonzero compile-time constant divisor",
                )
            quotient, remainder = self._divide(node.args[0], divisor)
            return list(quotient if op == "udiv" else remainder)
        if op == "shl":
            return [ZERO] * node.value + operands[0][: width - node.value]
        if op == "lshr":
            return operands[0][node.value :] + [ZERO] * node.value
        if op == "ashr":
            return operands[0][node.value :] + [operands[0][-1]] * node.value
        if op == "zext":
            return operands[0] + [ZERO] * (width - len(operands[0]))
        if op == "sext":
            return operands[0] + [operands[0][-1]] * (width - len(operands[0]))
        if op == "trunc":
            return operands[0][:width]
        if op == "eq":
            return [self._equal(operands[0], operands[1], wire_for(0))]
        if op == "ult":
            return [self._less(operands[0], operands[1], wire_for(0))]
        if op == "select":
            condition = operands[0][0]
            return [
                self._select_bit(condition, left, right, wire_for(index))
                for index, (left, right) in enumerate(
                    zip(operands[1], operands[2], strict=True)
                )
            ]
        raise UnsupportedConstructError(
            f"graph operation {op!r}", f"{op!r} has no reversible lowering"
        )

    def run(self) -> LoweredCircuit:
        graph = self.graph
        root = self.dag.root
        live: list[int] = []
        seen: set[int] = set()
        pending = [root]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            live.append(current)
            pending.extend(graph.nodes[current].args)
        live.sort()
        uses: dict[int, int] = {}
        for identifier in live:
            for argument in graph.nodes[identifier].args:
                uses[argument] = uses.get(argument, 0) + 1

        # A zero-extension of a single-use value is the same wires plus
        # constant zeros, so the inner value can be built in the low output
        # wires directly.
        final = root
        targets = list(self.output)
        while graph.nodes[final].op == "zext" and uses[graph.nodes[final].args[0]] == 1:
            final = graph.nodes[final].args[0]
            targets = targets[: graph.nodes[final].width]
        direct = graph.nodes[final].op in _DIRECT

        for identifier in live:
            if identifier > final or (identifier == final and direct):
                continue
            self.bits[identifier] = self._lower(identifier)

        boundary = len(self.gates)
        result = self._lower(final, targets) if direct else self.bits[final]
        for bit, wire in zip(result, targets, strict=True):
            if bit >= 2 and _wire(bit) == wire:
                # Already in place; an inverted reference just needs the flip.
                if bit & 1:
                    self._x(wire)
            else:
                self._cx(bit, wire)
        # Undo every intermediate value; the output register is untouched.
        self._undo(0, boundary)

        gates = tuple(Gate(name, qubits) for name, qubits in self.gates)
        reserved = set(self.output)
        for wires in self.inputs.values():
            reserved.update(wires)
        ancillas = tuple(
            wire for wire in range(self.wire_count) if wire not in reserved
        )
        return LoweredCircuit(
            gates=gates,
            num_qubits=self.wire_count,
            layout=CircuitLayout(
                inputs=dict(self.inputs), output=self.output, ancillas=ancillas
            ),
            function_name=self.dag.name,
        )


def synthesize(dag: Dag) -> LoweredCircuit:
    """Compile a dataflow graph to an X/CX/CCX circuit."""

    return _Synthesizer(dag).run()


__all__ = ["synthesize"]
