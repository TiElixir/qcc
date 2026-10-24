#include <stdint.h>
#include <stdbool.h>

/*
 * The demo motivates the search with
 *
 *     (x * x + 3) % 17 == 5.
 *
 * On the three-bit domain [0, 7] this predicate has the single solution
 * x == 6. The public is_solution entry point keeps the exact motivating
 * expression; the demo entry point uses the three-bit equivalent so the
 * optional statevector host remains small enough for local examples.
 */
bool is_solution(uint8_t x) {
    return (uint8_t)(x * x + 3) % 17 == 5;
}

typedef unsigned _BitInt(3) grover_input_t;
typedef unsigned _BitInt(1) grover_output_t;

grover_output_t grover_oracle(grover_input_t x) {
    return x == 6;
}
