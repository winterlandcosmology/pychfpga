default: libiceboard.so
.PHONY: clean default

SRCS :=	iceboard.c				\
	iceboard_hw.c				\
	iceboard_constants.c			\
	iceboard_fpga.c				\
	iceboard_net.c				\
	iceboard_mezz.c				\
	iceboard_bp.c				\
	iceboard_hk.c				\
	iceboard_sysinfo.c			\
	base64.c				\
	support_system.c			\
	i2c_eeprom.c				\
	ipmi_frui.c				\
	dnssd.c					\
	jtag.c

VPATH=../../src:../../include

libiceboard.so: $(SRCS:.c=.o)
	$(CC) $(LDFLAGS) -shared -o $@ $^	\
		-lm -ljansson -lssl -lcrypto

CFLAGS += -g -fPIC -Wall -Werror -Wno-unused-function -D_GNU_SOURCE	\
	  -I../../include -DLIBTUBER
LDFLAGS += -g -lavahi-client -lavahi-common

clean:
	rm -f *.o *.so *.d

# Dependencies. Adapted from scottmcpeak.com/autodepend/autodepend.html
-include $(SRCS:.c=.d)
%.o: %.c
	$(CC) -c $(CFLAGS) $< -o $*.o
	$(CC) -MM $(CFLAGS) $< > $*.d
	@cp -f $*.d $*.d.tmp
	@sed -e 's/.*://' -e 's/\\$$//' < $*.d.tmp | fmt -1 | \
	  sed -e 's/^ *//' -e 's/$$/:/' >> $*.d
	@rm -f $*.d.tmp
