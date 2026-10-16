#include <stdint.h>

uint16_t constant_shifts(uint8_t value) {
    uint8_t left = (uint8_t)(value << 3);
    uint8_t right = (uint8_t)(value >> 2);
    return (uint16_t)left | ((uint16_t)right << 8);
}
