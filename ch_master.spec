# This file is for specifying the entries in the configuration file.
# This is NOT the configuration file. That file should end in .conf.

n_input = integer(min = 1)
n_freq    = integer(min = 1)

[fpga]
  ip_address = ip_addr
  port = integer(min = 1)
  rec_port = integer(min = 1)
  samp_freq  = float(min = 1)
  ref_freq = float(min = 1)
  fft_shift = integer(min = 1)
  gain = integer(min = 0)
  data_width = integer(min = 4)
  group_frames = integer(min = 1 )
  enable_gpu_link = integer(min = 0 )
  host_ip = ip_addr
  gain_table_pkl = string
  subarray = integer(min=0)
  bitfile_name = string
  force = integer(min=0)
  [[adc_delay]]

[gpu]
  n_gpus = integer(min = 1)
  n_expected_gpus = integer(min = 1, default = 16)
  base_listen_port = integer(min = 10000, default = 41000)
  gpu_intergration_period = integer(min=32768)

[acq]
  base_path = string
  livefile = string
  frames_per_file = integer(min = 1)
  frames_per_livefile = integer(min = 1)
  n_frame_buf = integer(min = 10, default = 12)
  producer_max_range = integer(min = 2, default = 3)
  fpga_count_max = integer(default = 4294967295)

  [[fpga_hk]]
    rate = integer(min = 1, default = 10)
