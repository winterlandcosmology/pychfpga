#include <stdio.h>
#include "ftdispi.h"
#include <stdint.h>

int main(int argc, char **argv)
{
	struct ftdi_context fc;
	struct ftdispi_context fsc;
	int i;
	uint32_t x[] = {
		/* 12,20s/^[0-9]\t\([A-Z0-9]*\)$/\t\t0x\1,/ */
// #define IBERT_156_25
#define WRITE_EEPROM
//#if 0
		// ICEBoard Rev 0 Original configuration from Graeme for ARM processor debugging
		// 20/100/25 MHz
		// 0x31260320,
		// 0xEB060301,
		// 0x111E0302,
		// 0x68860303,
		// 0x68860314,
		// 0x10001E75,
		// 0x00CF09E6,
		// 0xBD90FB87,
		// 0x80001808,

// #elif defined(IBERT_156_25)
		/* 156.25 MHz on all 3 LVDS outputs;
		 * ARM and PHY clocks disabled */
		// 0x68260320,
		// 0xEA840301,
		// 0x681E0302,
		// 0xEB840303,
		// 0xEB860314,
		// 0x101C1E75,
		// 0x84BF49A6,
		// 0xBDB23BE7,
		// 0x20009D98,

		// IceBoard Rev0: Functional 10 GE & 1 GE, but approximate ARM frequencies 
		// 20.83  - 156.25 - 125 MHz by Graeme 
		//0x31160320,
		//0xEA840301,
		//0x681E0302,
		//0xEB840303,
		//0xEB860314,
		//0x101C1E75,
		//0x84BF49A6,
		//0xBDB23BE7,
		//0x20009D98,

		// ICEBoard Rev2 PLL1 : 0: 20 MHz LVCMOS, 1: 100 MHZ LVDS, 2: 25 MHz LVCMOS, 3: 125 MHz LVDS, 4: 200 MHz LVDS
		 0x01260320,
		 0xEB060301,
		 0x011E0302,
		 0xEB040303,
		 0xEB860314,
		 0x101C1E75,
		 0x849F4FE6,
		 0xBDB23BE7,
		 0x20009D98,

		//ICEBoard Rev2 PLL2 : 0: 156.25 MHz LVDS, 1: 156.25 MHz LVDS, 2: 156.25 MHz LVDS, 3: 125.00 MHz LVDS, 4: 10 MHz LVDS
		// 0xEB840320,
		// 0xEB840301,
		// 0xEB840302,
		// 0xEB860303,
		// 0xEB400014,
		// 0x101C1E75,
		// 0x84BF49A6,
		// 0xBDB23BE7,
		// 0x20009D98,

// 		// Scrachpad
// 		0x69260320,
// 		0x69060301,
// 		0x691E0302,
// 		0xEB040303,
// 		0x69860314,
// 		0x101C1E75,
// 		0x849F4FE6,
// 		0xBDB23BE7,
// 		0x20009D98,
// // // #endif
#ifdef WRITE_EEPROM
		0x0000001f, /* write (unlocked) */
#endif
	};

	uint32_t y;
	int n, m;
	uint32_t r6=0;

	if (ftdi_init(&fc) < 0) {
		fprintf(stderr, "ftdi_init failed\n");
		return 1;
	}
	i = ftdi_usb_open(&fc, 0x0403, 0x6014);
	if (i < 0 && i != -5) {
		fprintf(stderr, "OPEN: %s\n", ftdi_get_error_string(&fc));
		exit(-1);
	}
	ftdispi_open(&fsc, &fc, INTERFACE_A);
	ftdispi_setmode(&fsc, 1, 0, 0, 1, 0, 0);
	ftdispi_setclock(&fsc, 200000);
	ftdispi_setloopback(&fsc, 0);

	for(n=0; n<sizeof(x)/sizeof(*x); n++) {
#ifndef WRITE_EEPROM
		if((x[n] & 0xf) == 6) {
			r6 = x[n] | 0x84000000;
			x[n] &= ~0x04000000u;
		}
#endif
		fprintf(stderr, "%i: 0x%08x\n", n, x[n]);
		ftdispi_write(&fsc, &x[n], sizeof(*x), 0);
	}
#ifndef WRITE_EEPROM
	if(r6)
		ftdispi_write(&fsc, &r6, sizeof(r6), 0);
#endif

	ftdispi_close(&fsc, 1);
	return 0;
}
