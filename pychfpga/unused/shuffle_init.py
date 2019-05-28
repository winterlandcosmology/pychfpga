
c.fpga.set_corr_reset(0)
c.fpga.set_data_source('funcgen')

 for i,bb in enumerate(c):
    bb.fpga.set_funcgen_function('a', a=0x0+(i<<4))
    bb.fpga.CROSSBAR[0].STREAM_ID = 0x0+(i<<4)
    bb.fpga.CROSSBAR[1].STREAM_ID = 0x1+(i<<4)
    bb.fpga.REFCLK.SLAVE=1

r=c27
r.fpga.CROSSBAR2.SOF_WINDOW_STOP = 100
r.fpga.BP_SHUFFLE.reset_rx_equalizers()
r.fpga.REFCLK.sync() # needed
r.fpga.CROSSBAR2[0].print_frame_info()

c8.fpga.REFCLK.sync()


crx=b[0]
cb1=crx.fpga.CROSSBAR
cb2=crx.fpga.CROSSBAR2
bp=crx.fpga.BP_SHUFFLE
rx1=bp.gtx[0]
rx2=bp.gtx[1]
rx3=bp.gtx[2]
gpu=crx.fpga.GPU
bs2=cb2[0]
