/* deliberately unbounded pointers: the analyzer defaults them to 16 with a warning */
void scale(const int *src, int *dst, int count, int factor) {
    for (int i = 0; i < count; i++) {
        dst[i] = src[i] * factor;
    }
}
