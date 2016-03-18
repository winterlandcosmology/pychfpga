"""Hardware map object for the Agilent N5764A power supply.
"""

# import tornado.gen
# import select
# import socket
# import contextlib
# import logging
# import functools
import time

from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy import UniqueConstraint, CheckConstraint
from sqlalchemy.orm import relationship, backref
from sqlalchemy.orm.collections import attribute_mapped_collection

from pychfpga.core.icecore import hardware_map

from pychfpga.core.icecore import handler
from pychfpga.core.icecore import session
from agilent_N5700 import agilent_N5700
# from hw import ipmi_fru
# import datetime
# import base64

@session.register_yaml_object()
class AgilentN5764A(hardware_map.HWMResource, handler.HandlerObject):
    handler_name = 'AgilentN5764AHandler'
    __tablename__ = 'AgilentN5764A'
    # __table_args__ = (
    #     UniqueConstraint('serial'),
    # )
    __mapper_args__ = {'polymorphic_identity': 'AgilentN5764A',
                       'polymorphic_on':'_polymorphic_key'}
    __ipmi_part_number__ = None  # Must match part number in IPMI data

    _pk = Column(Integer, primary_key=True)
    _polymorphic_key = Column(String)  # Needed to allow multiple types of power supplies

    hostname = Column(String,
                    doc="The hostname (ip address or name of the power supply")

    # serial = Column(String,
    #                 doc="The serial number written on the board (e.g. '001')")

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, self.hostname)


class AgilentN5764AHandler(handler.Handler):  #, agilent_N5700
    """
    Provide the basic methods to operate an Agilent N5700-series power supply.
    """
    __handler_for__ = AgilentN5764A

    hostname = handler.HandlerParentAttribute(lambda ib: ib.hostname)

    def __init__(self, **kwargs):
        super(AgilentN5764AHandler, self).__init__(**kwargs)
        self.locked = True
        self.ps = None

    def open(self):
        if self.ps:
            raise RuntimeError('Power supply is already opened()')
        self.ps = agilent_N5700(interface='lan', ip_addr=self.hostname, ip_port=5025, timeout=0.5, verbose=0)

    def __repr__(self):
        if self.ps:
            return '%s %s @%s' % (self.ps.instrument_name, self.ps.instrument_model, self.hostname)
        else:
            return 'Unknown power supply'

    def lock(self):
        self.locked = True

    def unlock(self):
        self.locked = False

    def _check_lock(self):
        if self.locked:
            raise RuntimeError('Instrument is locked: cannot change its state. Call unlock() to allow changes to the instrument state')

    def power_on(self):
        self._check_lock()
        self.ps.output(state=True, readonly=False)

    def power_off(self):
        self._check_lock()
        self.ps.output(state=False, readonly=False)

    def power_enable(self, state):
        self._check_lock()
        self.ps.output(state=state, readonly=False)

    def power_cycle(self, delay=4):
        self._check_lock()
        self.ps.output(state=False, readonly=False)
        time.sleep(delay)
        self.ps.output(state=True, readonly=False)

    def status(self):
        return self.ps.status()

# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
