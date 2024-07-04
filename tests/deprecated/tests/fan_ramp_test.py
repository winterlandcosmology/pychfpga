import time
def test_temp(ib):
    for dcy in [100,80,60]:
        print 'Setting speed to %i' % dcy
        ib.set_crate_fan_speed(dcy)
        for x in range(15):
            time.sleep(1)
            temp = ib.get_motherboard_temperature(ib.TEMPERATURE_SENSOR.MB_FPGA_DIE)
            print '%3i%%: %0.2f' % (dcy, temp)
