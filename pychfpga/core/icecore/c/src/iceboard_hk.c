#include <tuber.h>
#include <string.h>

#include "iceboard.h"
#include "runtime.h"
#include "support.h"
#include "iceboard_hw.h"

tuber_method(DOUBLE, IceBoard, get_backplane_temperature,
	"Retrieve the backplane temperature.",
	1, ((STRING_CONST, sensor, NULL, "Which sensor? (See description below)")),
	2, (CATEGORY_ICEBOARD, CATEGORY_BACKPLANE),
	"The following temperature sensors are supported:\n"
	"\n"
	"	BACKPLANE_TEMPERATURE_SLOT1\n"
	"	BACKPLANE_TEMPERATURE_SLOT16"
) {
	FILE *f=NULL;
	char *fn=NULL;
	int i;

	if(!strcmp(BACKPLANE_TEMPERATURE_SLOT1, sensor))
		fn = "/sys/bus/i2c/devices/19-004e/temp1_input";
	else if(!strcmp(BACKPLANE_TEMPERATURE_SLOT16, sensor))
		fn = "/sys/bus/i2c/devices/19-004d/temp1_input";
	else {
		oops("Unknown backplane temperature sensor '%s'", sensor);
		return(0);
	}

	if(!(f = fopen(fn, "r"))) {
		oops("Unable to open I2C file %s for backplane temperature '%s'", fn, sensor);
		return(0);
	}

	fscanf(f, "%i", &i);
	fclose(f);
	return(i / 1000.);
}

tuber_method(DOUBLE, IceBoard, get_motherboard_temperature,
	"Retrieve the temperature from one of the motherboard's sensors.",
	1, ((STRING_CONST, sensor, NULL, "Which sensor? (See description below)")),
	1, (CATEGORY_ICEBOARD),
	"The following motherboard temperature sensors are recognized:\n"
	"	U34: MOTHERBOARD_TEMPERATURE_POWER, between the two 1.0V bucks,\n"
	"	U35: MOTHERBOARD_TEMPERATURE_FPGA, near USER SMA A,\n"
	"	U36: MOTHERBOARD_TEMPERATURE_ARM, under the CPU shield, and\n"
	"	U37: MOTHERBOARD_TEMPERATURE_PHY, also under the CPU shield.\n"
) {
	FILE *f=NULL;
	const char *fn=NULL;
	int i;
	double t;

	if(!strcmp(MOTHERBOARD_TEMPERATURE_FPGA_DIE, sensor)) {
		pthread_mutex_lock(&self->fpga_jtag_lock);
		/*xadc_init();*/
		t = xadc_read_temperature();
		pthread_mutex_unlock(&self->fpga_jtag_lock);
		return t;
	}

	if(!strcmp(MOTHERBOARD_TEMPERATURE_POWER, sensor))
		fn = "/sys/bus/i2c/devices/12-0048/temp1_input";
	else if(!strcmp(MOTHERBOARD_TEMPERATURE_ARM, sensor))
		fn = "/sys/bus/i2c/devices/12-004a/temp1_input";
	else if(!strcmp(MOTHERBOARD_TEMPERATURE_FPGA, sensor))
		fn = "/sys/bus/i2c/devices/12-004b/temp1_input";
	else if(!strcmp(MOTHERBOARD_TEMPERATURE_PHY, sensor))
		fn = "/sys/bus/i2c/devices/12-004c/temp1_input";
	else {
		oops("Unknown motherboard temperature sensor '%s'", sensor);
		return(0);
	}

	if(!(f = fopen(fn, "r"))) {
		oops("Unable to open I2C file %s for temperature sensor '%s'", fn, sensor);
		return(0);
	}

	fscanf(f, "%i", &i);
	fclose(f);

	return(i / 1000.);
}

tuber_method(DOUBLE, IceBoard, get_motherboard_voltage,
	"Retrieve the voltage from one of the motherboard's sensors.",
	1, ((STRING_CONST, rail, NULL, "Which rail? (See description below)")),
	1, (CATEGORY_ICEBOARD),
	"The following motherboard rails are instrumented:\n"
	"	MOTHERBOARD_RAIL_VCC3V3,\n"
	"	MOTHERBOARD_RAIL_VCC12V0,\n"
	"	MOTHERBOARD_RAIL_VCC5V5,\n"
	"	MOTHERBOARD_RAIL_VCC1V0_GTX,\n"
	"	MOTHERBOARD_RAIL_VCC1V0,\n"
	"	MOTHERBOARD_RAIL_VCC1V2,\n"
	"	MOTHERBOARD_RAIL_VCC1V5,\n"
	"	MOTHERBOARD_RAIL_VCC1V8, and\n"
	"	MOTHERBOARD_RAIL_VADJ."
) {
	FILE *f=NULL;
	const char *fn=NULL;
	int i;

	/* in1_input is bus voltage (i.e. output voltage) */
	if(!strcmp(MOTHERBOARD_RAIL_VCC12V0, rail))
		fn = "/sys/bus/i2c/devices/10-0047/in1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC5V5, rail))
		fn = "/sys/bus/i2c/devices/10-0048/in1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC3V3, rail))
		fn = "/sys/bus/i2c/devices/10-0049/in1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VADJ, rail))
		fn = "/sys/bus/i2c/devices/10-0043/in1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V8, rail))
		fn = "/sys/bus/i2c/devices/10-004b/in1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V5, rail))
		fn = "/sys/bus/i2c/devices/10-004c/in1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V2, rail))
		fn = "/sys/bus/i2c/devices/10-004d/in1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V0, rail))
		fn = "/sys/bus/i2c/devices/10-004e/in1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V0_GTX, rail))
		fn = "/sys/bus/i2c/devices/10-004f/in1_input";
	else {
		oops("Unknown motherboard power rail '%s'", rail);
		return(0);
	}

	if(!(f = fopen(fn, "r"))) {
		oops("Unable to open I2C file %s for voltage sensor '%s'", fn, rail);
		return(0);
	}

	fscanf(f, "%i", &i);
	fclose(f);

	return(i / 1000.);
}

tuber_method(DOUBLE, IceBoard, get_motherboard_current,
	"Retrieve the voltage from one of the motherboard's sensors.",
	1, ((STRING_CONST, rail, NULL, "Which rail? (See description below)")),
	1, (CATEGORY_ICEBOARD),
	"The following motherboard rails are instrumented:\n"
	"	MOTHERBOARD_RAIL_VCC3V3,\n"
	"	MOTHERBOARD_RAIL_VCC12V0,\n"
	"	MOTHERBOARD_RAIL_VCC5V5,\n"
	"	MOTHERBOARD_RAIL_VCC1V0_GTX,\n"
	"	MOTHERBOARD_RAIL_VCC1V0,\n"
	"	MOTHERBOARD_RAIL_VCC1V2,\n"
	"	MOTHERBOARD_RAIL_VCC1V5,\n"
	"	MOTHERBOARD_RAIL_VCC1V8, and\n"
	"	MOTHERBOARD_RAIL_VADJ."
) {
	FILE *f=NULL;
	const char *fn=NULL;
	int i;

	/* curr1_input is current measured across shunt (calibrated
	 * approximately using inductor as a crappy resistor); calibration
	 * is stored in kernel source. */
	if(!strcmp(MOTHERBOARD_RAIL_VCC12V0, rail))
		fn = "/sys/bus/i2c/devices/10-0047/curr1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC5V5, rail))
		fn = "/sys/bus/i2c/devices/10-0048/curr1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC3V3, rail))
		fn = "/sys/bus/i2c/devices/10-0049/curr1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VADJ, rail))
		fn = "/sys/bus/i2c/devices/10-0043/curr1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V8, rail))
		fn = "/sys/bus/i2c/devices/10-004b/curr1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V5, rail))
		fn = "/sys/bus/i2c/devices/10-004c/curr1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V2, rail))
		fn = "/sys/bus/i2c/devices/10-004d/curr1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V0, rail))
		fn = "/sys/bus/i2c/devices/10-004e/curr1_input";
	else if(!strcmp(MOTHERBOARD_RAIL_VCC1V0_GTX, rail))
		fn = "/sys/bus/i2c/devices/10-004f/curr1_input";
	else {
		oops("Unknown motherboard power rail '%s'", rail);
		return(0);
	}

	if(!(f = fopen(fn, "r"))) {
		oops("Unable to open I2C file %s for current sensor '%s'", fn, rail);
		return(0);
	}

	fscanf(f, "%i", &i);
	fclose(f);

	return(i / 1000.);
}

