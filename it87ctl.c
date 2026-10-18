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

static int sio_enter(void) {
    unsigned port = 0x2e;
    if (ioperm(port, 2, 1) != 0) return -1;
    outb(0x87, port);
    outb(0x01, port);
    outb(0x55, port);
    outb(0x55, port);
    outb(0x07, port);
    outb(0x07, port + 1);
    return 0;
}

static void sio_exit(void) {
    outb(0x02, 0x2e);
    outb(0x02, 0x2f);
    ioperm(0x2e, 2, 0);
}

static unsigned sio_read(unsigned reg) {
    outb((unsigned char)reg, 0x2e);
    return inb(0x2f);
}

static void sio_write(unsigned reg, unsigned value) {
    outb((unsigned char)reg, 0x2e);
    outb((unsigned char)value, 0x2f);
}

static int ensure_output(unsigned gpio) {
    unsigned reg = 0xc8u + gpio / 8u;
    unsigned mask = 1u << (gpio % 8u);
    if (sio_enter() != 0) return -1;
    unsigned current = sio_read(reg);
    sio_write(reg, current | mask);
    sio_exit();
    return 0;
}

static int set_gpio_value(unsigned gpio, unsigned requested) {
    unsigned port = 0x600u + gpio / 8u;
    unsigned mask = 1u << (gpio % 8u);
    if (ioperm(port, 1, 1) != 0) return -1;
    unsigned value = inb(port);
    unsigned next = requested ? (value | mask) : (value & ~mask);
    outb((unsigned char)next, port);
    ioperm(port, 1, 0);
    return 0;
}

static int disable_gp_led_blink(unsigned gpio, unsigned requested) {
    /*
     * IT8625E GPLED1 uses register 0xf8 to select the blinking GPIO.
     * GP47 is encoded as 0x27.  Moving the selected GPIO back to Simple
     * I/O mode and clearing the mapping stops the hardware blink engine.
     */
    if (sio_enter() != 0) return -1;
    unsigned mapping = sio_read(0xf8);
    unsigned location = mapping & 0x3f;
    if (location != 0) {
        unsigned old_gpio = ((location >> 3) * 10u) + (location & 0x07u);
        unsigned reg = 0xc0u + old_gpio / 10u - 1u;
        unsigned mask = 1u << (old_gpio % 10u);
        sio_write(reg, sio_read(reg) | mask);
    }
    sio_write(0xf8, mapping & 0xc0u);
    sio_exit();
    if (ensure_output(gpio) != 0) return -1;
    return set_gpio_value(gpio, requested);
}

int main(int argc, char **argv) {
    if ((argc != 3 && argc != 4) ||
        (strcmp(argv[1], "get") && strcmp(argv[1], "set") &&
         strcmp(argv[1], "disable-blink"))) {
        fprintf(stderr, "usage: %s get GPIO | set GPIO VALUE | disable-blink GPIO VALUE\n", argv[0]);
        return 2;
    }
    unsigned gpio = 0;
    if (parse_uint(argv[2], &gpio) || gpio > 79) {
        fprintf(stderr, "invalid GPIO: %s\n", argv[2]);
        return 2;
    }
    if (!strcmp(argv[1], "get")) {
        unsigned port = 0x600u + gpio / 8u;
        unsigned mask = 1u << (gpio % 8u);
        if (ioperm(port, 1, 1) != 0) {
            fprintf(stderr, "ioperm(0x%x): %s\n", port, strerror(errno));
            return 1;
        }
        printf("%u\n", (inb(port) & mask) != 0);
        ioperm(port, 1, 0);
        return 0;
    }
    if (argc != 4) {
        fprintf(stderr, "VALUE must be provided\n");
        return 2;
    }
    unsigned requested = 0;
    if (parse_uint(argv[3], &requested) || requested > 1) {
        fprintf(stderr, "VALUE must be 0 or 1\n");
        return 2;
    }
    if (!strcmp(argv[1], "disable-blink")) {
        if (disable_gp_led_blink(gpio, requested) != 0) {
            fprintf(stderr, "unable to disable IT87 GPIO LED blinking\n");
            return 1;
        }
        return 0;
    }
    if (ensure_output(gpio) != 0 || set_gpio_value(gpio, requested) != 0) {
        fprintf(stderr, "unable to configure IT87 GPIO output\n");
        return 1;
    }
    return 0;
}
