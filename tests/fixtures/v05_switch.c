#include <stdint.h>

/* Multi-way choices.  Clang emits an LLVM switch for every one of these,
 * including the ones written without the switch keyword. */

uint8_t if_chain(uint8_t a) {
    if (a == 0) return 7;
    else if (a == 1) return 9;
    else if (a == 5) return 30;
    else if (a == 9) return 77;
    else if (a == 13) return 2;
    else if (a == 40) return 91;
    else if (a == 41) return 3;
    return 1;
}

uint8_t ternary_chain(uint8_t a) {
    return a == 3 ? 17 : a == 200 ? 4 : a == 77 ? 250 : a == 78 ? 9 : 1;
}

uint8_t two_cases(uint8_t a, uint8_t b) {
    switch (a) {
    case 3: return (uint8_t)(b + 1);
    case 200: return (uint8_t)(b * 3);
    default: return b;
    }
}

/* Every value of the selector has a case, so the default is unreachable. */
uint8_t full_cover(uint8_t a, uint8_t b) {
    switch (a & 7) {
    case 0: return (uint8_t)(b + 1);
    case 1: return (uint8_t)(b - 1);
    case 2: return (uint8_t)(b ^ 0x55);
    case 3: return (uint8_t)(b << 1);
    case 4: return (uint8_t)(b >> 1);
    case 5: return (uint8_t)(~b);
    case 6: return (uint8_t)(b & 0x0F);
    default: return b;
    }
}

/* Several case values share one target, and the arms meet in a phi. */
uint8_t shared_targets(uint8_t a, uint8_t b) {
    uint8_t result = b;
    switch (a) {
    case 1:
    case 2:
    case 7:
        result = (uint8_t)(b + a);
        break;
    case 100:
        result = (uint8_t)(b * 5);
        break;
    case 101:
    case 250:
        result = (uint8_t)(b ^ a);
        break;
    }
    return (uint8_t)(result + 3);
}

uint8_t nested(uint8_t a, uint8_t b) {
    if (b > 100) {
        switch (a) {
        case 0: return 11;
        case 9: return (uint8_t)(b - 100);
        case 60: return 13;
        default: return a;
        }
    }
    return (uint8_t)(a ^ b);
}
