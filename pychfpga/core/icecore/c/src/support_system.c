#include <stdarg.h>
#include <stdlib.h>
#include <unistd.h>
#include <stdio.h>
#include <sys/types.h>
#include <syslog.h>

#include "support.h"

/*
 * Error Globals
 */

void fatal_call(const char *fmt, ...) {
	va_list ap;
	FILE *f;
	char fnbuf[32];

	/* First complain to stderr */
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);

	/* Then complain to syslog */
	openlog("tuber", LOG_CONS | LOG_PID, LOG_DAEMON);
	va_start(ap, fmt);
	vsyslog(LOG_CRIT, fmt, ap);
	va_end(ap);
	closelog();

	/* Finally, complain to a file in /tmp */
	sprintf(fnbuf, "/tmp/fatal.%i", (int)getpid());
	va_start(ap, fmt);
	if((f = fopen(fnbuf, "w"))) {
		vfprintf(f, fmt, ap);
		fclose(f);
	}
	va_end(ap);

	abort();
}

int fdscanf(int fd, const char *fmt, ...) {
	char buf[256];
	va_list ap;
	int x;

	x = read(fd, buf, sizeof(buf)-1);
	if(x<=0)
		return(x);

	va_start(ap, fmt);
	x = vsscanf(buf, fmt, ap);
	va_end(ap);

	return(x);
}
