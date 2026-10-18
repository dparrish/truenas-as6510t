#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/io.h>

static int parse_uint(const char *text, unsigned *out) {
    char *end = NULL;
    unsigned long value = strtoul(text, &end, 0);
    if (!text[0] || !end || *end || value > 255) return -1;
    *out = (unsigned)value;
    return 0;
}
static int ensure_output(unsigned gpio) {
    unsigned port = 0x2e;
    unsigned reg = 0xc8u + gpio / 8u;
    unsigned mask = 1u << (gpio % 8u);
    if (ioperm(port, 2, 1) != 0) return -1;
    outb(0x87, port);
    outb(0x01, port);
    outb(0x55, port);
    outb(0x55, port);
    outb(0x07, port);
    outb(0x07, port + 1);
    outb((unsigned char)reg, port);
    unsigned current = inb(port + 1);
    outb((unsigned char)(current | mask), port + 1);
    outb(0x02, port);
    outb(0x02, port + 1);
    ioperm(port, 2, 0);
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 3 && argc != 4) {
        fprintf(stderr, "usage: %s get GPIO | set GPIO VALUE\n", argv[0]);
        return 2;
    }
    unsigned gpio = 0;
    if (parse_uint(argv[2], &gpio) || gpio > 79) {
        fprintf(stderr, "invalid GPIO: %s\n", argv[2]);
        return 2;
    }
    unsigned port = 0x600u + gpio / 8u;
    unsigned mask = 1u << (gpio % 8u);
    if (ioperm(port, 1, 1) != 0) {
        fprintf(stderr, "ioperm(0x%x): %s\n", port, strerror(errno));
        return 1;
    }
    if (!strcmp(argv[1], "set") && argc == 4 && ensure_output(gpio) != 0) {
        fprintf(stderr, "unable to configure IT87 GPIO output\n");
        ioperm(port, 1, 0);
        return 1;
    }
    unsigned value = inb(port);
    if (!strcmp(argv[1], "get")) {
        printf("%u\n", (value & mask) != 0);
    } else if (!strcmp(argv[1], "set") && argc == 4) {
        unsigned requested = 0;
        if (parse_uint(argv[3], &requested) || requested > 1) {
            fprintf(stderr, "VALUE must be 0 or 1\n");
            ioperm(port, 1, 0);
            return 2;
        }
        unsigned next = requested ? (value | mask) : (value & ~mask);
        outb((unsigned char)next, port);
    } else {
        fprintf(stderr, "invalid operation\n");
        ioperm(port, 1, 0);
        return 2;
    }
    ioperm(port, 1, 0);
    return 0;
}
