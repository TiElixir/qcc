#include <stdint.h>

uint8_t uses_function_pointer(uint8_t value, uint8_t (*transform)(uint8_t)) { /* QCC_UNSUPPORTED: function pointer */
    return transform(value);
}
