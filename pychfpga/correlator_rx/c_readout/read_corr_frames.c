/*
 * chime_receiver.c - A simple UDP chime receiver
 * Sorts the arriving packets and writes to disk
 *  -- need to upgrade to use structs
 *  -- need to upgrade to read in configuration file for nchannels etc...
 * usage: chime_receiver <port>
 * started from http://www.cs.cmu.edu/afs/cs/academic/class/15213-f99/www/class26/udpserver.c
 */

#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <netdb.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <time.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <errno.h>
#include <signal.h>

//#include "read_corr_frames.h"

#include "../ciceboard_receiver.h"



/*
 * error - wrapper for perror
 
void error(char *msg) {
    perror(msg);
    exit(1);
}

void sig_handler(int sigNumber) {
    if (sigNumber == SIGINT) {
        printf("User interrupted, hopefully exiting nicely...\n");
        exit(0);
    }
}


typedef struct cAccumulation
 {
  unsigned int   timestamp;
  uint8_t data[34][8][BUFSIZE]; 

 } accFrame;
*/

int main(int argc, char **argv){

    //other();

    accFrame *frame = malloc(sizeof(accFrame));
    printf("Received: %d/%d\n", cget_corr_frame(frame, "38000", 3), NCMAC * NCOR);


}

int oth(int argc, char **argv) {

    int sockfd; /* socket */
    int clientlen; /* byte size of client's address */

    struct sockaddr_in serveraddr; /* server's addr */
    struct sockaddr_in clientaddr; /* client addr */
    struct hostent *hostp; /* client host info */
    char *hostaddrp; /* dotted decimal host addr string */
    int optval; /* flag value for setsockopt */
    int n; /* message byte size */


    char frame_id;  /* frame id*/
    char correlator_frame_id = 0xBF;
    uint8_t buf[BUFSIZE]; /* message buf */
    unsigned int current_timestamp;
    uint8_t corr_number, cmac_number;
    int new_acquisition = 0;
    int n_received_frames = 0;
    
        /*
     * check and use command line arguments
     */
    if (argc < 5 || argc > 6) {
        fprintf(stderr, "usage: %s <port> <outfile> <number files> <number frames> [verbose = 1/0]\n", argv[0]);
        exit(1);
    }
    int verbose = 0;
    int portno = atoi(argv[1]);
    int file_write_loops = atoi(argv[3]);
    int number_frames = atoi(argv[4]);
    if(argc == 6)
        verbose = atoi(argv[5]);

    accFrame frame;

    for(int i = 0 ; i < NCMAC ; i++)
        for(int j = 0 ; j < NCOR; j++)
            frame.received_list[i][j] = 0;


    /*
     * socket: create the parent socket
     */
    if(verbose)
        printf("Opening socket...\n");
    sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0)
        error("ERROR opening socket");

    /* setsockopt: Handy debugging trick that lets
     * us rerun the server immediately after we kill it;
     * otherwise we have to wait about 20 secs.
     * Eliminates "ERROR on binding: Address already in use" error.
     */
    optval = 1;
    setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR,
               (const void *)&optval , sizeof(int));


    /*
     * build the server's Internet address
     */
    memset((char *) &serveraddr, 0, sizeof(serveraddr));
    serveraddr.sin_family = AF_INET;
    serveraddr.sin_addr.s_addr = htonl(INADDR_ANY);
    serveraddr.sin_port = htons((unsigned short)portno);

    /*
     * bind: associate the parent socket with a port
     */
    if(verbose)
        printf("Binding socket...\n");
    if (bind(sockfd, (struct sockaddr *) &serveraddr,
             sizeof(serveraddr)) < 0)
        error("ERROR on binding");

    /*
     * main loop: wait for a datagram, sort and write to disk
     */
    clientlen = sizeof(clientaddr);
   

    if(verbose)
      printf("Entering main read loop.\n");

    while (1) {

        signal(SIGINT, sig_handler);

        //recvfrom: receive a UDP datagram or read from file

        memset(buf, 0, BUFSIZE);
          
        if(verbose==3)
          printf("Waiting for some information... ");
        n = recvfrom(sockfd, buf, BUFSIZE, 0,
                       (struct sockaddr *) &clientaddr, &clientlen);
        if(verbose==3)
          printf("Received some information.\n");

        if (n < 0)
            error("ERROR in recvfrom");

        frame_id = buf[0];
                       
        if ( frame_id == correlator_frame_id ) { //  Got a correlator frame
            
            corr_number = buf[2]; //& 0x0F;
            cmac_number = buf[3]; //% 0xFF;
            current_timestamp = (buf[11] << 24) + (buf[10] << 16) + (buf[9] << 8) +  buf[8];

            if(verbose==3)
                printf("Received a correlator frame for CMAC 0x%X and CORR 0x%X.\n", cmac_number, corr_number);

            
            if(corr_number == 0 && cmac_number == 0){
                if(verbose)
                    printf("Frames for new acquisition have arrived.\n");

                new_acquisition = 1;
                frame.timestamp = current_timestamp;
            }

            if(new_acquisition){
                n_received_frames++;
                if(current_timestamp != frame.timestamp){
                    if(verbose)
                        printf("****Received a frame that has a different timestamp than the last acquisition... Restarting.\n");
                    new_acquisition = 0;
                    for(int i = 0 ; i < NCMAC ; i++)
                        for(int j = 0 ; j < NCOR; j++)
                            frame.received_list[i][j] = 0;
                    
                }else{

                    memcpy(frame.data[cmac_number][corr_number], buf, BUFSIZE * sizeof(uint8_t));
                    frame.received_list[cmac_number][corr_number] = 1;

                    if(verbose==2)
                        printf("%5hX %5d %5d\n", frame.timestamp, cmac_number, corr_number);

                    if(corr_number == 7 && cmac_number == 33){
                        int total = 0;
                        for(int i = 0 ; i < NCMAC ; i++)
                            for(int j = 0 ; j < NCOR; j++)
                                total += frame.received_list[i][j];
                        if(verbose){
                            for(int i = 0 ; i < NCMAC ; i++){
                                for(int j = 0 ; j < NCOR; j++){
                                    printf("%d ",frame.received_list[i][j]);
                                }//for j < NCOR
                                printf("\n");
                            }//for i < NCMAC
                        
                        }//if verbose
                        if(total != NCMAC * NCOR){
                            new_acquisition = 0;
                            if(verbose)
                                printf("Did not get a full frame. Restarting.\n");
                        }

                        //if we get to this CMAC and CORR, then exit the while loop.
                        else
                            break;
                    }//if we are at the last corr and cmac
                }//if timestamps match
            }//if new_acquisition
        }//if we have a correlator frame
    }//while(1)
  
    return n_received_frames;
    

}
