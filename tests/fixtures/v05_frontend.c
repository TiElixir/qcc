#include <stdint.h>

/* Functions that newer Clang lowers to calls and integer intrinsics. */

static uint8_t square8(uint8_t x) { return (uint8_t)(x * x); }

uint8_t helper_call(uint8_t a, uint8_t b) {
    return (uint8_t)(square8(a) + square8(b));
}

__attribute__((noinline)) uint8_t pick8(uint8_t a, uint8_t b) {
    if (a > b) {
        return (uint8_t)(a - b);
    }
    return (uint8_t)(b ^ a);
}

uint8_t branching_call(uint8_t a, uint8_t b) {
    return (uint8_t)(pick8(a, b) + pick8(b, 3));
}

uint8_t variable_shl(uint8_t a, uint8_t b) { return (uint8_t)(a << (b & 7)); }

uint8_t variable_shr(uint8_t a, uint8_t b) { return (uint8_t)(a >> (b & 7)); }

uint8_t rotate_left(uint8_t a, uint8_t b) {
    b &= 7;
    return (uint8_t)((a << b) | (a >> ((8 - b) & 7)));
}

uint8_t popcount_builtin(uint8_t a) { return (uint8_t)__builtin_popcount(a); }

uint8_t leading_zeros(uint8_t a) { return a ? (uint8_t)__builtin_clz(a) : 99; }

uint8_t trailing_zeros(uint8_t a) { return a ? (uint8_t)__builtin_ctz(a) : 77; }

uint16_t byte_swap(uint16_t a) { return __builtin_bswap16(a); }

int8_t absolute(int8_t a) { return (int8_t)(a < 0 ? -a : a); }

int8_t signed_max(int8_t a, int8_t b) { return a > b ? a : b; }

uint8_t saturating_sub(uint8_t a, uint8_t b) {
    return a > b ? (uint8_t)(a - b) : 0;
}
