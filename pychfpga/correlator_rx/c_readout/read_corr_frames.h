/* 
* Header for ctimestream_receiver.c
 */

#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <netdb.h>
#include <sys/types.h> 
#include <sys/socket.h>
#include <time.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <errno.h>
#include <signal.h>
#include "hdf5.h"
#include "hdf5_hl.h"

#define BUFSIZE 32767

#define NCMAC 34
#define NCOR  8
//#define nsamples 2048
//#define nantenna 16
//#define nprod = nsamples * (nsamples+1) / 2

/*
 * error - wrapper for perror
 */
void error(char *msg);

void sig_handler(int sigNumber);

