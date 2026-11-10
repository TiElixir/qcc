#include <stdint.h>
#include <stdio.h>

/* One supported function next to several that are not. */

uint8_t pure_add(uint8_t a, uint8_t b) { return (uint8_t)(a + b); }

uint8_t logs_value(uint8_t value) {
    (void)printf("value=%u\n", (unsigned)value);
    return value;
}

uint8_t factorial(uint8_t n) {
    if (n == 0u) {
        return 1;
    }
    return (uint8_t)(n * factorial((uint8_t)(n - 1u)));
}

uint8_t reads_memory(const uint8_t *values, uint8_t index) {
    return values[index];
}

uint8_t calls_logger(uint8_t value) { return (uint8_t)(logs_value(value) + 1u); }
