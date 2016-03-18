# IceBoard base components
SUBDIRS := libiceboard fastpath

export BUILD_CC		= gcc
export CROSS_COMPILE	= arm-buildroot-linux-gnueabihf-
export CC		= $(CROSS_COMPILE)gcc
export LD		= $(CROSS_COMPILE)ld
export AS		= $(CROSS_COMPILE)as
export RANLIB		= $(CROSS_COMPILE)ranlib
export CXX		= $(CROSS_COMPILE)c++
export CPP		= $(CROSS_COMPILE)cpp
export STRIP		= $(CROSS_COMPILE)strip
export OBJDUMP		= $(CROSS_COMPILE)objdump
export CFLAGS		+= -O2 -Wall -Werror -fPIC -fno-strict-aliasing

default: $(SUBDIRS)
.PHONY: default clean $(SUBDIRS)

$(SUBDIRS):
	$(MAKE) -C $@

clean:
	$(foreach d,$(SUBDIRS), $(MAKE) -C $(d) $@;)
