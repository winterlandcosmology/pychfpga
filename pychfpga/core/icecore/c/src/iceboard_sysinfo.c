#include <tuber.h>
#include <string.h>
#include <syslog.h>

#include <sys/ioctl.h>
#include <mtd/mtd-user.h>
#include <errno.h>

#include "iceboard.h"
#include "base64.h"

FILE *get_motherboard_ipmi_file(const char *fopen_mode) {
	FILE *proc_mtd = NULL;
	char *linebuf = NULL;
	size_t linebuf_len = 0;
	int mtd_index = -1;
	char fnbuf[16];
	FILE *mtd = NULL;

	/* Figure out which MTD block we're supposed to use */
	if(!(proc_mtd = fopen("/proc/mtd", "r"))) {
		oops("Unable to open /proc/mtd!");
		goto out;
	}

	/* Find the line containing the correct partition label */
	while(getline(&linebuf, &linebuf_len, proc_mtd) != -1) {
		if(strstr(linebuf, "IPMI FRU") &&
				sscanf(linebuf, "mtd%i:", &mtd_index) == 1)
			break;
	}
	if(mtd_index == -1) {
		oops("Unable to find IPMI FRU partition in SPI flash!");
		goto out;
	}
	sprintf(fnbuf, "/dev/mtd%i", mtd_index);
	if(!(mtd = fopen(fnbuf, fopen_mode)))
		oops("Error opening IPMI FRU partition in SPI flash! (%s)",
				strerror(errno));

out:
	if(proc_mtd)
		fclose(proc_mtd);
	if(linebuf)
		free(linebuf);

	return mtd;
}

void cache_mb_frui(IceBoard *self) {
	uint8_t *buf = NULL;
	char *warning;

	frui_parser *p=NULL;

	FILE *mtd = NULL;
	mtd_info_t mtd_info;

	if(self->mb_frui) {
		frui_free(self->mb_frui);
		self->mb_frui = NULL;
	}

	if(!(mtd = get_motherboard_ipmi_file("r"))) {
		/* oops() already called */
		goto out;
	}

	if(ioctl(fileno(mtd), MEMGETINFO, &mtd_info) == -1) {
		oops("Unable to call ioctl(MEMGETINFO) on MTD!");
		goto out;
	}

	if(mtd_info.size <= 0 || (buf = malloc(mtd_info.size)) == NULL) {
		oops("Unable to allocate %i bytes for IPMI read!", mtd_info.size);
		goto out;
	}

	/* Read raw data from flash */
	if(fread(buf, 1, mtd_info.size, mtd) != mtd_info.size) {
		oops("Unable to read %i bytes from IPMI partition!", mtd_info.size);
		goto out;
	}

	/* Try to parse into FRUI structure */
	if(!(p = frui_parser_new()))
		goto out;
	if(!(self->mb_frui = frui_parser_loadb(p, mtd_info.size, buf))) {
		oops("Failed to parse IPMI FRU block! (Is this an uninitialized board?)");
		goto out;
	}

	/* Any warnings? Log them. */
	while((warning = frui_parser_get_warning(p)))
		syslog(LOG_WARNING, "IPMI parser: %s", warning);

out:
	if(mtd)
		fclose(mtd);
	if(buf)
		free(buf);
	if(p)
		frui_parser_free(p);
}

tuber_method(JSON, IceBoard, _get_motherboard_ipmi,
		"Read the SPI EEPROM, and interpret it as an IPMI descriptor.",
		0, (),
		1, (CATEGORY_ICEBOARD),
		"This method requires the SPI flash to contain meaningful "
		"IPMI data."
) {
	json_t *result = json_object();
	frui *frui;

	if((frui = self->mb_frui)) {
		if(frui_has_board_info(frui))
			json_object_set_new(result, "board", json_pack(
					"{s:s,s:s,s:s,s:s,s:s}",
					"manufacturer", frui_get_board_manufacturer(frui),
					"name", frui_get_board_name(frui),
					"serial_number", frui_get_board_serial_number(frui),
					"part_number", frui_get_board_part_number(frui),
					"frui_file_id", frui_get_board_fru_file_id(frui)));

		if(frui_has_product_info(frui))
			json_object_set_new(result, "product", json_pack(
					"{s:s,s:s,s:s,s:s,s:s,s:s,s:s}",
					"manufacturer", frui_get_product_manufacturer(frui),
					"name", frui_get_product_name(frui),
					"part_number", frui_get_product_part_number(frui),
					"version_number", frui_get_product_version_number(frui),
					"serial_number", frui_get_product_serial_number(frui),
					"asset_tag", frui_get_product_asset_tag(frui),
					"frui_file_id", frui_get_product_fru_file_id(frui)));
	}

	return result;
}

tuber_method(STRING, IceBoard, get_motherboard_serial,
		"Retrieve the serial number from the iceboard's SPI flash.",
		0, (),
		1, (CATEGORY_ICEBOARD),
		"The IceBoard does not have jumpered serial numbers, meaning "
		"the board's SPI flash must be programmed appropriately for "
		"this function to return meaningful data."
) {
	frui *frui;
	char *serial = NULL;

	if((frui = self->mb_frui))
		serial = strdup(frui_get_board_serial_number(frui));
	return serial;
}

tuber_method(VOID, IceBoard, _motherboard_eeprom_write_base64,
		"Write the I2C EEPROM associated with a mezzanine.",
		1, ((STRING_CONST, b64, NULL, "EEPROM contents (base64 encoded)")),
		1, (CATEGORY_ICEBOARD),
		""
) {
	uint8_t *buf = NULL;
	int slen, blen;
	FILE *mtd;
	mtd_info_t mtd_info;
	erase_info_t ei;
	int x;
	frui_parser *p=NULL;
	char *warning;
	frui *frui=NULL;

	if(!(mtd = get_motherboard_ipmi_file("w")))
		goto out;
	if(ioctl(fileno(mtd), MEMGETINFO, &mtd_info) == -1) {
		oops("Unable to call ioctl(MEMGETINFO) on MTD!");
		goto out;
	}

	if((slen = base64_validate_string(b64)) == -1) {
		oops("Invalid base-64 string supplied!");
		goto out;
	}

	blen = base64_size_blob(slen);
	if(blen > mtd_info.size) {
		oops("Refusing to write %i-byte data to %i-byte partition!",
				blen, mtd_info.size);
		goto out;
	}

	if(!(buf = malloc(blen + 1))) {
		oops("Unable to allocate %i bytes for SPI flash block!", blen);
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

	/* Delete partition */
	ei.length = mtd_info.erasesize;
	for(ei.start = 0; ei.start < mtd_info.size; ei.start += mtd_info.erasesize) {
		ioctl(fileno(mtd), MEMUNLOCK, &ei);
		ioctl(fileno(mtd), MEMERASE, &ei);
	}

	/* Write EEPROM */
	if((x = fwrite(buf, 1, blen, mtd)) != blen)
		oops("Incomplete MTD write! Write %i of %i bytes.", x, blen);

	/* Replace cached IPMI data */
	if(self->mb_frui)
		frui_free(self->mb_frui);
	self->mb_frui = frui;
	frui = NULL;
out:

	if(buf)
		free(buf);
	if(mtd)
		fclose(mtd);
	if(p)
		frui_parser_free(p);
	if(frui)
		frui_free(frui);
}
