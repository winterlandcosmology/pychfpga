/* 
 * chime_receiver.c - A simple UDP chime receiver
 * Sorts the arriving packets and writes to disk
 *  -- need to upgrade to use structs
 *  -- need to upgrade to read in configuration file for nchannels etc...
 * usage: chime_receiver <port>
 * started from http://www.cs.cmu.edu/afs/cs/academic/class/15213-f99/www/class26/udpserver.c
 * 
 * This is a port of the linux version to windows.
 * Replaced the close() calls with their windows equivalent closesocket(). Pushed all variable declarations to the top of their function.
 * Commented out unused variables *hostp, *hostaddrp.
 */

#include "ciceboard_receiver_win.h"

int keepRunning = 1;

/*
 * error - wrapper for perror
 */
void error(char *msg) {
  printf("error\n");
  perror(msg);
  keepRunning = 0;
}

void sig_handler(int sigNumber) {
    if (sigNumber == SIGINT) {
        printf("User interrupted, hopefully exiting nicely...\n");
        exit(0);
    }
}


int cget_corr_frame(accFrame *frame, char *port, int verbose){


    int sockfd; /* socket */
    int clientlen; /* byte size of client's address */
    int portno = atoi(port);
    struct sockaddr_in serveraddr; /* server's addr */
    struct sockaddr_in clientaddr; /* client addr */
    //struct hostent *hostp; /* client host info */
    //char *hostaddrp; /* dotted decimal host addr string */
    int optval; /* flag value for setsockopt */
    int n; /* message byte size */
    int i, j;

    char frame_id;  /* frame id*/
    char correlator_frame_id = 0xBF;

    uint8_t buf[BUFSIZE]; /* message buf */


    unsigned int current_timestamp;
    uint8_t corr_number, cmac_number;
    int new_acquisition = 0;
    int n_received_frames = 0;

    unsigned int frame_timestamps[NCMAC][NCOR] = {0};
    //int received[NCMAC][NCOR] = {0};

    /*
     * socket: create the parent socket
     */
//    if(verbose)
        //printf("Opening socket...\n");
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
//    if(verbose)
//        printf("Binding socket...\n");
    if (bind(sockfd, (struct sockaddr *) &serveraddr,
             sizeof(serveraddr)) < 0)
        error("ERROR on binding");

    /*
     * main loop: wait for a datagram, sort and write to disk
     */
    clientlen = sizeof(clientaddr);
   


    //if(verbose)
    //  printf("Entering main read loop.\n");

    while (1) {

        signal(SIGINT, sig_handler);

        //recvfrom: receive a UDP datagram or read from file

        memset(buf, 0, BUFSIZE);
          
        //if(verbose==3)
        //  printf("Waiting for some information... ");
        n = recvfrom(sockfd, buf, BUFSIZE, 0,
                       (struct sockaddr *) &clientaddr, &clientlen);
        //if(verbose==3)
        //  printf("Received some information.\n");

        if (n < 0)
            error("ERROR in recvfrom");

        frame_id = buf[0];
                       
        if ( frame_id == correlator_frame_id ) { //  Got a correlator frame
            
            corr_number = buf[2]; //& 0x0F;
            cmac_number = buf[3]; //% 0xFF;
            current_timestamp = (buf[11] << 24) + (buf[10] << 16) + (buf[9] << 8) +  buf[8];

            if(verbose==2)
                printf("Received a correlator frame for CMAC 0x%X and CORR 0x%X.\n", cmac_number, corr_number);

            
            if(current_timestamp != frame->timestamp){
              if(verbose==1 || verbose == 3 || verbose == 4)
                if(n_received_frames == 0)
                  printf("****Received first frame in a new acquisition.\n");
                else{
                  printf("****Received a frame that has a different timestamp than the last acquisition... Restarting.\n");
                  printf("\t\t\t  %X vs %X\n",current_timestamp, frame->timestamp);
                }

              for(i = 0 ; i < NCMAC ; i++)
                  for(j = 0 ; j < NCOR; j++)
                      frame->received_list[i][j] = 0;      

              frame->timestamp = current_timestamp;
              n_received_frames = 0;

            }
            n_received_frames++;
            memcpy(frame->data[cmac_number][corr_number], buf, BUFSIZE * sizeof(uint8_t));
            frame->received_list[cmac_number][corr_number] = 1;
            frame_timestamps[cmac_number][corr_number] = current_timestamp;

            if(verbose==3 || verbose==4)
                printf("%5hX %5d %5d %5d\n", frame->timestamp, cmac_number, corr_number, n_received_frames);

            if(n_received_frames == 8*34){                      
                if(verbose==1 || verbose == 3){
                    for(i = 0 ; i < NCMAC ; i++){
                        for(j = 0 ; j < NCOR; j++){
                            printf("%d ",frame->received_list[i][j]);
                        }//for j < NCOR
                        printf("\n");
                    }//for i < NCMAC

                    for(i = 0 ; i < NCMAC ; i++){
                        for(j = 0 ; j < NCOR; j++){
                            printf("%d ",frame_timestamps[i][j]);
                        }//for j < NCOR
                        printf("\n");
                    }//for i < NCMAC
                
                }//if verbose

                //if we have received everything, then exit the main loop.
                break;
            }//if we are at the last corr and cmac
        }//if we have a correlator frame
    }//while(1)
    closesocket(sockfd);
    return n_received_frames;
}


int cget_frame(singleTime *singleTimestamp, char *port, int number_channels, int verbose){
  //singleTime singleTimestamp;
  //Relabel channels
  int remap[] = {12,13,14,15,8,9,10,11,4,5,6,7,0,1,2,3};


  int sockfd; /* socket */
  int portno; /* port to listen on */
  int clientlen; /* byte size of client's address */
  struct sockaddr_in serveraddr; /* server's addr */
  struct sockaddr_in clientaddr; /* client addr */
  uint8_t buf[BUFSIZE]; /* message buf */
  int optval; /* flag value for setsockopt */
  int n; /* message byte size */
  char frame_id;  /* frame id*/
  char ant_channel;  
  char timestream_frame_id_value = 0xA0;
  unsigned short stream_id;
  unsigned short word_length;
  uint32_t timestamp;
  uint32_t last_timestamp;
  int x; /* Loop variable */
  int ant_packet_count = 0;
  int first = 1;
  int i, j;

  keepRunning = 1;

  portno = atoi(port);
    /* 
   * socket: create the parent socket 
   */
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
  if (bind(sockfd, (struct sockaddr *) &serveraddr, 
     sizeof(serveraddr)) < 0) 
    error("ERROR on binding");

  /* 
   * main loop: wait for a datagram, sort and write to disk
   */
  clientlen = sizeof(clientaddr);
  //printf("buffer size %d\n", BUFSIZE);

  x=0;
  ant_packet_count = 0;
  first = 1;
  //printf("reading on port...\n");
  while (  keepRunning) {
      signal(SIGINT, sig_handler);
      //printf("keepRunning %d\n", keepRunning);
      /*
      * recvfrom: receive a UDP datagram or read from file
      */
      memset(buf, 0, BUFSIZE);
      
      n = recvfrom(sockfd, buf, BUFSIZE, 0,
      (struct sockaddr *) &clientaddr, &clientlen);
      if (n < 0)
        error("ERROR in recvfrom");
      //printf("number of bytes received: %d\n", n);



      frame_id = buf[0] & 0xF0;
      //printf("%x\n",buf[0]); 
      if ( frame_id == timestream_frame_id_value ) { /*  Got a timestream frame*/
          if(verbose)
            printf("got a frame\n");
          /* unpack the header timestamp*/

          timestamp = (uint32_t) (((uint32_t)buf[5]<<24) | ((uint32_t)buf[6]<<16) | ((uint32_t)buf[7]<<8) | buf[8]);
          if(verbose)
            printf("header has timestamp %d\n", timestamp);
          if (first == 1){
              last_timestamp = timestamp;
              first = 0;
              if(verbose)
                printf("Set Timestamps to start %d new and %d old\n", timestamp, last_timestamp);
          }

          if( timestamp == last_timestamp){
              ant_channel = buf[0] & 0x0F;
              stream_id = (uint16_t) (((uint16_t)buf[1]<<8) | (buf[2]));
              word_length = (uint16_t) (((uint16_t)buf[3]<<8) | (buf[4]));
              singleTimestamp->timestamp = timestamp;
              //singleTime.antenna = ant_channel;
              for (i = 0; i < nsamples; ++i){
                singleTimestamp->timestream[ant_channel][i] = buf[i+9];
              }
              if(verbose)
                printf("##################building packet timestamp %d, ant channel %d, packet number %d\n", timestamp, ant_channel, ant_packet_count);
              ant_packet_count += 1;
              
            }
          else if ( (timestamp != last_timestamp) && (ant_packet_count == number_channels) ){
              keepRunning = 0;
          }
          else if( (timestamp != last_timestamp) && (ant_packet_count < number_channels) ){
              if(verbose)
                printf("got new timestamp %u, old timestamp %u, before finishing with only %d antennas, starting over... %u\n", timestamp,last_timestamp, ant_packet_count, first);
              for (j = 0; j < number_channels; ++j){
                 for (i = 0; i < nsamples; ++i){
                   singleTimestamp->timestream[j][i] = 0;
                 }
              }
              // Start populating first new one now.  
              ant_channel = buf[0] & 0x0F;
              stream_id = (uint16_t) (((uint16_t)buf[1]<<8) | (buf[2]));
              word_length = (uint16_t) (((uint16_t)buf[3]<<8) | (buf[4]));
              singleTimestamp->timestamp = timestamp;
              //singleTime.antenna = ant_channel;
              for (i = 0; i < nsamples; ++i){
                singleTimestamp->timestream[ant_channel][i] = buf[i+9];
              }
              ant_packet_count = 1;
              last_timestamp = timestamp;
              timestamp = 0;
          }
          else{
              printf("got bad data?  not sure how got here!\n");
          }
        }

  }
  closesocket(sockfd);
  return 0;
}