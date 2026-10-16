#include <stdint.h>
#include <stdlib.h>

uint8_t uses_malloc(uint8_t value) {
    void *storage = malloc((size_t)value + 1u); /* QCC_UNSUPPORTED: malloc */
    return (uint8_t)(uintptr_t)storage;
}
