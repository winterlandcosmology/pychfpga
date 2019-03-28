/* 
* Header for ctimestream_receiver.c
*
* Commented out any UNIX specific libraries for the windows version, as well as added the winsocket2 library.
* Defined all the standard integer types from the missing libraries in the typedef block.
*
 */

#include <stdio.h>
//#include <unistd.h>
#include <stdlib.h>
#include <string.h>
//#include <netdb.h>
#include <sys/types.h> 
//#include <sys/socket.h>
#include <winsock2.h>
#include <time.h>
//#include <netinet/in.h>
//#include <arpa/inet.h>
#include <errno.h>
#include <signal.h>

#define BUFSIZE 5*512 + 12
#define NCMAC 34
#define NCOR  8
#define nsamples 2048
#define nantenna 16
#define nprod = nsamples * (nsamples+1) / 2

typedef __int8              int8_t;
typedef __int16             int16_t;
typedef __int32             int32_t;
typedef __int64             int64_t;
typedef unsigned __int8     uint8_t;
typedef unsigned __int16    uint16_t;
typedef unsigned __int32    uint32_t;
typedef unsigned __int64    uint64_t;

/*
 * error - wrapper for perror
 */
void error(char *msg);

void sig_handler(int sigNumber);

typedef struct {
    int32_t real;  /* Real part */
    int32_t imag;  /* Imaginary part */
  } complex_t;

typedef struct accumulation
 {
  unsigned int   timestamp;
  uint8_t data[NCMAC][NCOR][BUFSIZE]; /* message buf */
  uint8_t received_list[NCMAC][NCOR]; /* received frame list */

 } accFrame;

typedef struct spectrum
 {
  unsigned int   timestamp;
  int8_t timestream[nantenna][nsamples];
 } singleTime;



 
int cget_corr_frame(accFrame *frame, char *port, int verbose);

int cget_frame(singleTime *singleTimestamp, char *port, int number_channels, int verbose);
