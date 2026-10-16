#include <stdbool.h>
#include <stdint.h>

/* Representative v0.3 functions used by scripts/benchmarks.py. */
uint8_t add(uint8_t left, uint8_t right) {
    return (uint8_t)(left + right);
}

uint16_t multiply(uint8_t left, uint8_t right) {
    return (uint16_t)left * (uint16_t)right;
}

uint8_t compare(uint8_t left, uint8_t right) {
    return (uint8_t)(left < right);
}

uint8_t popcount(uint8_t value) {
    return (uint8_t)(
        (value & 1u) +
        ((value >> 1) & 1u) +
        ((value >> 2) & 1u) +
        ((value >> 3) & 1u) +
        ((value >> 4) & 1u) +
        ((value >> 5) & 1u) +
        ((value >> 6) & 1u) +
        ((value >> 7) & 1u)
    );
}

bool is_solution(uint8_t value) {
    return (uint8_t)(value * value + 3) % 17 == 5;
}
