#ifndef __ICEBOARD_H__
#define __ICEBOARD_H__

#include "runtime.h"
#include "support.h"
#include "iceboard_constants.h"
#include "ipmi_frui.h"

#include <stdint.h>
#include <pthread.h>
#include <jansson.h>

typedef struct IceBoard IceBoard;

#define FPGA_SPIDEV "/dev/spidev4.1"
#define FPGA_SPIDEV_CLK 16e6

/*
 * Types
 */

typedef int bool;

struct IceBoard {
	uint32_t (*rread)(IceBoard *, void*);
	void (*rwrite)(IceBoard *, void*, uint32_t);
	void (*randeq)(IceBoard *, void*, uint32_t);
	void (*roreq)(IceBoard *, void*, uint32_t);

	/* We keep IPMI data around *only* while the mezzanine is turned on.
	 * When it's off, it's conceivable that the board will be hotplugged. */
	frui *mezz_frui[NUM_MEZZANINES];
	frui *bp_frui;
	frui *mb_frui;

	char *personality;

	pthread_mutex_t fpga_spidev_lock;
	FILE *fpga_spidev;

	int bp_slot;
	int bp_initialized;

	/* There's a single, gigantic lock for all I2C access through the I2C
	 * matrix. Sadly, it's necessary: imagine contention on the backplane.
	 * The bus is left in a sorry state until we successfully wrap up the
	 * access, and *nothing* on I2C can be used in the meantime. Ugh. */
	pthread_mutex_t i2c_mtx_lock;

	pthread_mutex_t fpga_jtag_lock;

	/* Thread used for DNS-SD interactions */
	pthread_t dnssd_thread;

	int dnssd_txtupdate_fd; /* write an int to this for txt updates. */
};

/*
 * IceBoard-wide methods
 */

IceBoard *new_IceBoard(void);
void delete_IceBoard(IceBoard *);

void IceBoard_set_mezzanine_power(IceBoard *self, bool power, int mezzanine);
bool IceBoard_get_mezzanine_power(IceBoard *self, int mezzanine);
bool IceBoard_is_mezzanine_present(IceBoard *self, int mezzanine);

char *IceBoard_get_motherboard_serial(IceBoard *self);
char *IceBoard__get_mezzanine_type(IceBoard *self, int mezzanine);
char *IceBoard__get_mezzanine_version(IceBoard *self, int mezzanine);
char *IceBoard__get_mezzanine_serial(IceBoard *self, int mezzanine);

double IceBoard_get_motherboard_temperature(IceBoard *, const char *);
double IceBoard_get_motherboard_voltage(IceBoard *, const char *);
double IceBoard_get_motherboard_current(IceBoard *, const char *);

/*double IceBoard_get_mezzanine_temperature(IceBoard *, const char *, int);*/
double IceBoard_get_mezzanine_voltage(IceBoard *, const char *, int);
double IceBoard_get_mezzanine_current(IceBoard *, const char *, int);

void IceBoard__set_fpga_bitstream_base64(IceBoard *, const char *);
bool IceBoard_is_fpga_programmed(IceBoard *);
void IceBoard_clear_fpga_bitstream(IceBoard *);
int IceBoard_load_bitstream(IceBoard *ib, const char* bits, int len);

char *IceBoard__get_arm_ip(IceBoard *, const char *);
char *IceBoard__get_arm_mac(IceBoard *, const char *);

const char *IceBoard__get_personality(IceBoard *);
void IceBoard__set_personality(IceBoard *, const char *);

int IceBoard_get_backplane_slot(IceBoard *);
bool IceBoard_is_backplane_present(IceBoard *);

//const char *IceBoard__when_compiled_icecore(IceBoard *);
//int IceBoard__cache_bp_frui(IceBoard *);

/*
 * IPMI methods
 */

frui *IceBoard_get_mezzanine_ipmi_raw_cached(IceBoard *self, const int mezzanine);
json_t *IceBoard__get_mezzanine_ipmi(IceBoard *self, const int mezzanine);

int IceBoard__mezzanine_lock_ipmi_cache(IceBoard *self);
int IceBoard__mezzanine_unlock_ipmi_cache(IceBoard *self);

void cache_bp_frui(IceBoard *);
void cache_mb_frui(IceBoard *);
void cache_mezz_frui(IceBoard *, int);
void cache_bp_slot(IceBoard *);

/*
 * Register access.
 */

uint32_t fpga_spi_rread(IceBoard *, void*);
void fpga_spi_rwrite(IceBoard *, void*, uint32_t);
void fpga_spi_randeq(IceBoard *, void*, uint32_t);
void fpga_spi_roreq(IceBoard *, void*, uint32_t);

/* Low-level methods (iceboard_ll.c) */
frui *get_mezzanine_ipmi_raw(const int mezzanine);
frui *get_motherboard_ipmi_raw(void);
frui *get_backplane_ipmi_raw(void);
FILE *get_motherboard_ipmi_file(const char *fopen_mode);

/*
 * Threads
 */

int dnssd_main(IceBoard *);

#endif
