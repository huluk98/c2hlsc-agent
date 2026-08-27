void accum(const int *in, int *out, int n) {
    int s = 0;
    for (int i = 0; i < n; i++) { s += in[i]; out[i] = s; }
}
