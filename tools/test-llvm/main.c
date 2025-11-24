// main.c
#include <stdio.h>

extern int add_i32(int, int);

int main(void) {
	int r = add_i32(40, 2);
	printf("add_i32(40, 2) = %d\n", r);
	return 0;
}
