#include <stdint.h>

uint8_t udiv_zero8(uint8_t value) {
    return (uint8_t)(value / 0u);
}

uint8_t urem_zero8(uint8_t value) {
    return (uint8_t)(value % 0u);
}
