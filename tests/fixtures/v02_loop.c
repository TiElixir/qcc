#include <stdint.h>

uint8_t popcount8(uint8_t value) {
    uint8_t count = 0;
    for (uint16_t bit = 0; bit < 8u; ++bit) {
        count += (uint8_t)((value >> bit) & 1u);
    }
    return count;
}
