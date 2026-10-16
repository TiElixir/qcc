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

`grover_oracle.c` contains a self-inverse bit-flip oracle.  It XORs a fixed
mask into its input, so applying the generated circuit twice returns every
qubit to its original value.  It is a useful small oracle fixture for a host
program that supplies a separate work/phase register.

The examples are source inputs, not standalone executables; qcc invokes Clang
and compiles the selected function.  Use `--dump-ir` to inspect the textual
LLVM IR received by the lowering pipeline.
