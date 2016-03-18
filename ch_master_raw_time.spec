# This file is for specifying the entries in the configuration file.
# This is NOT the configuration file. That file should end in .conf.

log_target = string
log_level = string

[fpga]
  yamlfile = string
  subarrays = integer(min=0)
  prog = integer(min=0)
  bitfile = string
  no_mezz = integer(min=0)
  open = integer(min=0)
  sampling_frequency  = float(min = 1)
  data_width = integer(min = 4)
  group_frames = integer(min = 1 )
  delay_file = string
  sync_method = string
  sync_source = string
  adc_channels = int_list