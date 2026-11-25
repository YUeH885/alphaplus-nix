CROSS_COMPILE ?= aarch64-linux-gnu-
ifeq ($(origin CC),default)
CC = $(CROSS_COMPILE)gcc
endif
HEXAGON_CC ?= clang-21
HEXAGON_LD ?= ld.lld-21
CFLAGS += -O2 -Wall -Wextra -Werror
LDLIBS += -lhexagonrpc

ROOT ?= $(abspath ../..)
SYSROOT ?= $(firstword $(wildcard $(ROOT)/build/debian-cross-arm64/rootfs-*))
ifneq ($(SYSROOT),)
CFLAGS += --sysroot=$(SYSROOT)
endif

all: client libalphaplus_cdsp_skel.so

client: client.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $< $(LDLIBS)

vector.o: vector.c
	$(HEXAGON_CC) --target=hexagon -mcpu=hexagonv66 -fPIC -ffreestanding \
		-fno-stack-protector -O2 -Wall -Wextra -Werror -c -o $@ $<

libalphaplus_cdsp_skel.so: vector.o hexagon.ld
	$(HEXAGON_LD) -shared -Bsymbolic --hash-style=sysv -z max-page-size=4096 \
		-T hexagon.ld -soname $@ -o $@ vector.o

clean:
	rm -f client libalphaplus_cdsp_skel.so vector.o

.PHONY: all clean
