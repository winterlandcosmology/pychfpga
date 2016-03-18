#!/usr/bin/python

'''System monitor.

This script runs on-board (it's not intended to run on a PC), and provides
monitoring services including the following:

    - System health monitoring
    - "Panic" actions
'''

import tuber
import yaml
import smtplib
import email.mime.text

from tornado.ioloop import PeriodicCallback, IOLoop
from tornado.gen import coroutine

import logging
import logging.config

# Configure logging and instantiate a logger.
logging.config.dictConfig(yaml.load('''
    version: 1
    formatters:
        compact:
            format: '%(asctime)s | %(levelname)s | %(name)s | %(message)s'
        complete:
            format: >-
                %(asctime)s |
                %(levelname)s |
                %(filename)s:%(lineno)d |
                %(funcName)s |
                %(name)s |
                %(message)s
    handlers:
        syslog:
            class: logging.SysLogHandler
            level: WARNING
            formatter: compact
            facility: iceboard_monitor
        file:
            class : logging.handlers.RotatingFileHandler
            formatter: complete
            level: DEBUG
            maxBytes: 1048576
            backupCount: 8
            filename: /tmp/iceboard-monitor.log
    root:
        handlers: [syslog, file]
        level: NOTSET
'''))
logging.captureWarnings(True)

# The board must have access to the Internet for this to work correctly!
def sendmail(recipient, sender, subject, message,
             smtp_server, smtp_port,
             smtp_user, smtp_password):

    msg = email.mime.text.MIMEText(message)
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = recipient

    s = smtplib.SMTP(smtp_server, smtp_port)
    s.starttls()
    s.login(smtp_user, smtp_password)
    s.sendmail(sender, recipient.split(","), msg.as_string())
    s.quit()

# Define and instantiate an IceBoard.
class IceBoard(tuber.TuberObject):
    pass

ib = IceBoard(hostname='iceboard004.local')

# Panic actions

@coroutine
def fpga_panic():
    try:
        logger.critical("FPGA panic!")

        # Deprogram the FPGA. Also power off the mezzanines, since they may be a
        # culprit (and since they aren't useful without the FPGA anyway)
        with ib.tuber_context() as ctx:
            ctx.clear_fpga_bitstream()
            ctx.set_mezzanine_power(False, 1)
            ctx.set_mezzanine_power(False, 2)
            yield ctx._tuber_flush_async()

        # Now try to send an e-mail if we can.
        panic_configuration = ib.get_panic_configuration()
        if set(panic_configuration.keys()) >= {
            'sendemail.smtppass',
            'sendemail.from',
            'sendemail.to',
            'sendemail.smtpuser',
            'sendemail.smtpserverport',
        }:
        if all(map(lambda x: x in panic_configuration, ('sendemail.smtppass' in all(

        sendmail('gsmecher@gmail.com', 'chime.correlator@gmail.com',
                'subject', 'message', 'smtp.gmail.com', 587,
                'chime.correlator', 'penticton')
    except Exception as e:
        logger.critical("Eek! %r" % e)

@coroutine
def power_panic():
    logger.critical("Power supply panic!")

    # Make sure mezzanines are powered off.
    with ib.tuber_context() as ctx:
        ctx.set_mezzanine_power(False, 1)
        ctx.set_mezzanine_power(False, 2)
        yield ctx._tuber_flush_async()

# Monitoring functions, called periodically. These must include their own
# exception block, since otherwise exceptions go undetected.

@coroutine
def check_motherboard():
    try:
        # It might eventually be useful to have multiple set points per rail here.
        MB_TEMPERATURE_ACTIONS = {
            ib.TEMPERATURE_SENSOR.MB_PHY: (80, None),
            ib.TEMPERATURE_SENSOR.MB_ARM: (80, None),
            ib.TEMPERATURE_SENSOR.MB_FPGA: (80, fpga_panic),
            ib.TEMPERATURE_SENSOR.MB_FPGA_DIE: (80, fpga_panic),
            ib.TEMPERATURE_SENSOR.MB_POWER: (80, power_panic),
        }

        # Build 'results' a dictionary of temperature values.
        results = {}
        with ib.tuber_context() as ctx:
            for (sensor, (limit, action)) in MB_TEMPERATURE_ACTIONS.items():
                results[sensor] = {
                        "future": ctx.get_motherboard_temperature(sensor),
                        "sensor": sensor,
                        "limit": limit,
                        "action": action,
                }
            yield ctx._tuber_flush_async()

        # Check against permitted limits
        for (sensor, d) in results.items():
            temp = d['future'].result()
            limit = d['limit']
            action = d['action']

            if temp > limit:
                print "Eek! %f > %f" % (temp, limit)

                if action:
                    action()

    except Exception as e:
        logger.critical("Eek! %r" % e)

@coroutine
def check_backplane():

    try:
        pass

    except Exception as e:
        print "Eek! %r" % e


if __name__=='__main__':
    iol = IOLoop.instance()

    # Install a temperature checker
    temp_cb = PeriodicCallback(check_motherboard, 10000)
    temp_cb.start()

    # Install backplane checker
    if ib.is_backplane_present():
        bp_cb = PeriodicCallback(check_backplane, 10000)
        bp_cb.start()

    # Enter event loop (never returns)
    iol.start()

# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
