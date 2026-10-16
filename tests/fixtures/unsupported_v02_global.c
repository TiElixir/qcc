#include <stdint.h>

uint8_t mutable_state = 7u; /* QCC_UNSUPPORTED: global mutable state */

uint8_t reads_global(uint8_t value) {
    mutable_state = (uint8_t)(mutable_state + value);
    return mutable_state;
}
