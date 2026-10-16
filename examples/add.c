#include <stdint.h>

/* A fixed-width modular addition suitable for qcc's reversible subset. */
uint8_t add(uint8_t a, uint8_t b) {
    return (uint8_t)(a + b);
}
