#include <stdint.h>

uint8_t min8(uint8_t a, uint8_t b) {
    return a < b ? a : b;
}

uint8_t max8(uint8_t a, uint8_t b) {
    return a > b ? a : b;
}

uint8_t abs_diff8(uint8_t a, uint8_t b) {
    if (a >= b) {
        return (uint8_t)(a - b);
    }
    return (uint8_t)(b - a);
}

uint8_t clamp8(uint8_t value, uint8_t lower, uint8_t upper) {
    if (value < lower) {
        return lower;
    }
    if (value > upper) {
        return upper;
    }
    return value;
}
