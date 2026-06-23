#include <stdint.h>

void vector_add(const int32_t *a, const int32_t *b, int32_t *out, int n) {
  for (int i = 0; i < n; ++i) {
    out[i] = a[i] + b[i];
  }
}
