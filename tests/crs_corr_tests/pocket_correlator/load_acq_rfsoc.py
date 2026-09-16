import numpy as np
import h5py
import os

def load_data(acq, 
              data_path, 
              gains_path, 
              file_numbers = None,
              trim_data = True,
              apply_counts = True,
              apply_gains = True,
              ):

    # Determine firmware architecture:
    if int(acq[0:4]) < 2025:
        firmware_type = 'shifted_fft_common_log_gain'
    elif int(acq[0:4]) == 2025 or int(acq[0:4]) > 2025:
        if int(acq[4:6]) <= 3:
            if int(acq[4:6]) == 3 and int(acq[6:8]) < 11:
                firmware_type = 'shifted_fft_common_log_gain'
            if int(acq[4:6]) == 3 and int(acq[6:8]) >= 11:
                firmware_type = 'bitgrowth_fft_floating_point_gain'
            else:
                firmware_type = 'shifted_fft_common_log_gain'
        else:
            firmware_type = 'bitgrowth_fft_floating_point_gain'
            
    print(f'Firmware type is {firmware_type}')

    # Load data arrays
    if file_numbers is None:
        file_names = os.listdir(f'{data_path}/{acq}')
        N_FILES = len(file_names)
    
        np.sort([int(os.listdir(data_path + '/' + acq)[n].split('.hdf5')[0]) for n in range(len(os.listdir(data_path + '/' + acq)))])
        file_numbers = np.sort([int(file_name.split('.hdf5')[0]) for file_name in file_names])

    else:
        N_FILES = len(file_numbers)
    
    ctr = 0 # counter
    for n in file_numbers: # range(N_FILES):
        print(f'Loading data file {data_path}/{acq}/{n}.hdf5')
        df = h5py.File(f'{data_path}/{acq}/{n}.hdf5', 'r')
        
        # Gather the products and frequencies from the first data file only,
        # since we don't need to load this more than once. We use the counter 
        # just in case 
        if ctr == 0:
            prod = df['index_map']['prod'][:]
            f = df['index_map']['freq'][:]
            NBINS = len(f)

            N_PRODS = len(prod)

            n_firmware_frames = df['vis'].attrs['n_firmware_frames']
            n_software_frames = df['vis'].attrs['n_software_frames']

            vis_all = df['vis'][:]
            counts_all = df['counts'][:]
            sat_all = df['sat'][:]
            t_all = df['index_map']['time'][:]

            if firmware_type == 'shifted_fft_common_log_gain':
                adc_overflows_all = df['stats']['adc_overflows'][:]

            if firmware_type == 'bitgrowth_fft_floating_point_gain':
                adc_overrange_all = df['stats']['adc_overrange'][:]
                adc_overvoltage_all = df['stats']['adc_overvoltage'][:]

            frame_count_all = df['stats']['frame_count'][:]
            fft_overflows_all = df['stats']['fft_overflows'][:]
            scaler_overflows_all = df['stats']['scaler_overflows'][:]
            scaler_stats_ready_all = df['stats']['scaler_stats_ready'][:]
            corr_overrun_all = df['stats']['corr_overrun'][:]

        else:
            vis_all = np.concatenate((vis_all, df['vis'][:]))
            counts_all = np.concatenate((counts_all, df['counts'][:]))
            sat_all = np.concatenate((sat_all, df['sat'][:]))
            t_all = np.concatenate((t_all, df['index_map']['time'][:]))

            if firmware_type == 'shifted_fft_common_log_gain':
                adc_overflows_all = np.concatenate((adc_overflows_all, df['stats']['adc_overflows'][:]))

            if firmware_type == 'bitgrowth_fft_floating_point_gain':
                adc_overrange_all = np.concatenate((adc_overrange_all, df['stats']['adc_overrange'][:]))
                adc_overvoltage_all = np.concatenate((adc_overvoltage_all, df['stats']['adc_overrange'][:])) 

            corr_overrun_all = np.concatenate((corr_overrun_all, df['stats']['corr_overrun'][:]))
            fft_overflows_all = np.concatenate((fft_overflows_all, df['stats']['fft_overflows'][:]))
            frame_count_all = np.concatenate((frame_count_all, df['stats']['frame_count'][:]))
            scaler_overflows_all = np.concatenate((scaler_overflows_all, df['stats']['scaler_overflows'][:]))
            scaler_stats_ready_all = np.concatenate((scaler_stats_ready_all, df['stats']['scaler_stats_ready'][:]))

        # Close file
        df.close()

        # Increment counter
        ctr += 1

    if trim_data:

        # Determine which indices need to be trimmed, based on whether or not they have all their expected counts. We also trim
        # the first four bins by default because they are not sent by the RFSoC anyways.

        if int(acq[0:4]) < 2025:
            counts_format_new = False
        elif int(acq[0:4]) == 2025 or int(acq[0:4]) > 2025:
            if int(acq[4:6]) <= 10:
                if int(acq[4:6]) == 10 and int(acq[6:8]) < 13:
                    counts_format_new = False
                if int(acq[4:6]) == 10 and int(acq[6:8]) >= 13:
                    counts_format_new = True
                else:
                    counts_format_new = False
            else:
                counts_format_new = True

        if counts_format_new:
            print('Counts format is new (later than or equal to October 13, 2025)')
            delete_capture_idx = np.where(counts_all != n_software_frames*len(f))[0]
        else:
            print('Counts format is old (earlier than October 13, 2025)')
            delete_capture_idx = []
            for n in range(counts_all.shape[0]):
                # print(n, len(np.where(counts_all[n, 4:] != vis_1s_nsf)[0]), counts_all[n, 4:][np.where(counts_all[n, 4:] != n_software_frames)[0]])
                if not all(counts_all[n, 4:] == n_software_frames):
                    delete_capture_idx.append(n)

        # Trim data accordingly.
        print(f'Trimming {len(delete_capture_idx)} captures out of {vis_all.shape[0]} total captures')
        print(f'Trim indices: {delete_capture_idx}')

        # If there's nothing to trim, change the delete_capture_index to None
        if len(delete_capture_idx) == 0:
                delete_capture_idx = None
            
        # Data
        f = np.copy(f[4:])
        vis_all = np.delete(vis_all[:, 4:], delete_capture_idx, axis = 0)
        counts_all = np.delete(counts_all, delete_capture_idx, axis = 0)
        sat_all = np.delete(sat_all[:, 4:], delete_capture_idx, axis = 0)
        t_all = np.delete(t_all, delete_capture_idx, axis = 0)
        
        # Statistics
        if firmware_type == 'shifted_fft_common_log_gain':
            adc_overflows_all = np.delete(adc_overflows_all, delete_capture_idx, axis = 0)

        if firmware_type == 'bitgrowth_fft_floating_point_gain':
            adc_overrange_all = np.delete(adc_overrange_all, delete_capture_idx, axis = 0)
            adc_overvoltage_all = np.delete(adc_overvoltage_all, delete_capture_idx, axis = 0)
            
        corr_overrun_all = np.delete(corr_overrun_all, delete_capture_idx, axis = 0)
        fft_overflows_all = np.delete(fft_overflows_all, delete_capture_idx, axis = 0)
        scaler_overflows_all = np.delete(scaler_overflows_all, delete_capture_idx, axis = 0)
        scaler_stats_ready_all = np.delete(scaler_stats_ready_all, delete_capture_idx, axis = 0)

        # Print a sanity check that all the counts are valid for the trimmed datasets
        if counts_format_new:
            if all(counts_all == n_software_frames*NBINS):
                print(f'All counts have the expected {n_software_frames} software frame(s) after trimming.')
            else:
                print(f'WARNING! Not all counts have the expected {n_software_frames} software frame(s) after trimming.')
        else:
            if all(counts_all.flatten() == n_software_frames):
                print(f'All counts have the expected {n_software_frames} software frame(s) after trimming.')
            else:
                print(f'WARNING! Not all counts have the expected {n_software_frames} software frame(s) after trimming.')

    if apply_counts:
        if counts_format_new:
            total_frames = n_software_frames*n_firmware_frames
        else:
            total_frames = counts_all*n_firmware_frames   
        for n in range(N_PRODS):
            vis_all[..., n] /= total_frames

    # Load gains
    print(f'Loading gain file {gains_path}/gains.hdf5')
    gf = h5py.File(f'{gains_path}/gains.hdf5', 'r')

    if firmware_type == 'shifted_fft_common_log_gain':
        # For backwards compatibility
        glin = gf['lin'][:].astype(np.float64) # cast to float64 so operations with these arrays are correct
        glog = gf['log'][:].astype(np.float64)

    elif firmware_type == 'bitgrowth_fft_floating_point_gain':
        # For backwards compatibility
        glin = gf['gain_lin'][:].astype(np.float64) # cast to float64 so operations with these arrays are correct
        glog = gf['gain_log'][:].astype(np.float64)

    gf.close()

    if trim_data:
        # Remove first 4 bins from linear gain
        glin = glin[:, 4:]

        # If using floating point gains, we also need to trim the first four frequency bins from glog
        if firmware_type == 'bitgrowth_fft_floating_point_gain':
            glog = glog[:, 4:]
    
    if apply_gains:    

        vis_all *= float((2**44)**2) # Shift from "scaler" units to "post-gain" units
        
        for n in range(N_PRODS):
            idx0 = prod[n][0]
            idx1 = prod[n][1]
            for m in range(vis_all.shape[0]):
                vis_all[m, :, n] /= ((glin[idx0] * 2**glog[idx0]) * (glin[idx1] * 2**glog[idx1]))

    if firmware_type == 'shifted_fft_common_log_gain':
        return (vis_all, 
                f, 
                prod, 
                t_all, 
                counts_all, 
                glin, 
                glog, 
                sat_all, 
                adc_overflows_all, 
                fft_overflows_all, 
                scaler_overflows_all, 
                scaler_stats_ready_all,
                corr_overrun_all, 
                frame_count_all
                )
        
    elif firmware_type == 'bitgrowth_fft_floating_point_gain':
        return (vis_all, 
                f, 
                prod, 
                t_all, 
                counts_all, 
                glin, 
                glog, 
                sat_all, 
                adc_overrange_all,
                adc_overvoltage_all, 
                fft_overflows_all, 
                scaler_overflows_all, 
                scaler_stats_ready_all,
                corr_overrun_all, 
                frame_count_all
                )

def load_raw_data(acq, 
                  data_path, 
                  load_adc = True,
                  load_fft = True
                  ):

    # Determine firmware architecture:
    if int(acq[0:4]) < 2025:
        firmware_type = 'shifted_fft_common_log_gain'
    elif int(acq[0:4]) == 2025 or int(acq[0:4]) > 2025:
        if int(acq[4:6]) <= 3:
            if int(acq[4:6]) == 3 and int(acq[6:8]) < 11:
                firmware_type = 'shifted_fft_common_log_gain'
            if int(acq[4:6]) == 3 and int(acq[6:8]) >= 11:
                firmware_type = 'bitgrowth_fft_floating_point_gain'
            else:
                firmware_type = 'shifted_fft_common_log_gain'
        else:
            firmware_type = 'bitgrowth_fft_floating_point_gain'
    
    # Load data arrays
    file_names = os.listdir(f'{data_path}/{acq}')
    N_FILES = len(file_names)

    np.sort([int(os.listdir(data_path + '/' + acq)[n].split('.hdf5')[0]) for n in range(len(os.listdir(data_path + '/' + acq)))])
    file_numbers = np.sort([int(file_name.split('.hdf5')[0]) for file_name in file_names])

    ctr = 0 # counter
    for n in file_numbers: # range(N_FILES):
        print(f'Loading data file {data_path}/{acq}/{n}.hdf5')
        df = h5py.File(f'{data_path}/{acq}/{n}.hdf5', 'r')
        
        # Gather the products and frequencies from the first data file only,
        # since we don't need to load this more than once. We use the counter 
        # just in case 
        if ctr == 0:
            prod = df['index_map']['prod'][:]
            f = df['index_map']['freq'][:]

            N_PRODS = len(prod)

            if load_adc:
                adc_all = df['adc'][:]
            if load_fft:
                fft_all = df['fft'][:]
                
            t_all = df['index_map']['time'][:]

            if firmware_type == 'shifted_fft_common_log_gain':
                adc_overflows_all = df['stats']['adc_overflows'][:]

            if firmware_type == 'bitgrowth_fft_floating_point_gain':
                adc_overrange_all = df['stats']['adc_overrange'][:]
                adc_overvoltage_all = df['stats']['adc_overvoltage'][:]

            corr_overrun_all = df['stats']['corr_overrun'][:]
            fft_overflows_all = df['stats']['fft_overflows'][:]
            frame_count_all = df['stats']['frame_count'][:]
            scaler_overflows_all = df['stats']['scaler_overflows'][:]

        else:
            if load_adc:
                adc_all = np.hstack((adc_all, df['adc'][:])) # Use hstack in this case, since axis 0 corresponds to ADC input axis
            if load_fft:
                fft_all = np.hstack((fft_all, df['fft'][:]))
                
            t_all = np.concatenate((t_all, df['index_map']['time'][:]))

            if firmware_type == 'shifted_fft_common_log_gain':
                adc_overflows_all = np.concatenate((adc_overflows_all, df['stats']['adc_overflows'][:]))

            if firmware_type == 'bitgrowth_fft_floating_point_gain':
                adc_overrange_all = np.concatenate((adc_overrange_all, df['stats']['adc_overrange'][:]))
                adc_overvoltage_all = np.concatenate((adc_overvoltage_all, df['stats']['adc_overrange'][:])) 

            corr_overrun_all = np.concatenate((corr_overrun_all, df['stats']['corr_overrun'][:]))
            fft_overflows_all = np.concatenate((fft_overflows_all, df['stats']['fft_overflows'][:]))
            frame_count_all = np.concatenate((frame_count_all, df['stats']['frame_count'][:]))
            scaler_overflows_all = np.concatenate((scaler_overflows_all, df['stats']['scaler_overflows'][:]))

        # Close file
        df.close()

        # Increment counter
        ctr += 1

    # Assign None as needed
    if load_adc and not load_fft:
        fft_all = None
    elif load_fft and not load_adc:
        adc_all = None

    if firmware_type == 'shifted_fft_common_log_gain':
        return adc_all, fft_all, f, prod, t_all, adc_overflows_all, fft_overflows_all, scaler_overflows_all, corr_overrun_all, frame_count_all

    if firmware_type == 'shifted_fft_common_log_gain':
        return adc_all, fft_all, f, prod, t_all, adc_overrange_all, adc_overvoltage_all, fft_overflows_all, scaler_overflows_all, corr_overrun_all, frame_count_all