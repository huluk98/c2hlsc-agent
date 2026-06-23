#include <stdint.h>

uint32_t bit_mix(int32_t a, uint32_t b, int shift) {
  uint32_t rotated = (b << (shift & 7)) | (b >> ((8 - shift) & 7));
  uint32_t masked = ((uint32_t)a & 0x00ff00ffu) ^ rotated;
  return masked + (uint32_t)(a >> 3);
}
