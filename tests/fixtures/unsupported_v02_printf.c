#include <stdint.h>
#include <stdio.h>

uint8_t uses_printf(uint8_t value) {
    (void)printf("value=%u\\n", (unsigned)value); /* QCC_UNSUPPORTED: printf I/O */
    return value;
}
