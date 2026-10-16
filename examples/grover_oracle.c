#include <stdint.h>

/*
 * A small bit-flip oracle: applying it twice restores the input.  A host
 * Grover driver can use the returned mask as the oracle work register.
 */
uint8_t grover_oracle(uint8_t x) {
    return (uint8_t)(x ^ (uint8_t)0x2a);
}
