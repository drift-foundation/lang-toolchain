#include <stdint.h>
#include <stddef.h>

/* Simulates SSL_get0_alpn_selected: writes a pointer and length via out-params */
static const unsigned char alpn_data[] = { 'h', '2' };

void get_alpn_selected(void *ssl, const unsigned char **out_data, unsigned int *out_len) {
    (void)ssl;
    *out_data = alpn_data;
    *out_len = 2;
}

/* Simple out-param: write an int via pointer */
void write_int(int *out, int value) {
    *out = value;
}
