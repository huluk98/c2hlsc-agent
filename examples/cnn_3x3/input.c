#include <stdint.h>

/*
 * Single-channel 3x3 CNN convolution layer ("same" zero padding, stride 1,
 * CNN convention: cross-correlation, no kernel flip).
 *
 *   out[r][c] = sum_{i,j in 0..2} in[r+i-1][c+j-1] * kernel[i][j]
 *
 * Bit-width analysis (signed int8 input x signed int8 kernel):
 *   product  : [-128*127, -128*-128] = [-16256, 16384]  -> 16 bits signed
 *   sum of 9 : [9*-16256, 9*16384]   = [-146304, 147456] -> 19 bits signed
 * The interface carries the accumulator in int32_t; the value always fits
 * in a signed 19-bit word (ap_int<19> / signed [18:0] in RTL).
 */
void cnn_conv3x3(const int8_t *in, const int8_t *kernel, int32_t *out) {
  for (int r = 0; r < 3; ++r) {
    for (int c = 0; c < 3; ++c) {
      int32_t acc = 0;
      for (int i = 0; i < 3; ++i) {
        for (int j = 0; j < 3; ++j) {
          int rr = r + i - 1;
          int cc = c + j - 1;
          if (rr >= 0 && rr < 3 && cc >= 0 && cc < 3) {
            acc += (int32_t)in[rr * 3 + cc] * (int32_t)kernel[i * 3 + j];
          }
        }
      }
      out[r * 3 + c] = acc;
    }
  }
}
