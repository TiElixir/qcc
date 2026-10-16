#include <stdint.h>

uint8_t nested_if_else(uint8_t x, uint8_t y) {
    if (x == y) {
        if (x < 128u) {
            return (uint8_t)(x + 1u);
        }
        return (uint8_t)(y - 1u);
    }
    if (x < y) {
        if ((x & 1u) == 0u) {
            return x;
        }
        return y;
    }
    if ((y & 1u) == 0u) {
        return (uint8_t)(y + 1u);
    }
    return (uint8_t)(x - 1u);
}
