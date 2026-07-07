import labpy
import time

ps_params = {'labpy_object': 'agilent_N5700', 'adapter': 'TCPIP::10.10.10.151'}

def open_instrument(params):
    class_name = params.pop('labpy_object')
    return labpy.open_instrument(class_name, **params)


_PS = open_instrument(ps_params)

def start_ps():
    _PS.set_output(state=True)

def close_ps():
    _PS.set_output(state=False)

def restart_ps():
    _PS.set_output(state=False)
    time.sleep(5)
    _PS.set_output(state=True)