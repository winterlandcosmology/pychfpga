#include <tuber.h>
#include <string.h>
#include <syslog.h>
#include <unistd.h>
#include <sys/types.h>
#include <fcntl.h>
#include <errno.h>

#include "iceboard.h"
#include "runtime.h"
#include "support.h"
#include "iceboard_hw.h"

#include "i2c_eeprom.h"
#include "base64.h"

#define BP_EEPROM_FILE "/sys/bus/i2c/devices/19-0054/eeprom"

void cache_bp_frui(IceBoard *self) {
	uint8_t buf[1024];
	char *warning;

	frui_parser *p=NULL;
	int fd = -1;
	int attempt;
	const int MAXATTEMPTS=25;

	if(self->bp_frui) {
		frui_free(self->bp_frui);
		self->bp_frui = NULL;
	}

	for(attempt = 1; attempt < MAXATTEMPTS; attempt++) {
		/* Crude attempt at arbitration */
		syslog(LOG_INFO, "Trying to read backplane IPMI block: attempt number %i", attempt);
		usleep(1000 * self->bp_slot);

		/* Read raw data from EEPROM */
		if(-1 == (fd = open(BP_EEPROM_FILE, O_RDONLY))) {
			syslog(LOG_ERR, "Unable to open I2C EEPROM! Is the backplane present?");
			goto out;
		}

		if(read(fd, buf, sizeof(buf)) != sizeof(buf)) {
			syslog(LOG_WARNING, "Error (%s) reading raw data from EEPROM! Retrying...", strerror(errno));
			close(fd);
			continue; /* retry */
		}
		close(fd);

		if(p) {
			frui_parser_free(p);
			p = NULL;
		}
		if(!(p = frui_parser_new()))
			return;

		/* Try to parse into FRUI structure */
		if(!(self->bp_frui = frui_parser_loadb(p, sizeof(buf), buf))) {
			syslog(LOG_ERR, "Failed to parse IPMI FRU block! (Is "
					"this an uninitialized backplane?) "
					"Retrying...");
			continue;
		}

		syslog(LOG_INFO, "Parsing backplane IPMI block: success!");
		self->bp_initialized = 1;
		break; /* success! */
	}
	if(attempt == MAXATTEMPTS) {
		syslog(LOG_ERR, "Failed read backplane EEPROM. Arbitration battle?");
		goto out;
	}

	/* Any warnings? Log them. */
	while((warning = frui_parser_get_warning(p)))
		syslog(LOG_WARNING, "IPMI parser: %s", warning);

out:
	if(p)
		frui_parser_free(p);
}

void cache_bp_slot(IceBoard *self) {
	struct iceboard_gpio *slotid[4];
	int b_pu[4], b_pd[4];
	int bit;
	int slot_pu, slot_pd;

	const char *mux_filenames[]={
		"/sys/kernel/debug/omap_mux/vout1_b_cb_c3",
		"/sys/kernel/debug/omap_mux/vout1_b_cb_c4",
		"/sys/kernel/debug/omap_mux/vout1_b_cb_c5",
		"/sys/kernel/debug/omap_mux/vout1_b_cb_c6",
	};

	const char *bp_arm_gpios[]={
		"BP_ARM_GPIO0",
		"BP_ARM_GPIO1",
		"BP_ARM_GPIO2",
		"BP_ARM_GPIO3"
	};

	FILE *f;

	/* This function does not use I2C and reads the backplane slot number
	 * pull up/downs at the ARM itself */

	for(bit=0; bit<4; bit++) {
		f = fopen(mux_filenames[bit], "w");
		if(f == NULL) {
			syslog(LOG_ERR, "Failed to open pin mux file: %s", mux_filenames[bit]);
			return;
		}
		fprintf(f, "0x40080"); /* Input pin - pull down enabled */
		fflush(f);
		slotid[bit] = get_gpio_by_netname(bp_arm_gpios[bit]);
		if(!slotid[bit]) {
			syslog(LOG_ERR, "Failed to obtain BP GPIO GPIO handle!");
			fclose(f);
			return;
		}

		/* Retrieve slot number after enabling arm pull downs */
		b_pd[bit] = gpio_get(slotid[bit], 1);

		fprintf(f, "0x60080"); /* Input pin - pull up enabled */
		fclose(f);

		/* Retrieve slot number after enabling arm pull up */
		b_pu[bit] = gpio_get(slotid[bit], 1);
	}

	slot_pu = ((b_pu[0] << 0) | (b_pu[1] << 1) | (b_pu[2] << 2) | (b_pu[3] << 3)) + 1;
	slot_pd = ((b_pd[0] << 0) | (b_pd[1] << 1) | (b_pd[2] << 2) | (b_pd[3] << 3)) + 1;

	if (slot_pd == slot_pu) {
		self->bp_slot = slot_pu;
		syslog(LOG_ERR, "Got backplane slot %i\n", self->bp_slot);
	} else {
		self->bp_slot = -1; /* Not in a backplane */
		syslog(LOG_ERR, "Got backplane slot %i; this board is not in a backplane.\n", self->bp_slot);
	}
}

tuber_method(VOID, IceBoard, _initialize_backplane,
		"Initialize this IceBoard's cache of backplane data. This MUST be done serially!",
		0, (),
		1, (CATEGORY_ICEBOARD),
		"This function is necessary because of I2C contention on the "
		"backplane. Boards must not compete with each other for "
		"access to the backplane EEPROM; for now, we push the "
		"responsibility for this to the Python layer. Sorry.\n\n"
		"Eventually, it'll be done via multimaster-capable I2C."
) {
	/* Don't repeat ourselves */
	if(self->bp_initialized)
		return;

	cache_bp_frui(self);

	/* Trigger update of DNS-SD TXT records */
	write(self->dnssd_txtupdate_fd, (int[]){0}, sizeof(int));

	self->bp_initialized = 1;
}

tuber_method(BOOLEAN, IceBoard, is_backplane_present,
		"Determines if a backplane is present.",
		0, (()),
		2, (CATEGORY_ICEBOARD, CATEGORY_BACKPLANE),
		""
) {
	if(self->bp_slot == -1) {
		return 0;
	}
	else {
		return 1;
	}
}

tuber_method(INTEGER, IceBoard, get_backplane_slot,
		"Retrieves the slot number this iceboard occupies.",
		0, (()),
		1, (CATEGORY_ICEBOARD),
		""
) {
	return self->bp_slot;
}

tuber_method(INTEGER, IceBoard, _cache_bp_frui,
		"Reads the eeprom on the backplane and updates the FRUI cache.",
		0, (()),
		1, (CATEGORY_ICEBOARD),
		""
) {
	cache_bp_frui(self);
	return 0;
}

tuber_method(VOID, IceBoard, _backplane_eeprom_write_base64,
		"Write the I2C EEPROM associated with a backplane.",
		1, ((STRING_CONST, b64, NULL, "EEPROM contents (base64 encoded)")),
		2, (CATEGORY_ICEBOARD, CATEGORY_BACKPLANE),
		""
) {
	uint8_t *buf = NULL;
	int slen, blen;
	FILE *f = NULL;
	frui_parser *p=NULL;
	char *warning;
	frui *frui=NULL;

	if((slen = base64_validate_string(b64)) == -1) {
		oops("Invalid base-64 string supplied!");
		goto out;
	}

	blen = base64_size_blob(slen);
	if(blen < 0 || blen > 1024) {
		oops("Backplane EEPROM has invalid size %i!", blen);
		goto out;
	}

	if(!(buf = malloc(blen + 1))) {
		oops("Unable to allocate %i bytes for EEPROM block!", blen);
		goto out;
	}

	if(base64_decode_string(slen, b64, blen, buf) == -1) {
		oops("Error while decoding base-64 block!");
		goto out;
	}

	/* Try to parse into FRUI structure into memory BEFORE writing it to
	   the EEPROM. As a side-effect, we update the local FRUI cache. */
	if(!(p = frui_parser_new()))
		goto out;
	if(!(frui = frui_parser_loadb(p, blen, buf))) {
		oops("Failed to parse IPMI FRU block! (%s) Refusing to program EEPROM.",
				frui_parser_get_error(p));
		goto out;
	}

	/* Any warnings? Log them. */
	while((warning = frui_parser_get_warning(p)))
		syslog(LOG_WARNING, "IPMI parser: %s", warning);

	/* Write EEPROM */
	f = fopen(BP_EEPROM_FILE, "w");
	if(!f || fwrite(buf, 1, blen, f) != blen) {
		oops("Error (%s) during EEPROM write!", strerror(ferror(f)));
		goto out;
	}

	/* Replace cached IPMI data */
	if(self->bp_frui)
		frui_free(self->bp_frui);
	self->bp_frui = frui;
	self->bp_initialized = 1;
	frui = NULL;

out:
	pthread_mutex_unlock(&self->i2c_mtx_lock);

	if(buf)
		free(buf);
	if(f)
		fclose(f);
	if(p)
		frui_parser_free(p);
	if(frui)
		frui_free(frui);
}

tuber_method(JSON, IceBoard, _get_backplane_ipmi,
		"Read the I2C EEPROM, and interpret it as an IPMI descriptor.",
		0, (),
		2, (CATEGORY_ICEBOARD, CATEGORY_BACKPLANE),
		""
) {
	json_t *result;

	if(!self->bp_initialized) {
		oops("Backplane cache of IPMI data is not yet populated! Try "
		     "_initialize_backplane() -- you CANNOT run this function "
		     "in a parallel context.");
		return NULL;
	}

	if(!self->bp_frui) {
		oops("Backplane not present or has no IPMI data!");
		return NULL;
	}

	result = json_object();
	if(frui_has_board_info(self->bp_frui))
		json_object_set_new(result, "board", json_pack(
				"{s:s,s:s,s:s,s:s,s:s}",
				"manufacturer", frui_get_board_manufacturer(self->bp_frui),
				"name", frui_get_board_name(self->bp_frui),
				"serial_number", frui_get_board_serial_number(self->bp_frui),
				"part_number", frui_get_board_part_number(self->bp_frui),
				"frui_file_id", frui_get_board_fru_file_id(self->bp_frui)));

	if(frui_has_product_info(self->bp_frui))
		json_object_set_new(result, "product", json_pack(
				"{s:s,s:s,s:s,s:s,s:s,s:s,s:s}",
				"manufacturer", frui_get_product_manufacturer(self->bp_frui),
				"name", frui_get_product_name(self->bp_frui),
				"part_number", frui_get_product_part_number(self->bp_frui),
				"version_number", frui_get_product_version_number(self->bp_frui),
				"serial_number", frui_get_product_serial_number(self->bp_frui),
				"asset_tag", frui_get_product_asset_tag(self->bp_frui),
				"frui_file_id", frui_get_product_fru_file_id(self->bp_frui)));

	return result;
}

tuber_method(STRING, IceBoard, _get_backplane_type,
	"Get backplane type.",
	0, (),
	2, (CATEGORY_ICEBOARD, CATEGORY_BACKPLANE),
	""
) {
	/* This function returns a malloc()'d string that MUST be free()d! */
	const char *t=NULL;

	/* Refresh the cached value and return that. */
	if(self->bp_frui)
		t = frui_get_product_part_number(self->bp_frui);

	if(!t) {
		oops("Unknown backplane type! Is the IPMI block valid?");
		return NULL;
	}

	return strdup(t);
}

tuber_method(STRING, IceBoard, _get_backplane_version,
	"Get backplane version.",
	0, (),
	2, (CATEGORY_ICEBOARD, CATEGORY_BACKPLANE),
	""
) {
	/* This function returns a malloc()'d string that MUST be free()d! */
	const char *t=NULL;

	/* Refresh the cached value and return that. */
	if(self->bp_frui)
		t = frui_get_product_version_number(self->bp_frui);

	if(!t) {
		oops("Unknown backplane version! Is the IPMI block valid?");
		return NULL;
	}

	return strdup(t);
}

tuber_method(STRING, IceBoard, _get_backplane_serial,
	"Get mezzanine serial number.",
	0, (),
	2, (CATEGORY_ICEBOARD, CATEGORY_BACKPLANE),
	""
) {
	/* This function returns a malloc()'d string that MUST be free()d! */
	const char *s=NULL;

	/* Refresh the cached value and return that. */
	if(self->bp_frui)
		s = frui_get_product_serial_number(self->bp_frui);

	if(!s) {
		oops("Unspecified backplane serial number! Is the IPMI block valid?");
		return NULL;
	}

	return strdup(s);
}
