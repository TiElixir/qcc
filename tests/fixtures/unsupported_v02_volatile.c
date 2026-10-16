#include <stdint.h>

uint8_t uses_volatile(uint8_t value) {
    volatile uint8_t storage = value; /* QCC_UNSUPPORTED: volatile */
    return storage;
}
