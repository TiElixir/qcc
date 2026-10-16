#include <stdint.h>

uint8_t recursive_value(uint8_t value) {
    if (value == 0u) {
        return 0;
    }
    return (uint8_t)(value + recursive_value((uint8_t)(value - 1u))); /* QCC_UNSUPPORTED: recursion */
}
