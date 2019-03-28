import os
import sys
import getopt
import logging
import time
import pickle
import gc
from memory_profiler import profile



from datetime import datetime
import numpy as np
import h5py

def line_index_to_coordinates(N, index):
    '''
    Reuturn the i,j corrdinates of an element flattened upper triangle array.
    '''
    
    reference_triangle = np.arange(N**2).reshape((N, N))
    flat = reference_triangle[np.triu_indices(N)]
   
    i = np.zeros(len(flat))
    first = 0
    last = 0
    k = 0
    while last < len(flat):
        first = last 
        last = first + N - k
        i[np.arange(first, last)] = k
        k += 1
    
    j = flat % N

    return int(i[index]), int(j[index])

def build_corr_prod_lut(N):
    '''
    Build the correlation product index LUT.
    '''

    lookup_table = np.zeros((N, N),dtype=int)
    for i in xrange(N*(N+1)/2):
        lookup_table[line_index_to_coordinates(N, i)] = i
        
    return lookup_table

CORRELATION_LUT = build_corr_prod_lut(16)    

def unfold_triangle(data_line):

    '''
    Unfold a (N*(N+1)/2, n_freq) array into a (N,N,n_freq) correlation triangle.

    '''

    roots = np.roots([1, 1, -2 * data_line.shape[0]]) #Solve for N given the input size.
    n_ch = int(np.rint(roots.max())) #The positive root is the number of channels.

    data_array = np.zeros((n_ch, n_ch, data_line.shape[1]), dtype=complex) * np.nan
    indices = np.triu_indices(n_ch)
    
    data_array[indices] = data_line
    
    return data_array

def get_product(i, j):

    '''
    get_product_from_flattened_array(i, j)

    Returns the correlation products for i x conj(j).

    Data are stored on disk as the flattened upper triangle of the correlation matrix
    so this function is required to convert i,j to an index in the flattened array.
    This function assumes 16 channels.

    '''
    try:
        products = CORRELATION_LUT[i, j]
    except IndexError:
        print "Product index is out of bounds: i:{0:d} j:{1:d}".format(i, j)
        raise

    return products