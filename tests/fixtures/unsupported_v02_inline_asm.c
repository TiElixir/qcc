#include <stdint.h>

uint8_t uses_inline_asm(uint8_t value) {
    uint8_t result;
    __asm__ volatile("movb %1, %0" : "=q"(result) : "q"(value)); /* QCC_UNSUPPORTED: inline asm */
    return result;
}
