# qcc examples

These examples use the deliberately small fixed-width C subset accepted by
qcc.  The compiler treats `uint8_t` arithmetic modulo 256 and lowers supported
expressions to reversible `X`, `CX`, and `CCX` gates.

Compile the modular adder to OpenQASM 3:

```console
$ qcc compile examples/add.c --function add --emit qasm3 -o add.qasm
```

Inspect resource counts or validate the generated circuit:

```console
$ qcc stats examples/add.c --function add
$ qcc check examples/add.c --function add
```

`grover_oracle.c` is the bit-flip oracle used by `grover_demo.py`.  The demo's
motivating predicate is

```text
(x * x + 3) % 17 == 5
```

over the three-bit domain `0 <= x < 8`, where the only solution is `x == 6`.
`grover_oracle.c` records that motivating predicate in its source comment and
uses the equivalent `x == 6` entry point because the v0.2 compiler deliberately
does not lower multiplication or remainder.  qcc compiles it to a reversible
circuit that XORs the predicate into a one-bit output register.
The host prepares that output in `|->`, so the bit flip becomes a phase oracle.

Run the deterministic host demo without optional dependencies:

```console
$ PYTHONPATH=src python examples/grover_demo.py
```

Install the optional Qiskit integration to run the statevector path (and
install `qiskit-aer` as well if Aer sampling is desired):

```console
$ python -m pip install -e ".[qiskit]"
$ python -m pip install qiskit-aer
$ PYTHONPATH=src python examples/grover_demo.py
```

The examples are source inputs, not standalone executables; qcc invokes Clang
and compiles the selected function.  Use `--dump-ir` to inspect the textual
LLVM IR received by the lowering pipeline.
