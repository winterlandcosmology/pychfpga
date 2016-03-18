#include <fcntl.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>

#include <tuber.h>

#include "iceboard.h"
#include "iceboard_hw.h"
#include "i2c_eeprom.h"

#include <errno.h>
#include <syslog.h>

struct iceboard_gpio *get_gpio_by_netname(const char *name) {
	int n;
	struct iceboard_gpio *gpio;

	/* Find the GPIO in our look-up table */
	for(n=0; n<ICEBOARD_GPIO_COUNT; n++) {
		gpio = iceboard_gpio+n;

		if(strcmp(name, gpio->name) == 0)
			return(gpio);
	}

	return(NULL);
}

int export_gpio_by_netname(const char *name) {
	FILE *f;
	char buf[256];
	struct iceboard_gpio *gpio;

	if(!(gpio = get_gpio_by_netname(name)))
		return(-1);

	/* See if the GPIO is already exported. */
	sprintf(buf, "/sys/class/gpio/%s", gpio->name);
	if(access(buf, R_OK|W_OK|X_OK) != 0) {
		/* Doesn't exist or permissions weird. Try exporting. If that
		 * fails too, then complain. */
		f = fopen("/sys/class/gpio/export", "w");
		if(!f)
			return(-2);
		if(fprintf(f, "%i\n", gpio->gpio_num) <= 0)
			return(-3);
		fclose(f);
	}

	/* Set up the direction; open direction and value files. If the
	 * direction has already been correctly set, don't re-assign it:
	 * this can clobber outputs! */
	sprintf(buf, "/sys/class/gpio/%s/direction", gpio->name);
	if((gpio->dir_fd = open(buf, O_RDWR)) == -1)
		return(-4);
	read(gpio->dir_fd, buf, sizeof(buf));
	if(gpio->mode==INPUT && buf[0] != 'i')
		dprintf(gpio->dir_fd, "in\n");
	else if(gpio->mode==OUTPUT && buf[0] != 'o')
		dprintf(gpio->dir_fd, "out\n");

	sprintf(buf, "/sys/class/gpio/%s/value", gpio->name);
	if((gpio->value_fd = open(buf, O_RDWR)) == -1)
		return(-5);

	return(0);
}

void gpio_set(struct iceboard_gpio *self, int value) {
	/* To de-glitch, use the direction and not the value interface. See
	 * kernel Documentation/gpio.txt. */
	dprintf(self->dir_fd, value ? "high" : "low");
}

int gpio_get(struct iceboard_gpio *self, bool set_input) {
	char buf[4];

	if(set_input)
		dprintf(self->dir_fd, "in\n");

	pthread_mutex_lock(&self->lock);
	lseek(self->value_fd, 0, SEEK_SET);
	read(self->value_fd, buf, sizeof(buf));
	pthread_mutex_unlock(&self->lock);

	return(buf[0]=='1');
}

tuber_method(STRING_CONST, IceBoard, get_clock_source,
		"Returns the type of clock input selected by iceboard jumpers",
		0, (),
		1, (CATEGORY_ICEBOARD),
		"Checks the state of ARMClkSel0 and ARMClkSel1 lines, "
		"and computes if Backplane (bp), the SMA(sma) or, "
		"crystal (xtal) was selected."
) {

	struct iceboard_gpio *clksel0, *clksel1;

	/* Obtain GPIO references */
	if(!(clksel0 = get_gpio_by_netname("ARMClkSel0")) ||
			!(clksel1 = get_gpio_by_netname("ARMClkSel1")))  {
		oops("Unable to obtain ARMClkSel0, ARMClkSel1!");
		return NULL;
	}

	/* Make sure we aren't asserting ARMClkSel0, ARMClkSel1 */
	(void)gpio_get(clksel0, 1);
	(void)gpio_get(clksel1, 1);

	/* Checking clock source type */
	if(gpio_get(clksel1, 0))
		return CLOCK_SOURCE_XTAL;

	if( gpio_get(clksel0, 0))
		return CLOCK_SOURCE_SMA;

	return CLOCK_SOURCE_BP;
}
