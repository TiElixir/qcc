#include <stdint.h>

uint16_t all_icmp_predicates(uint8_t a, uint8_t b) {
    uint16_t result = 0;
    result |= (uint16_t)((a == b) ? 1u : 0u) << 0;
    result |= (uint16_t)((a != b) ? 1u : 0u) << 1;
    result |= (uint16_t)((a > b) ? 1u : 0u) << 2;
    result |= (uint16_t)((a >= b) ? 1u : 0u) << 3;
    result |= (uint16_t)((a < b) ? 1u : 0u) << 4;
    result |= (uint16_t)((a <= b) ? 1u : 0u) << 5;
    result |= (uint16_t)(((int8_t)a > (int8_t)b) ? 1u : 0u) << 6;
    result |= (uint16_t)(((int8_t)a >= (int8_t)b) ? 1u : 0u) << 7;
    result |= (uint16_t)(((int8_t)a < (int8_t)b) ? 1u : 0u) << 8;
    result |= (uint16_t)(((int8_t)a <= (int8_t)b) ? 1u : 0u) << 9;
    return result;
}
