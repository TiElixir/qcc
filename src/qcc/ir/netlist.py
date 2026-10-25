"""Immutable-friendly reversible gate netlists.

The IR deliberately stays small: wires are integer indices, registers are
represented by *LSB-first* sequences of those indices, and every primitive
operation is self-inverse.  :class:`CircuitBuilder` is the mutable boundary;
its :meth:`~CircuitBuilder.build` method freezes the gate sequence in a
:class:`Circuit`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from operator import index as _index
from types import MappingProxyType
from typing import Any, ClassVar, TypeAlias

QubitLike: TypeAlias = int


class _DomainToken:
    """Identity-only token used to reject accidental cross-allocator wiring."""

    __slots__ = ("serial",)
    _next_serial = 0

    def __init__(self) -> None:
        self.serial = _DomainToken._next_serial
        _DomainToken._next_serial += 1

    def __repr__(self) -> str:
        return f"domain{self.serial}"


class Qubit(int):
    """An integer wire index carrying its allocator domain.

    ``Qubit`` is an ``int`` subclass, so it can be passed anywhere an integer
    wire is accepted.  The attached domain is retained by gates and layouts to
    catch mixing qubits allocated by unrelated :class:`QubitAllocator`
    instances.  Plain integers remain useful for callers that already own a
    circuit-wide numbering scheme.
    """

    domain: object

    def __new__(cls, value: int, domain: object) -> Qubit:  # noqa: PYI034
        value = _as_index(value)
        result = int.__new__(cls, value)
        result.domain = domain
        return result

    @property
    def index(self) -> int:
        return int(self)

    def __repr__(self) -> str:
        return f"q[{int(self)}]"


def _as_index(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("a boolean is not a valid qubit index")
    try:
        result = _index(value)
    except TypeError as exc:
        raise TypeError(f"qubit index must be an integer, got {value!r}") from exc
    if result < 0:
        raise ValueError(f"qubit index must be non-negative, got {result}")
    return result


def _qubit_domain(value: object) -> object | None:
    return getattr(value, "domain", None)


def _infer_domain(
    values: Iterable[object], explicit: object | None = None
) -> object | None:
    domains = {
        id(domain): domain
        for value in values
        if (domain := _qubit_domain(value)) is not None
    }
    if len(domains) > 1:
        raise ValueError("qubits from different circuit domains cannot be combined")
    inferred = next(iter(domains.values()), None)
    if explicit is not None and inferred is not None and explicit is not inferred:
        raise ValueError("qubit does not belong to the requested circuit domain")
    return explicit if explicit is not None else inferred


def _normalize_qubits(
    values: object, extra: tuple[object, ...] = ()
) -> tuple[QubitLike, ...]:
    if extra:
        raw: tuple[object, ...]
        if isinstance(values, (str, bytes)):
            raise TypeError("gate operands must be qubit indices")
        try:
            first: tuple[object, ...] = tuple(values)  # type: ignore[arg-type]
        except TypeError:
            first = (values,)
        raw = first + extra
    elif isinstance(values, (str, bytes)):
        raise TypeError("gate operands must be qubit indices")
    else:
        try:
            # A single integer is one operand; a Qubit is also an integer.
            raw = (values,) if isinstance(values, int) else tuple(values)  # type: ignore[arg-type]
        except TypeError:
            raw = (values,)
    result = tuple(
        _as_index(value) if not isinstance(value, Qubit) else value for value in raw
    )
    if len({int(value) for value in result}) != len(result):
        raise ValueError("a gate's operands must be distinct")
    return result


@dataclass(frozen=True, slots=True, init=False)
class Gate:
    """A validated self-inverse primitive gate.

    The constructor accepts either ``Gate("CX", (control, target))`` or
    ``Gate("CX", control, target)``.  Prefer the named constructors
    :meth:`x`, :meth:`cx`, :meth:`ccx`, and :meth:`mcx` in new code.
    """

    op: str
    qubits: tuple[QubitLike, ...]
    domain: object | None

    _ALIASES: ClassVar[dict[str, str]] = {
        "NOT": "X",
        "CNOT": "CX",
        "TOFFOLI": "CCX",
    }
    _ARITY: ClassVar[dict[str, int]] = {"X": 1, "CX": 2, "CCX": 3}

    def __init__(
        self,
        op: str,
        qubits: object = (),
        *extra: object,
        domain: object | None = None,
    ) -> None:
        if not isinstance(op, str):
            raise TypeError("gate operation must be a string")
        normalized = op.upper()
        normalized = self._ALIASES.get(normalized, normalized)
        if normalized not in {"X", "CX", "CCX", "MCX"}:
            raise ValueError(f"unsupported reversible gate {op!r}")
        operands = _normalize_qubits(qubits, extra)
        required = self._ARITY.get(normalized)
        if required is not None and len(operands) != required:
            raise ValueError(f"{normalized} requires exactly {required} operands")
        if normalized == "MCX" and len(operands) < 2:
            raise ValueError("MCX requires at least one control and one target")
        actual_domain = _infer_domain(operands, domain)
        object.__setattr__(self, "op", normalized)
        object.__setattr__(self, "qubits", operands)
        object.__setattr__(self, "domain", actual_domain)

    @classmethod
    def x(cls, target: QubitLike) -> Gate:
        return cls("X", target)

    @classmethod
    def cx(cls, control: QubitLike, target: QubitLike) -> Gate:
        return cls("CX", control, target)

    @classmethod
    def ccx(cls, control_a: QubitLike, control_b: QubitLike, target: QubitLike) -> Gate:
        return cls("CCX", control_a, control_b, target)

    @classmethod
    def mcx(cls, controls: Iterable[QubitLike], target: QubitLike) -> Gate:
        controls_tuple = tuple(controls)
        return cls("MCX", controls_tuple + (target,))

    # Convenient spelling for callers that use operation names as methods.
    not_ = x
    cnot = cx
    toffoli = ccx

    @property
    def name(self) -> str:
        return self.op

    @property
    def kind(self) -> str:
        return self.op

    @property
    def operation(self) -> str:
        return self.op

    @property
    def controls(self) -> tuple[QubitLike, ...]:
        if self.op == "X":
            return ()
        return self.qubits[:-1]

    @property
    def target(self) -> QubitLike:
        return self.qubits[-1]

    @property
    def is_mcx(self) -> bool:
        return self.op == "MCX"

    def inverse(self) -> Gate:
        """Return the inverse gate (all supported primitives are involutions)."""

        return self

    def on(self, *qubits: QubitLike) -> Gate:
        """Return this operation applied to a new operand tuple."""

        return Gate(self.op, qubits, domain=self.domain)

    def validate(self, num_qubits: int, domain: object | None = None) -> None:
        if num_qubits < 0:
            raise ValueError("the circuit width cannot be negative")
        if self.domain is not None and domain is not None and self.domain is not domain:
            raise ValueError("gate operands belong to a different circuit domain")
        for qubit in self.qubits:
            value = _as_index(qubit)
            if value >= num_qubits:
                raise ValueError(
                    f"qubit {value} is outside a {num_qubits}-qubit circuit"
                )

    def __str__(self) -> str:
        operands = ", ".join(f"q[{int(qubit)}]" for qubit in self.qubits)
        return f"{self.op} {operands}"

    def __repr__(self) -> str:
        return f"Gate({self.op!r}, {self.qubits!r})"


@dataclass(frozen=True, slots=True, init=False)
class CircuitLayout:
    """Named LSB-first register layout for a circuit.

    ``registers["a"][0]`` is the least-significant bit of register ``a``.
    A layout can be made directly from integer wire lists or obtained from a
    :class:`QubitAllocator`.  The mapping and all register tuples are frozen
    on construction, making a layout safe to share between immutable circuit
    fragments.
    """

    registers: Mapping[str, tuple[QubitLike, ...]]
    ancillas: tuple[QubitLike, ...]
    num_qubits: int
    domain: object | None

    def __init__(
        self,
        registers: Mapping[str, Iterable[QubitLike]] | None = None,
        ancillas: Iterable[QubitLike] = (),
        num_qubits: int | None = None,
        domain: object | None = None,
    ) -> None:
        raw_registers = registers or {}
        normalized: dict[str, tuple[QubitLike, ...]] = {}
        all_values: list[object] = []
        for name, values in raw_registers.items():
            if not isinstance(name, str) or not name:
                raise ValueError("register names must be non-empty strings")
            if name in normalized:
                raise ValueError(f"duplicate register {name!r}")
            register = _normalize_qubits(tuple(values))
            if len({int(value) for value in register}) != len(register):
                raise ValueError(f"register {name!r} contains a duplicate qubit")
            normalized[name] = register
            all_values.extend(register)

        normalized_ancillas = _normalize_qubits(tuple(ancillas))
        register_numeric = [
            int(value) for register in normalized.values() for value in register
        ]
        ancilla_numeric = [int(value) for value in normalized_ancillas]
        if len(set(ancilla_numeric)) != len(ancilla_numeric):
            raise ValueError("ancillas must not contain duplicate qubits")
        # A named register may intentionally be declared as an ancilla (for
        # example allocator.allocate_ancilla("carry")); overlap between the
        # two views is therefore allowed.  Duplicates within either view are
        # still rejected above and while registers are normalized.
        all_values.extend(normalized_ancillas)
        numeric = register_numeric + ancilla_numeric
        actual_domain = _infer_domain(all_values, domain)

        if num_qubits is None:
            width = max(numeric, default=-1) + 1
        else:
            width = _as_index(num_qubits)
            if numeric and max(numeric) >= width:
                raise ValueError("layout qubit is outside the declared circuit width")

        object.__setattr__(self, "registers", MappingProxyType(normalized))
        object.__setattr__(self, "ancillas", normalized_ancillas)
        object.__setattr__(self, "num_qubits", width)
        object.__setattr__(self, "domain", actual_domain)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.registers)

    @property
    def all_qubits(self) -> tuple[int, ...]:
        return tuple(range(self.num_qubits))

    @property
    def num_wires(self) -> int:
        return self.num_qubits

    def register(self, name: str) -> tuple[QubitLike, ...]:
        try:
            return self.registers[name]
        except KeyError as exc:
            raise KeyError(f"unknown register {name!r}") from exc

    def qubit(self, name: str, bit: int) -> QubitLike:
        register = self.register(name)
        bit_index = _as_index(bit)
        try:
            return register[bit_index]
        except IndexError as exc:
            raise IndexError(f"bit {bit_index} is outside register {name!r}") from exc

    def width(self, name: str) -> int:
        return len(self.register(name))

    def __getitem__(self, name: str) -> tuple[QubitLike, ...]:
        return self.register(name)

    def __iter__(self) -> Iterator[str]:
        return iter(self.registers)

    def __len__(self) -> int:
        return len(self.registers)

    def is_compatible(self, other: CircuitLayout) -> bool:
        if self.num_qubits != other.num_qubits:
            return False
        if {name: tuple(map(int, bits)) for name, bits in self.registers.items()} != {
            name: tuple(map(int, bits)) for name, bits in other.registers.items()
        }:
            return False
        if tuple(map(int, self.ancillas)) != tuple(map(int, other.ancillas)):
            return False
        return (
            self.domain is None or other.domain is None or self.domain is other.domain
        )

    def with_register(self, name: str, qubits: Iterable[QubitLike]) -> CircuitLayout:
        updated = dict(self.registers)
        updated[name] = tuple(qubits)
        return CircuitLayout(updated, self.ancillas, self.num_qubits, self.domain)

    def __repr__(self) -> str:
        registers = {
            name: tuple(map(int, bits)) for name, bits in self.registers.items()
        }
        return (
            f"CircuitLayout(registers={registers!r}, "
            f"ancillas={tuple(map(int, self.ancillas))!r}, "
            f"num_qubits={self.num_qubits})"
        )


class QubitAllocator:
    """Allocate integer-like qubits and assemble an LSB-first layout."""

    def __init__(self, start: int = 0) -> None:
        self._next = _as_index(start)
        self._domain = _DomainToken()
        self._registers: dict[str, tuple[Qubit, ...]] = {}
        self._ancillas: list[Qubit] = []

    @property
    def domain(self) -> object:
        return self._domain

    @property
    def num_qubits(self) -> int:
        return self._next

    def allocate(self, count: int = 1) -> Qubit | tuple[Qubit, ...]:
        count = _as_index(count)
        allocated = tuple(
            Qubit(self._next + offset, self._domain) for offset in range(count)
        )
        self._next += count
        if count == 1:
            return allocated[0]
        return allocated

    alloc = allocate

    def new_qubit(self) -> Qubit:
        value = self.allocate()
        assert isinstance(value, Qubit)
        return value

    allocate_qubit = new_qubit

    def allocate_register(self, name: str, width: int) -> tuple[Qubit, ...]:
        if name in self._registers:
            raise ValueError(f"register {name!r} is already allocated")
        width = _as_index(width)
        values = self.allocate(width)
        assert isinstance(values, tuple)
        self._registers[name] = values
        return values

    alloc_register = allocate_register
    register = allocate_register

    def allocate_ancilla(
        self, name: str | int | None = None, count: int = 1
    ) -> Qubit | tuple[Qubit, ...]:
        if isinstance(name, int) and not isinstance(name, bool):
            if count != 1:
                raise ValueError("ancilla count was supplied twice")
            count, name = name, None
        values = self.allocate(count)
        values_tuple = (values,) if isinstance(values, Qubit) else values
        self._ancillas.extend(values_tuple)
        if name is not None:
            if not isinstance(name, str):
                raise TypeError("named ancillas require a string name")
            if name in self._registers:
                raise ValueError(f"register {name!r} is already allocated")
            self._registers[name] = values_tuple
        return values

    alloc_ancilla = allocate_ancilla

    @property
    def layout(self) -> CircuitLayout:
        return CircuitLayout(
            self._registers,
            self._ancillas,
            num_qubits=self._next,
            domain=self._domain,
        )

    def __repr__(self) -> str:
        return f"QubitAllocator(num_qubits={self.num_qubits}, domain={self._domain!r})"


class ResourceCounts(Mapping[str, int]):
    """Deterministic primitive-gate and wire counts for a circuit."""

    _ALIASES: ClassVar[dict[str, str]] = {
        "gate_count": "gates",
        "total_gates": "gates",
        "qubit_count": "qubits",
        "wire_count": "qubits",
        "depth_count": "depth",
        "X": "x",
        "CX": "cx",
        "CCX": "ccx",
        "MCX": "mcx",
        "x_count": "x",
        "cx_count": "cx",
        "ccx_count": "ccx",
        "mcx_count": "mcx",
        "ancilla_count": "ancillas",
    }

    def __init__(self, values: Mapping[str, int]) -> None:
        self._values = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> int:
        canonical = self._ALIASES.get(key, key)
        return self._values[canonical]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> int:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def as_dict(self) -> dict[str, int]:
        return dict(self._values)

    def __call__(self) -> ResourceCounts:
        """Permit both ``circuit.resource_counts`` and ``...()`` spellings."""

        return self

    def __repr__(self) -> str:
        return f"ResourceCounts({dict(self._values)!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            try:
                return self.as_dict() == dict(other)
            except (TypeError, ValueError):
                return False
        return NotImplemented


@dataclass(frozen=True, slots=True, init=False)
class Circuit:
    """An immutable sequence of validated reversible gates."""

    gates: tuple[Gate, ...]
    num_qubits: int
    layout: CircuitLayout | None
    domain: object | None
    name: str | None
    optimization_level: int | None = field(default=None, compare=False)
    requested_optimization_level: int | None = field(default=None, compare=False)
    baseline: Circuit | None = field(default=None, compare=False, repr=False)
    _compiled: tuple[tuple[int, int, int], ...] = field(
        init=False, repr=False, compare=False
    )
    _fast_simulator: Callable[[int], int] | None = field(
        init=False, repr=False, compare=False
    )

    def __init__(
        self,
        gates: Iterable[Gate] | int = (),
        num_qubits: int | None = None,
        layout: CircuitLayout | Mapping[str, Iterable[QubitLike]] | None = None,
        domain: object | None = None,
        name: str | None = None,
        optimization_level: int | None = None,
        requested_optimization_level: int | None = None,
        baseline: Circuit | None = None,
    ) -> None:
        # ``Circuit(4)`` is a useful empty-circuit shorthand.
        if isinstance(gates, int) and num_qubits is None:
            num_qubits = _as_index(gates)
            gate_tuple: tuple[Gate, ...] = ()
        else:
            gate_tuple = tuple(gates)  # type: ignore[arg-type]
        if layout is not None and not isinstance(layout, CircuitLayout):
            layout = CircuitLayout(layout)

        max_gate_qubit = max(
            (int(q) for gate in gate_tuple for q in gate.qubits), default=-1
        )
        inferred_width = layout.num_qubits if layout is not None else max_gate_qubit + 1
        if num_qubits is None:
            width = inferred_width
        else:
            width = _as_index(num_qubits)
            if width < inferred_width:
                raise ValueError(
                    "gate or layout qubit is outside the declared circuit width"
                )

        domains = [
            candidate
            for candidate in (domain, layout.domain if layout else None)
            if candidate is not None
        ]
        for gate in gate_tuple:
            if not isinstance(gate, Gate):
                raise TypeError("circuits contain Gate instances")
            if gate.domain is not None:
                domains.append(gate.domain)
        actual_domain: object | None = domains[0] if domains else None
        if any(candidate is not actual_domain for candidate in domains[1:]):
            raise ValueError("circuit components belong to different circuit domains")
        for gate in gate_tuple:
            gate.validate(width, actual_domain)
        if layout is not None and layout.num_qubits != width:
            raise ValueError("circuit width and layout width must agree")

        object.__setattr__(self, "gates", gate_tuple)
        object.__setattr__(self, "num_qubits", width)
        object.__setattr__(self, "layout", layout)
        object.__setattr__(self, "domain", actual_domain)
        object.__setattr__(self, "name", name)
        if optimization_level is not None and (
            isinstance(optimization_level, bool)
            or not isinstance(optimization_level, int)
            or optimization_level < 0
        ):
            raise ValueError("optimization level must be a non-negative integer")
        object.__setattr__(self, "optimization_level", optimization_level)
        if requested_optimization_level is not None and (
            isinstance(requested_optimization_level, bool)
            or not isinstance(requested_optimization_level, int)
            or requested_optimization_level < 0
        ):
            raise ValueError(
                "requested optimization level must be a non-negative integer"
            )
        object.__setattr__(
            self, "requested_optimization_level", requested_optimization_level
        )
        if baseline is not None and not isinstance(baseline, Circuit):
            raise TypeError("baseline must be a qcc Circuit")
        object.__setattr__(self, "baseline", baseline)
        compiled: list[tuple[int, int, int]] = []
        operation_codes = {"X": 0, "CX": 1, "CCX": 2, "MCX": 3}
        for gate in gate_tuple:
            controls = 0
            for qubit in gate.controls:
                controls |= 1 << int(qubit)
            compiled.append((operation_codes[gate.op], controls, 1 << int(gate.target)))
        object.__setattr__(self, "_compiled", tuple(compiled))
        object.__setattr__(self, "_fast_simulator", None)

    @classmethod
    def empty(
        cls,
        num_qubits: int = 0,
        *,
        layout: CircuitLayout | None = None,
        name: str | None = None,
    ) -> Circuit:
        return cls((), num_qubits=num_qubits, layout=layout, name=name)

    @classmethod
    def from_gates(
        cls,
        gates: Iterable[Gate],
        *,
        num_qubits: int | None = None,
        layout: CircuitLayout | None = None,
        name: str | None = None,
    ) -> Circuit:
        return cls(gates, num_qubits=num_qubits, layout=layout, name=name)

    @property
    def ancillas(self) -> tuple[QubitLike, ...]:
        return () if self.layout is None else self.layout.ancillas

    @property
    def compiled_gates(self) -> tuple[tuple[int, int, int], ...]:
        """Return simulator-ready ``(opcode, controls, target)`` masks."""

        return self._compiled

    def fast_simulate(self, state: int) -> int:
        """Run a cached straight-line integer evaluator for this circuit.

        The ordinary simulator remains gate-oriented when tracing is enabled.
        For repeated basis-state differential tests this generated evaluator
        removes Python object dispatch from the hot loop without changing the
        circuit semantics or gate representation.
        """

        if self._fast_simulator is None:
            lines = ["def _run(state):"]
            for opcode, controls, target in self._compiled:
                if opcode == 0:
                    lines.append(f"    state ^= {target}")
                else:
                    lines.append(
                        f"    if state & {controls} == {controls}: state ^= {target}"
                    )
            lines.append("    return state")
            namespace: dict[str, Any] = {}
            exec("\n".join(lines), {}, namespace)
            object.__setattr__(self, "_fast_simulator", namespace["_run"])
        simulator = self._fast_simulator
        assert simulator is not None
        return simulator(state)

    def simulate(self, inputs: Any = None, **kwargs: Any) -> dict[str, Any]:
        """Simulate the circuit and return named registers and ancillas.

        The lower-level :func:`qcc.sim.classical.simulate` helper returns the
        complete integer wire state.  This method is the user-facing
        structured result: named registers are returned by name and declared
        ancillas are returned as a qubit-to-bit mapping.
        """

        from qcc.sim.classical import read_registers
        from qcc.sim.classical import simulate as run_classical

        result = run_classical(self, inputs, **kwargs)
        state = result[0] if isinstance(result, tuple) else result
        if self.layout is None:
            return {"state": int(state)}
        values: dict[str, Any] = read_registers(state, self)
        values["ancillas"] = {
            int(qubit): (state >> int(qubit)) & 1 for qubit in self.layout.ancillas
        }
        return values

    def to_qasm3(self, *, layout: Any | None = None, validate: bool = True) -> str:
        """Emit this circuit as validated OpenQASM 3 source."""

        from qcc.backend.qasm3 import to_qasm3

        return to_qasm3(self, layout=layout, validate=validate)

    def to_qiskit(self) -> Any:
        """Translate this circuit through the optional Qiskit adapter."""

        from qcc.qiskit import to_quantum_circuit

        return to_quantum_circuit(self)

    @property
    def qubits(self) -> tuple[int, ...]:
        return tuple(range(self.num_qubits))

    @property
    def n_qubits(self) -> int:
        return self.num_qubits

    @property
    def num_wires(self) -> int:
        return self.num_qubits

    @property
    def gate_counts(self) -> dict[str, int]:
        return {
            operation: self.resources()[operation]
            for operation in ("x", "cx", "ccx", "mcx")
        }

    def append(self, gate: Gate) -> Circuit:
        return Circuit(
            self.gates + (gate,),
            num_qubits=self.num_qubits,
            layout=self.layout,
            domain=self.domain,
            name=self.name,
            requested_optimization_level=self.requested_optimization_level,
        )

    def extend(self, gates: Iterable[Gate] | Circuit) -> Circuit:
        extra = gates.gates if isinstance(gates, Circuit) else tuple(gates)
        return Circuit(
            self.gates + tuple(extra),
            num_qubits=self.num_qubits,
            layout=self.layout,
            domain=self.domain,
            name=self.name,
            requested_optimization_level=self.requested_optimization_level,
        )

    def compose(self, other: Circuit) -> Circuit:
        """Concatenate circuits over the same wire domain."""

        if not isinstance(other, Circuit):
            raise TypeError("can only compose a Circuit with another Circuit")
        if self.num_qubits != other.num_qubits:
            raise ValueError("circuits must have the same number of qubits")
        if (
            self.domain is not None
            and other.domain is not None
            and self.domain is not other.domain
        ):
            raise ValueError("circuits belong to different qubit domains")
        if (
            self.layout is not None
            and other.layout is not None
            and not self.layout.is_compatible(other.layout)
        ):
            raise ValueError("circuits have incompatible layouts")
        layout = self.layout if self.layout is not None else other.layout
        domain = self.domain if self.domain is not None else other.domain
        return Circuit(
            self.gates + other.gates,
            num_qubits=self.num_qubits,
            layout=layout,
            domain=domain,
            name=self.name,
        )

    then = compose

    def inverse(self) -> Circuit:
        """Return the inverse circuit by reversing gate order."""

        return Circuit(
            tuple(gate.inverse() for gate in reversed(self.gates)),
            num_qubits=self.num_qubits,
            layout=self.layout,
            domain=self.domain,
            name=self.name,
            optimization_level=self.optimization_level,
            requested_optimization_level=self.requested_optimization_level,
            baseline=self.baseline,
        )

    def decompose_mcx(self) -> Circuit:
        """Return an equivalent X/CX/CCX circuit using clean layout ancillas.

        A gate with ``k`` controls uses ``k - 2`` declared clean ancillas as a
        conjunction ladder.  Each ladder is uncomputed before the next gate,
        so the same workspace can safely be reused across the circuit.
        """

        if not any(gate.op == "MCX" for gate in self.gates):
            return self
        ancillas = () if self.layout is None else tuple(self.layout.ancillas)
        ancilla_set = {int(qubit) for qubit in ancillas}
        touched_ancillas: set[int] = set()
        expanded: list[Gate] = []
        for gate in self.gates:
            if gate.op != "MCX":
                expanded.append(gate)
                touched_ancillas.update(
                    int(qubit) for qubit in gate.qubits if int(qubit) in ancilla_set
                )
                continue
            controls = tuple(gate.controls)
            target = gate.target
            if len(controls) == 1:
                expanded.append(Gate.cx(controls[0], target))
                touched_ancillas.update(
                    int(qubit) for qubit in gate.qubits if int(qubit) in ancilla_set
                )
                continue
            if len(controls) == 2:
                expanded.append(Gate.ccx(controls[0], controls[1], target))
                touched_ancillas.update(
                    int(qubit) for qubit in gate.qubits if int(qubit) in ancilla_set
                )
                continue
            required = len(controls) - 2
            operand_set = {int(operand) for operand in gate.qubits}
            work = tuple(
                qubit
                for qubit in ancillas
                if int(qubit) not in touched_ancillas and int(qubit) not in operand_set
            )[:required]
            if len(work) != required:
                raise ValueError(
                    f"MCX with {len(controls)} controls requires {required} "
                    "clean ancilla qubits in the circuit layout at this point"
                )
            operands = (*controls, target, *work)
            if len(set(operands)) != len(operands):
                raise ValueError("MCX clean workspace overlaps its operands")
            ladder = [Gate.ccx(controls[0], controls[1], work[0])]
            for index in range(2, len(controls) - 1):
                ladder.append(
                    Gate.ccx(work[index - 2], controls[index], work[index - 1])
                )
            ladder.append(Gate.ccx(work[-1], controls[-1], target))
            expanded.extend(ladder)
            expanded.extend(reversed(ladder[:-1]))
            touched_ancillas.update(
                int(qubit) for qubit in gate.qubits if int(qubit) in ancilla_set
            )
        return Circuit(
            tuple(expanded),
            num_qubits=self.num_qubits,
            layout=self.layout,
            domain=self.domain,
            name=self.name,
            optimization_level=self.optimization_level,
            requested_optimization_level=self.requested_optimization_level,
            baseline=self.baseline,
        )

    invert = inverse

    def validate(self) -> Circuit:
        for gate in self.gates:
            gate.validate(self.num_qubits, self.domain)
        return self

    def _depth(self) -> int:
        last_use = [-1] * self.num_qubits
        depth = 0
        for gate in self.gates:
            layer = 1 + max((last_use[int(qubit)] for qubit in gate.qubits), default=-1)
            for qubit in gate.qubits:
                last_use[int(qubit)] = layer
            depth = max(depth, layer + 1)
        return depth

    def resources(self) -> ResourceCounts:
        counts = {"x": 0, "cx": 0, "ccx": 0, "mcx": 0}
        for gate in self.gates:
            counts[gate.op.lower()] += 1
        values = {
            "gates": len(self.gates),
            "qubits": self.num_qubits,
            "depth": self._depth(),
            "ancillas": len(self.ancillas),
            **counts,
        }
        return ResourceCounts(values)

    @property
    def resource_counts(self) -> ResourceCounts:
        return self.resources()

    def optimize(self, level: int = 2, *, opt: int | None = None) -> Circuit:
        """Return a v0.4-optimized copy while preserving the circuit layout."""

        from qcc.opt.gates import optimize_circuit

        selected = level if opt is None else opt
        return optimize_circuit(self, selected)

    @property
    def optimizer(self) -> int | None:
        """Return the optimizer level used to produce this circuit, if known."""

        return self.optimization_level

    @property
    def requested_optimizer(self) -> int | None:
        """Return the optimizer level requested by the compiler, if known."""

        return self.requested_optimization_level

    def report(self, *, opt: int | None = None) -> Any:
        """Return the backend-neutral resource report for this circuit."""

        from qcc.backend.report import resource_report
        from qcc.opt.gates import normalize_level

        target = self if opt is None else self.optimize(opt)
        return resource_report(
            target,
            requested_level=(
                target.requested_optimization_level
                if opt is None
                else normalize_level(opt)
            ),
            before=target.baseline,
        )

    resource_report = report

    @property
    def depth(self) -> int:
        return self._depth()

    @property
    def gate_count(self) -> int:
        return len(self.gates)

    def to_builder(self) -> CircuitBuilder:
        return CircuitBuilder.from_circuit(self)

    def pretty(self) -> str:
        header = (
            f"Circuit(qubits={self.num_qubits}, gates={len(self.gates)}, "
            f"depth={self.depth})"
        )
        if not self.gates:
            return header
        lines = [header]
        lines.extend(
            f"  {number:04d}: {gate}" for number, gate in enumerate(self.gates)
        )
        return "\n".join(lines)

    pretty_print = pretty

    def __iter__(self) -> Iterator[Gate]:
        return iter(self.gates)

    def __len__(self) -> int:
        return len(self.gates)

    def __getitem__(self, item: int | slice) -> Gate | tuple[Gate, ...]:
        return self.gates[item]

    def __add__(self, other: Circuit) -> Circuit:
        return self.compose(other)

    def __str__(self) -> str:
        return self.pretty()

    def __repr__(self) -> str:
        return f"Circuit(gates={self.gates!r}, num_qubits={self.num_qubits})"


class CircuitBuilder:
    """Mutable gate appender that produces immutable :class:`Circuit` values."""

    def __init__(
        self,
        num_qubits: int | CircuitLayout | QubitAllocator = 0,
        *,
        layout: CircuitLayout | Mapping[str, Iterable[QubitLike]] | None = None,
        allocator: QubitAllocator | None = None,
        name: str | None = None,
        auto_grow: bool | None = None,
    ) -> None:
        if isinstance(num_qubits, QubitAllocator):
            if allocator is not None:
                raise ValueError("allocator was supplied twice")
            allocator = num_qubits
            num_qubits = allocator.num_qubits
            if layout is None:
                layout = allocator.layout
        elif isinstance(num_qubits, CircuitLayout):
            if layout is not None:
                raise ValueError("layout was supplied twice")
            layout = num_qubits
            num_qubits = layout.num_qubits
        if allocator is not None and layout is None:
            layout = allocator.layout
        if layout is not None and not isinstance(layout, CircuitLayout):
            layout = CircuitLayout(layout)
        requested_width = _as_index(num_qubits)
        if layout is not None:
            if requested_width not in (0, layout.num_qubits):
                raise ValueError("builder width and layout width must agree")
            requested_width = layout.num_qubits
        self._num_qubits = requested_width
        self._layout = layout
        self._domain = (
            layout.domain
            if layout is not None
            else (allocator.domain if allocator else None)
        )
        self._allocator = allocator
        self._name = name
        self._gates: list[Gate] = []
        self._auto_grow = (
            (layout is None and requested_width == 0)
            if auto_grow is None
            else auto_grow
        )

    @classmethod
    def from_circuit(cls, circuit: Circuit) -> CircuitBuilder:
        builder = cls(
            circuit.num_qubits,
            layout=circuit.layout,
            name=circuit.name,
            auto_grow=False,
        )
        builder._domain = circuit.domain
        builder._gates.extend(circuit.gates)
        return builder

    @property
    def num_qubits(self) -> int:
        return self._num_qubits

    @property
    def n_qubits(self) -> int:
        return self._num_qubits

    @property
    def layout(self) -> CircuitLayout | None:
        return self._layout

    @property
    def allocator(self) -> QubitAllocator | None:
        return self._allocator

    @property
    def domain(self) -> object | None:
        return self._domain

    @property
    def gates(self) -> tuple[Gate, ...]:
        return tuple(self._gates)

    def _check_gate(self, gate: Gate) -> None:
        if not isinstance(gate, Gate):
            raise TypeError("append expects a Gate")
        if (
            gate.domain is not None
            and self._domain is not None
            and gate.domain is not self._domain
        ):
            raise ValueError("gate operands belong to a different circuit domain")
        if gate.domain is not None and self._domain is None:
            self._domain = gate.domain
        largest = max((int(qubit) for qubit in gate.qubits), default=-1)
        if largest >= self._num_qubits:
            if self._auto_grow and self._layout is None:
                self._num_qubits = largest + 1
            else:
                raise ValueError(
                    f"qubit {largest} is outside a {self._num_qubits}-qubit builder"
                )
        gate.validate(self._num_qubits, self._domain)

    def append(self, gate: Gate) -> CircuitBuilder:
        self._check_gate(gate)
        self._gates.append(gate)
        return self

    add = append
    add_gate = append

    def extend(self, gates: Iterable[Gate] | Circuit) -> CircuitBuilder:
        if isinstance(gates, Circuit):
            if gates.num_qubits > self._num_qubits and not self._auto_grow:
                raise ValueError("fragment is wider than the builder")
            if (
                self._domain is not None
                and gates.domain is not None
                and self._domain is not gates.domain
            ):
                raise ValueError("fragment belongs to a different circuit domain")
            for gate in gates:
                self.append(gate)
            return self
        for gate in gates:
            self.append(gate)
        return self

    def x(self, target: QubitLike) -> CircuitBuilder:
        return self.append(Gate.x(target))

    def cx(self, control: QubitLike, target: QubitLike) -> CircuitBuilder:
        return self.append(Gate.cx(control, target))

    def ccx(
        self, control_a: QubitLike, control_b: QubitLike, target: QubitLike
    ) -> CircuitBuilder:
        return self.append(Gate.ccx(control_a, control_b, target))

    def mcx(self, controls: Iterable[QubitLike], target: QubitLike) -> CircuitBuilder:
        return self.append(Gate.mcx(controls, target))

    def allocate_clean_ancillas(self, count: int) -> tuple[int, ...]:
        """Extend the builder with clean workspace wires for a library block."""

        count = _as_index(count)
        bits = tuple(range(self._num_qubits, self._num_qubits + count))
        self._num_qubits += count
        if self._layout is None:
            registers: Mapping[str, Iterable[QubitLike]] = {}
            old_ancillas: tuple[QubitLike, ...] = ()
        else:
            registers = self._layout.registers
            old_ancillas = self._layout.ancillas
        self._layout = CircuitLayout(
            registers,
            ancillas=(*old_ancillas, *bits),
            num_qubits=self._num_qubits,
            domain=self._domain,
        )
        return bits

    allocate_workspace = allocate_clean_ancillas

    # Explicit add_* spellings are convenient in lowering code.
    add_x = x
    add_cx = cx
    add_ccx = ccx
    add_mcx = mcx

    def compose(self, circuit: Circuit) -> CircuitBuilder:
        """Append an immutable fragment over this builder's wire domain."""

        return self.extend(circuit)

    then = compose

    def build(self) -> Circuit:
        return Circuit(
            tuple(self._gates),
            num_qubits=self._num_qubits,
            layout=self._layout,
            domain=self._domain,
            name=self._name,
        )

    to_circuit = build

    @property
    def circuit(self) -> Circuit:
        return self.build()

    def inverse(self) -> Circuit:
        return self.build().inverse()

    def clear(self) -> CircuitBuilder:
        self._gates.clear()
        return self

    def __len__(self) -> int:
        return len(self._gates)

    def __iter__(self) -> Iterator[Gate]:
        return iter(self._gates)

    def __str__(self) -> str:
        return self.build().pretty()


__all__ = [
    "Circuit",
    "CircuitBuilder",
    "CircuitLayout",
    "Gate",
    "Qubit",
    "QubitAllocator",
    "QubitLike",
    "ResourceCounts",
]
