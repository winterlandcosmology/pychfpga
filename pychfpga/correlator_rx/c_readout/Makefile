#CC=gcc
HDF5ROOT=/home/sean/work/code/hdf5
LIBS = -L $(HDF5ROOT)/lib -lhdf5 -lhdf5_hl
INC  = -I $(HDF5ROOT)/include/ -I.

#read_corr_frames: read_corr_frames.c
	#$(CC) read_corr_frames.c -o read_corr_frames $(INC) $(LIBS) 


CC=gcc
CFLAGS=-I.  $(INC)
DEPS = ../ciceboard_receiver.h
OBJ = read_corr_frames.o ciceboard_receiver.o 

all: read_corr_frames

%.o: %.c $(DEPS)
	$(CC) -c -o $@ $< $(CFLAGS) 

read_corr_frames: $(OBJ)
	gcc -o $@ $^ $(CFLAGS) $(INC) $(LIBS) 

clean:
	rm *.o read_corr_frames
