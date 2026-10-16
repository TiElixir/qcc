#include <stdint.h>

uint8_t self_inverse(uint8_t x) {
    return (uint8_t)(x ^ (uint8_t)0x5a);
}
