import numpy as np

def pfb_fft(data, N=4):
    """ Computes a Polyphase-filter-bank real FFT.
    
    ``data`` is a (n_frames x frame_size) array.
    ``N`` is the number of frames across which the windowing is applied.

    Uses the sinc + Hamming window function. 
    
    Returns a real fft for `n_frames-3` frames. This function is interchangeable with the Numpy fft.rfft()   
    """
    data = np.array(data)  # make sure this is a numpy array
    number_of_frames, frame_length = data.size

    # Compute window function to be applied to N consecutive frames
    sinc_window = np.sinc((np.arange(-frame_length * N/2, frame_length * N/2) + 0.5) / frame_length)  # the 0.5 offset is needed to make sure the sinc function is symmetric
    hamming_window = np.hamming(frame_length * N)
    window = np.reshape((sinc_window * hamming_window), (4, -1)) # Split the window in (4 x frame_length) array

    # Apply the window to each N frames
    windowed_data = [np.sum(data[n:n+4, :] * window, axis=0) for n in range(0, number_of_frames-4+1)]

    # compute the real FFT
    return np.fft.rfft(windowed_data)
