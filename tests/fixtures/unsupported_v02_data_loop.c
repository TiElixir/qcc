#include <stdint.h>

uint8_t data_dependent_loop(uint8_t limit) {
    uint8_t result = 0;
    for (uint16_t index = 0; index < limit; ++index) { /* QCC_UNSUPPORTED: data-dependent loop */
        result ^= (uint8_t)index;
    }
    return result;
}
