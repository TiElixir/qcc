#include <stdint.h>

uint8_t unresolved_pointer(const uint8_t *values, uint8_t index) {
    return values[index]; /* QCC_UNSUPPORTED: unresolved pointer */
}
