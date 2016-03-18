#ifndef __ICEBOARD_CONSTANTS_H__
#define __ICEBOARD_CONSTANTS_H__

#include <strings.h>

typedef int bool;

#define NUM_MEZZANINES 2

/* Categories for tuber_methods */
extern const char *CATEGORY_ICEBOARD;
extern const char *CATEGORY_MEZZANINE;
extern const char *CATEGORY_BACKPLANE;

/* Units */
extern const char *HZ;
extern const char *RAW;
extern const char *VOLTS;
extern const char *AMPS;
extern const char *WATTS;
extern const char *DAC_COUNTS;
extern const char *ADC_COUNTS;
extern const char *NORMALIZED;
extern const char *DEGREES;
extern const char *RADIANS;
extern const char *OHMS;

static bool is_hertz(const char *u)		{return u && !strcasecmp(u, HZ); }
static bool is_raw(const char *u)		{return u && !strcasecmp(u, RAW); }
static bool is_volts(const char *u)		{return u && !strcasecmp(u, VOLTS); }
static bool is_amps(const char *u)		{return u && !strcasecmp(u, AMPS); }
static bool is_watts(const char *u)		{return u && !strcasecmp(u, WATTS); }
static bool is_dac_counts(const char *u)	{return u && !strcasecmp(u, DAC_COUNTS); }
static bool is_adc_counts(const char *u)	{return u && !strcasecmp(u, ADC_COUNTS); }
static bool is_normalized(const char *u)	{return u && !strcasecmp(u, NORMALIZED); }
static bool is_degrees(const char *u)		{return u && !strcasecmp(u, DEGREES); }
static bool is_radians(const char *u)		{return u && !strcasecmp(u, RADIANS); }
static bool is_ohms(const char *u)		{return u && !strcasecmp(u, OHMS); }

/* Temperature sensors */
extern const char *MOTHERBOARD_TEMPERATURE_POWER;
extern const char *MOTHERBOARD_TEMPERATURE_ARM;
extern const char *MOTHERBOARD_TEMPERATURE_FPGA;
extern const char *MOTHERBOARD_TEMPERATURE_FPGA_DIE;
extern const char *MOTHERBOARD_TEMPERATURE_PHY;

extern const char *BACKPLANE_TEMPERATURE_SLOT1;
extern const char *BACKPLANE_TEMPERATURE_SLOT16;

/* Motherboard power rails */
extern const char *MOTHERBOARD_RAIL_VCC3V3;
extern const char *MOTHERBOARD_RAIL_VCC12V0;
extern const char *MOTHERBOARD_RAIL_VCC5V5;
extern const char *MOTHERBOARD_RAIL_VCC1V0_GTX;
extern const char *MOTHERBOARD_RAIL_VCC1V0;
extern const char *MOTHERBOARD_RAIL_VCC1V2;
extern const char *MOTHERBOARD_RAIL_VCC1V5;
extern const char *MOTHERBOARD_RAIL_VCC1V8;
extern const char *MOTHERBOARD_RAIL_VADJ;

extern const char *MEZZANINE_RAIL_VCC3V3;
extern const char *MEZZANINE_RAIL_VCC12V0;
extern const char *MEZZANINE_RAIL_VADJ;

extern const char *CLOCK_SOURCE_XTAL;
extern const char *CLOCK_SOURCE_SMA;
extern const char *CLOCK_SOURCE_BP;

#endif
