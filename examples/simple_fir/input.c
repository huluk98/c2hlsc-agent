#include <stdint.h>

void simple_fir(const int16_t *x, const int16_t *coef, int32_t *y, int n) {
  for (int i = 0; i < n; ++i) {
    int32_t acc = 0;
    for (int tap = 0; tap < 4; ++tap) {
      int idx = i - tap;
      int16_t sample = idx >= 0 ? x[idx] : 0;
      acc += (int32_t)sample * (int32_t)coef[tap];
    }
    y[i] = acc;
  }
}
