#include <syslog.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <syslog.h>
#include <stdint.h>

#include "iceboard.h"
#include "ipmi_frui.h"

#include <avahi-common/simple-watch.h>
#include <avahi-common/error.h>
#include <avahi-common/malloc.h>
#include <avahi-common/domain.h>
#include <avahi-client/client.h>
#include <avahi-client/publish.h>


static AvahiClient *client = NULL;
static AvahiSimplePoll *simple_poll = NULL;

typedef struct {
	IceBoard *ib;
	char *hostname;
	char *stype;
	char *domain;
	char *host;
	uint16_t port;
	AvahiStringList *txt;
	AvahiEntryGroup *group;
} Config;

static int register_stuff(Config *config);

static void entry_group_callback(AvahiEntryGroup *g, AvahiEntryGroupState state, Config *config) {
	assert(g);
	assert(config);

	switch (state) {

	case AVAHI_ENTRY_GROUP_ESTABLISHED:
		syslog(LOG_INFO, "Established under name '%s'", config->hostname);
		break;

	case AVAHI_ENTRY_GROUP_FAILURE:
		syslog(LOG_ERR, "Failed to register: %s", avahi_strerror(avahi_client_errno(client)));
		break;

	case AVAHI_ENTRY_GROUP_COLLISION: {
		syslog(LOG_ERR, "Name collision! (%s)", config->hostname);
		break;
	}

	case AVAHI_ENTRY_GROUP_UNCOMMITED:
	case AVAHI_ENTRY_GROUP_REGISTERING:
		break;
	}
}

static int register_stuff(Config *config) {
	char cname[64];
	char hostname_formatted[64];

	assert(config);
	assert(config->ib);

	if(config->group)
		avahi_entry_group_reset(config->group);

	if(!(config->group = avahi_entry_group_new(client,
					(AvahiEntryGroupCallback)entry_group_callback,
					config))) {
		syslog(LOG_ERR, "Failed to create entry group: %s",
				avahi_strerror(avahi_client_errno(client)));
		return -1;
	}

	if (avahi_entry_group_add_service_strlst(config->group, AVAHI_IF_UNSPEC, AVAHI_PROTO_UNSPEC, 0,
			config->hostname, config->stype, config->domain,
			config->host, config->port,
			config->txt) < 0) {

		syslog(LOG_ERR, "Failed to add service: %s", avahi_strerror(avahi_client_errno(client)));
		return -1;
	}

	if(config->ib->bp_frui) {
		/* Publish CNAME so we can address this board by crate */
		memset(cname, 0, sizeof(cname));
		snprintf(cname, sizeof(cname)-1, "slot%i.icecrate%s.local",
				config->ib->bp_slot,
				frui_get_board_serial_number(config->ib->bp_frui));

		memset(hostname_formatted, 0, sizeof(hostname_formatted));
		snprintf(hostname_formatted, sizeof(hostname_formatted)-1,
				"%c%s%clocal",
				strlen(config->hostname),
				config->hostname,
				strlen("local"));

		syslog(LOG_INFO, "Trying to publish CNAME %s...", cname);

		if(avahi_entry_group_add_record(config->group, AVAHI_IF_UNSPEC, AVAHI_PROTO_UNSPEC,
				AVAHI_PUBLISH_USE_MULTICAST|AVAHI_PUBLISH_ALLOW_MULTIPLE,
				cname, AVAHI_DNS_CLASS_IN, AVAHI_DNS_TYPE_CNAME, AVAHI_DEFAULT_TTL,
				hostname_formatted,
				strlen(config->hostname) + strlen("local") + 3) < 0) {
				/* hostname + local + 2 lengths + 1 null */
			syslog(LOG_ERR, "Unable to publish CNAME! (%s)",
					avahi_strerror(avahi_client_errno(client)));
			return -1;
		}
	}

	if(avahi_entry_group_commit(config->group) < 0) {
		syslog(LOG_ERR, "Unable to commit Avahi group! (%s)",
				avahi_strerror(avahi_client_errno(client)));
		return -1;
	}

	return 0;
}

static void client_callback(AvahiClient *c,
		AvahiClientState state,
		Config *config)
{
	int error;

	client = c;

	switch (state) {
	case AVAHI_CLIENT_FAILURE:

		if (avahi_client_errno(c) == AVAHI_ERR_DISCONNECTED) {
			syslog(LOG_NOTICE, "Reconnecting to Avahi...");

			avahi_client_free(client);
			client = NULL;
			config->group = NULL;

			if (!(client = avahi_client_new(avahi_simple_poll_get(simple_poll),
							AVAHI_CLIENT_NO_FAIL,
							(AvahiClientCallback)client_callback,
							config, &error))) {
				syslog(LOG_ERR, "Failed to create avahi client (%s)", avahi_strerror(error));
				avahi_simple_poll_quit(simple_poll);
			}
		} else {
			syslog(LOG_ERR, "Client failure, exiting (%s)", avahi_strerror(avahi_client_errno(c)));
			avahi_simple_poll_quit(simple_poll);
		}

		break;

	case AVAHI_CLIENT_S_RUNNING:
		if (register_stuff(config) < 0) {
			syslog(LOG_ERR, "Failure during register_stuff()!");
			avahi_simple_poll_quit(simple_poll);
		}
		break;

	case AVAHI_CLIENT_S_COLLISION:
	case AVAHI_CLIENT_S_REGISTERING:

		if (config->group) {
			avahi_entry_group_free(config->group);
			config->group = NULL;
		}
		break;

	case AVAHI_CLIENT_CONNECTING:
		break;
	}
}

AvahiStringList *register_ipmi(AvahiStringList *txt, const char *prefix, frui *frui) {
	char txt_serial[64], txt_part[64], txt_manuf[64];
	const char *serial, *part, *manuf;

	/* If we can't add anything, don't. */
	if(!frui)
		return txt;

	serial = frui_get_board_serial_number(frui);
	snprintf(txt_serial, sizeof(txt_serial)-1, "%s-serial=%s", prefix, serial);
	txt = avahi_string_list_add(txt, txt_serial);

	part = frui_get_board_part_number(frui);
	snprintf(txt_part, sizeof(txt_part)-1, "%s-part=%s", prefix, part);
	txt = avahi_string_list_add(txt, txt_part);

	manuf = frui_get_board_manufacturer(frui);
	snprintf(txt_manuf, sizeof(txt_manuf)-1, "%s-manufacturer=%s", prefix, manuf);
	txt = avahi_string_list_add(txt, txt_manuf);

	return txt;
}

void rebuild_txt(Config *config) {
	char txt_slot[64];

	if(config->txt) {
		avahi_string_list_free(config->txt);
		config->txt = NULL;
	}

	/* Motherboard */
	config->txt = register_ipmi(config->txt, "motherboard", config->ib->mb_frui);

	/* Mezzanine */
	config->txt = register_ipmi(config->txt, "mezzanine1", config->ib->mezz_frui[0]);
	config->txt = register_ipmi(config->txt, "mezzanine2", config->ib->mezz_frui[1]);

	/* Backplane */
	if(config->ib->bp_frui) {
		config->txt = register_ipmi(config->txt, "backplane", config->ib->bp_frui);
		snprintf(txt_slot, sizeof(txt_slot)-1, "backplane-slot=%i", config->ib->bp_slot);
		config->txt = avahi_string_list_add(config->txt, txt_slot);
	}
}

static void watch_callback(AvahiWatch *w, int fd, AvahiWatchEvent event, Config *config) {
	int s;
	ssize_t l;

	assert(w);
	assert(event == AVAHI_WATCH_IN);

	l = read(fd, &s, sizeof(s));
	assert(l == sizeof(s));

	syslog(LOG_INFO, "Got TXT record update.");

	rebuild_txt(config);
	register_stuff(config);
}

int dnssd_main(IceBoard *self)
{
	const char *mb_serial;
	char hostname[64], svcname[64], fqhn[64];
	int error;
	Config *config;
	const AvahiPoll *p;
	int pipe_fds[2];

	if(!self->mb_frui) {
		syslog(LOG_WARNING, "Unable to retrieve IPMI data from SPI flash!");
		return(-1);
	}

	mb_serial = frui_get_board_serial_number(self->mb_frui);

	/* Host name phase: use sethostname(), and advise Avahi about the new
	 * name. */
	snprintf(hostname, sizeof(hostname)-1, "iceboard%s", mb_serial);
	snprintf(fqhn, sizeof(fqhn)-1, "iceboard%s.local.", mb_serial);
	snprintf(svcname, sizeof(svcname-1), "iceboard%s._tuber-jsonrpc._tcp", mb_serial);
	if(sethostname(hostname, strlen(hostname)) == -1) {
		syslog(LOG_WARNING, "Unable to set hostname (%s, %s)",
				hostname, strerror(errno));
		return(-1);
	}

	config = &(Config){
		.ib = self,
		.hostname = hostname,
		.domain = "local",
		.stype = "_tuber-jsonrpc._tcp",
		.host = fqhn,
		.port = 80,
		.txt = NULL,
		.group = NULL,
	};

	rebuild_txt(config);

	/* Connect to avahi */
	if (!(simple_poll = avahi_simple_poll_new())) {
		syslog(LOG_ERR, "Failed to create simple poll object.");
		fprintf(stderr, "Failed to create simple poll object.\n");
		goto fail;
	}

	/* We keep a watched FD around to alert avahi to TXT record changes. */
	if(pipe2(pipe_fds, O_NONBLOCK) == -1) {
		syslog(LOG_ERR, "Failed to create pipe() file descriptors!  (%s)", strerror(errno));
		fprintf(stderr, "Failed to create pipe() file descriptors!  (%s)", strerror(errno));
		goto fail;
	}
	p = avahi_simple_poll_get(simple_poll);
	p->watch_new(p, pipe_fds[0], AVAHI_WATCH_IN, (AvahiWatchCallback)watch_callback, config);
	self->dnssd_txtupdate_fd = pipe_fds[1];

	if (!(client = avahi_client_new(avahi_simple_poll_get(simple_poll), 0,
					(AvahiClientCallback)client_callback,
					config, &error))) {
		syslog(LOG_ERR, "Failed to create client object: %s", avahi_strerror(error));
		fprintf(stderr, "Failed to create client object: %s\n", avahi_strerror(error));
		goto fail;
	}

	error = avahi_client_set_host_name(client, hostname);
	if (error < 0 && error != AVAHI_ERR_NO_CHANGE) {
		syslog(LOG_ERR, "Failed to create host name resolver: %s", avahi_strerror(avahi_client_errno(client)));
		fprintf(stderr, "Failed to create host name resolver: %s\n", avahi_strerror(avahi_client_errno(client)));
		goto fail;
	}

	avahi_simple_poll_loop(simple_poll);

	return(0);

fail:

	if (client)
		avahi_client_free(client);

	if (simple_poll)
		avahi_simple_poll_free(simple_poll);

	return -1;
}
