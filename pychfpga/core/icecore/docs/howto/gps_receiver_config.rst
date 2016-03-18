GPS receiver configuration
==========================

This HowTo describes how to configure the Spectrum Instruments TM-4 Irig-B GPS receiver to use it as a source for the IceBoard.


Output Configuration
    + Out A: Mux1, IRIG-B
    + Out B: 10 MHz
    + Out C: 10 MHz

We set both Out A and Out B to the 10MHz output so we can drive two crates.

PPS Mode:
	Set PPS mode to FILPPS when Time Valid, and GPSPPS otherwise. This applies to the PPS signal on the Muxes. We don't use PPS, but Spectrum Instruments have changes their firmware for us so the IRIG_B signal would also switch to the filtered version when the PPS signal switches. This is important to have the IRIG-B timing be stable relative to the 10 MHz reference clock, and therefore allow us to specify triggers that safely occur between two 10 MHz edges and allow all boards to trig on the same 10 MHz edge.

Time format
	IRIGB207 is needed if we want the year.

Timing Mode:
	Static is the easiest to make work. It just measures time, assuming the location has been properly entered by the user. It works with only one visible satellite. It is easy to get a Tive Valid and reference Lock  in a short delay, even if the antenna is indoors next to a window in front of buildings that block most of the sky.

	On a good location, use Survey mode to figure out the position automatically.

Connection to the IceCrate
--------------------------

Use a 6dB attenuator before conecting to the IceCrate because the output level of the TM4 CMOS output exceeds the IceCrate input range.


.. vim: sts=3 ts=3 sw=3 tw=78 smarttab expandtab
