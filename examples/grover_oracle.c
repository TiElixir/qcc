#include <stdint.h>

/*
 * The demo motivates the search with
 *
 *     (x * x + 3) % 17 == 5.
 *
 * On the three-bit domain [0, 7] this predicate has the single solution
 * x == 6.  qcc v0.2 deliberately does not lower multiplication or remainder,
 * so the selected entry point below spells the equivalent bounded predicate
 * using the supported equality operation.
 */
typedef unsigned _BitInt(3) grover_input_t;
typedef unsigned _BitInt(1) grover_output_t;

grover_output_t grover_oracle(grover_input_t x) {
    return x == 6;
}
