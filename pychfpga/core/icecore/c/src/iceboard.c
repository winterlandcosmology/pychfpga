#include <tuber.h>

#include "iceboard.h"
#include "iceboard_hw.h"
#include "runtime.h"

#include <string.h>
#include <syslog.h>
#include <linux/spi/spidev.h>
#include <sys/ioctl.h>
#include <unistd.h>

tuber_object(IceBoard,
		"Hardware wrapper for the McGill ICEboard.",
		"");

tuber_constructor(IceBoard,
		"Create a new ICEboard object.",
		"") {

	IceBoard *self = NULL;
	int n;
	const struct iceboard_gpio *gpio;

	if(!((self = calloc(1, sizeof(*self)))))
		fatal("Out of memory!");

	self->rread = fpga_spi_rread;
	self->rwrite = fpga_spi_rwrite;
	self->randeq = fpga_spi_randeq;
	self->roreq = fpga_spi_roreq;

	/* Create mutexes */
	self->fpga_spidev_lock = (pthread_mutex_t)PTHREAD_MUTEX_INITIALIZER;
	self->fpga_jtag_lock = (pthread_mutex_t)PTHREAD_MUTEX_INITIALIZER;
	self->i2c_mtx_lock = (pthread_mutex_t)PTHREAD_MUTEX_INITIALIZER;

	/* Open and configure SPI device */
	if(!(self->fpga_spidev = fopen(FPGA_SPIDEV, "w")))
		fatal("Unable to open SPI device %s!", FPGA_SPIDEV);

	if(ioctl(fileno(self->fpga_spidev), SPI_IOC_WR_MAX_SPEED_HZ, (uint32_t[]){FPGA_SPIDEV_CLK}) == -1)
		fatal("Unable to set SPI device speed!");

	if(ioctl(fileno(self->fpga_spidev), SPI_IOC_WR_MODE, (uint8_t[]){0}) < 0)
		fatal("Error setting SPI mode");

	/* Set up GPIOs. Note that this happens every time an iceboard object
	 * is created, which may have side-effects for some GPIOs. */
	for(n=0; n<ICEBOARD_GPIO_COUNT; n++) {
		gpio = iceboard_gpio+n;

		if(export_gpio_by_netname(gpio->name) != 0)
			fatal("Unable to export GPIO %s (number %i)",
					gpio->name, gpio->gpio_num);
	}

	/* Grab IPMI data from motherboard, backplane, and mezzanines */
	cache_mb_frui(self);

	pthread_mutex_lock(&self->i2c_mtx_lock);
	cache_mezz_frui(self, 1); /* grab mezzanine data first, since the */
	cache_mezz_frui(self, 2); /* backplane is not 100% trustworthy */
	cache_bp_slot(self); /* needs GPIOs */
	cache_bp_frui(self); /*TODO: make this happen! */
	pthread_mutex_unlock(&self->i2c_mtx_lock);

	/* Spawn DNS-SD responder */
	pthread_create(&self->dnssd_thread, NULL,
			(void*(*)(void*))dnssd_main,
			self);

	return(self);
}


tuber_destructor(IceBoard,
		"Clean up after an ICEboard reference.",
		"") {
	int n;

	for(n=0; n<NUM_MEZZANINES; n++)
		if(self->mezz_frui[n])
			frui_free(self->mezz_frui[n]);

	if(self->bp_frui)
		frui_free(self->bp_frui);

	if(self->mb_frui)
		frui_free(self->mb_frui);

	pthread_mutex_destroy(&self->fpga_spidev_lock);
	pthread_mutex_destroy(&self->i2c_mtx_lock);
	pthread_mutex_destroy(&self->fpga_jtag_lock);
	fclose(self->fpga_spidev);

	free(self);
}

tuber_method(VOID, IceBoard, reboot,
	"Reboots an iceboard.",
	0, (),
	1, (CATEGORY_ICEBOARD),
	"Reboots the dfmux."
) {
	if(!fork())
		execv("/sbin/reboot", (char *[]){"/sbin/reboot",});
}

tuber_method(VOID, IceBoard, _set_syslog_mask,
	"Sets the syslog level for the IceBoard.",
	8, (
		(BOOLEAN, emerg, json_true(), "Enable LOG_EMERG messages"),
		(BOOLEAN, alert, json_true(), "Enable LOG_ALERT messages"),
		(BOOLEAN, crit, json_true(), "Enable LOG_CRIT messages"),
		(BOOLEAN, err, json_true(), "Enable LOG_ERR messages"),
		(BOOLEAN, warning, json_true(), "Enable LOG_WARNING messages"),
		(BOOLEAN, notice, json_false(), "Enable LOG_NOTICE messages"),
		(BOOLEAN, info, json_false(), "Enable LOG_INFO messages"),
		(BOOLEAN, debug, json_false(), "Enable LOG_DEBUG messages")
	),
	1, (CATEGORY_ICEBOARD),
	"These levels correspond directly to setlogmask() parameters, and "
	"are used by the C runtime to filter which syslog() calls are "
	"passed on to the log daemon on each IceBoard. After that, any log "
	"messages that survive filtering are passed on to the syslogd daemon."
) {
	int mask=0;

	if(emerg)
		mask |= LOG_EMERG;
	if(alert)
		mask |= LOG_ALERT;
	if(crit)
		mask |= LOG_CRIT;
	if(err)
		mask |= LOG_ERR;
	if(warning)
		mask |= LOG_WARNING;
	if(notice)
		mask |= LOG_NOTICE;
	if(info)
		mask |= LOG_INFO;
	if(debug)
		mask |= LOG_DEBUG;

	setlogmask(mask);
}

tuber_method(JSON, IceBoard, _get_syslog_mask,
	"Retrieves the syslog level for the IceBoard.",
	0, (),
	1, (CATEGORY_ICEBOARD),
	"These levels correspond directly to setlogmask() parameters, and "
	"are used by the C runtime to filter which syslog() calls are "
	"passed on to the log daemon on each IceBoard. After that, any log "
	"messages that survive filtering are passed on to the syslogd daemon."
) {
	int mask = setlogmask(0);
	return json_pack("{s:b,s:b,s:b,s:b,s:b,s:b,s:b,s:b}",
		"emerg", json_boolean(mask & LOG_EMERG),
		"alert", json_boolean(mask & LOG_ALERT),
		"crit", json_boolean(mask & LOG_CRIT),
		"err", json_boolean(mask & LOG_ERR),
		"warning", json_boolean(mask & LOG_WARNING),
		"notice", json_boolean(mask & LOG_NOTICE),
		"info", json_boolean(mask & LOG_INFO),
		"debug", json_boolean(mask & LOG_DEBUG));
}

tuber_method(VOID, IceBoard, _syslog_test,
	"Sends a test message to syslog.",
	2, (
		(INTEGER, priority, NULL, "see syslog(3)"),
		(STRING_CONST, message, NULL, "see sylog(3)")
	),
	1, (CATEGORY_ICEBOARD),
	"Sends a test message to syslog(3)."
) {
	syslog(priority, "%s", message);
}

tuber_method(STRING, IceBoard, _get_syslog_buffer,
	"Dumps the contents of the log buffer.",
	0, (),
	1, (CATEGORY_ICEBOARD),
	"This function reads from /var/log/messages, so it's dependent on "
	"syslogd (or equivalent.)"
) {
	char *buf;
	FILE *f;

	if(!(f = fopen("/var/log/messages", "r"))) {
		oops("Unable to load /var/log/messages!");
		return NULL;
	}

	if(!(buf = calloc(1, 8192)))
		return NULL;

	/* Seek back a ways, then walk forwards until we find a newline */
	fseek(f, -8191, SEEK_END);
	fgets(buf, 8191, f);
	fread(buf, 1, 8191, f);
	fclose(f);
	return buf;
}

tuber_method(STRING_CONST, IceBoard, _get_personality,
	"Retrieves the IceBoard's 'personality'.",
	0, (),
	1, (CATEGORY_ICEBOARD),
	"The 'personality' is just a string constant that tells the IceBoard "
	"its purpose. This is an experiment-specific string (i.e. 'Dfmux')."
) {
	return self->personality;
}

tuber_method(VOID, IceBoard, _set_personality,
	"Sets the IceBoard's 'personality'.",
	1, ((STRING_CONST, personality, NULL, "Personality.")),
	1, (CATEGORY_ICEBOARD),
	"The 'personality' is just a string constant that tells the IceBoard "
	"its purpose. This is an experiment-specific string (i.e. 'Dfmux')."
) {
	self->personality = strndup(personality, 128);
}

tuber_method(VOID, IceBoard, _sleep,
	"Do something, slowly.",
	1, ((DOUBLE, seconds, NULL, "How long to sleep for.")),
	1, (CATEGORY_ICEBOARD),
	"This method only blocks a single thread, so you can use it to "
	"validate concurrency code upstream. Note that our web server "
	"uses something like 4 threads, so you will eventually start to "
	"serialize requests if you call this enough times on a single "
	"board."
) {
	usleep(seconds * 1e6);
}
