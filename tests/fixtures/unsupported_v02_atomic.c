#include <stdint.h>
#include <stdatomic.h>

uint8_t uses_atomic(uint8_t value) {
    _Atomic uint8_t storage = value; /* QCC_UNSUPPORTED: atomic */
    return atomic_fetch_add(&storage, 1u);
}
