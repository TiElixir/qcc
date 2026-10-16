#include <stdint.h>

uint16_t multiply8(uint8_t a, uint8_t b) {
    return (uint16_t)a * (uint16_t)b;
}

uint8_t udiv3_8(uint8_t value) {
    return (uint8_t)(value / 3u);
}

uint8_t urem3_8(uint8_t value) {
    return (uint8_t)(value % 3u);
}

uint8_t udiv5_8(uint8_t value) {
    return (uint8_t)(value / 5u);
}

uint8_t urem5_8(uint8_t value) {
    return (uint8_t)(value % 5u);
}

uint8_t udiv255_8(uint8_t value) {
    return (uint8_t)(value / 255u);
}

uint8_t urem255_8(uint8_t value) {
    return (uint8_t)(value % 255u);
}
