CC ?= cc
CFLAGS ?= -O2 -fPIC -Wall -Wextra -std=c99

OBJS = diagnostic_runtime.o error_dummy.o

all: libdrift_runtime.a

libdrift_runtime.a: $(OBJS)
	ar rcs $@ $(OBJS)

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

clean:
	rm -f $(OBJS) libdrift_runtime.a
