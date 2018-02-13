#!/usr/bin/env python
#
# masterctl: control and query ch_master via the REST interface.
#

from __future__ import print_function

import json
import rest

class ChMasterClient(rest.RESTClient):

    def print_result(self, d):
        if d == {}:
            self.print('ok')
        elif 'error' in d:
            self.print_error(d['error'].rstrip())
        else:
            self.print(json.dumps(d, sort_keys=True, indent=2))

    def nop(self):
        self.print('Doing nothing')

    def get_methods(self):
        return self.get('methods')


    def ping(self):
        """
        Test connection to ch_master.
        """
        import random
        nonce = random.getrandbits(32)
        r = self.post('echo', nonce=nonce)
        if 'nonce' in r and nonce == int(r['nonce']):
            self.print("ok")
            return
        self.print("internal error!. Server reply was: \n%s" % '\n'.join('%s:%s' % (k,v) for (k,v) in r.items()))

    def set_state(self, state):
        r = self.post('set-state', state=state)
        self.print_result(r)

    def start(self, yaml):
        """
        Start ch_master with specified config file.
        """
        if not yaml:
            raise ValueError('A YAML configuration filename must be specified')
        from pychfpga.fpga_array import load_yaml_config
        config = load_yaml_config(yaml.encode('ascii'))
        r = self.post('start', **config)
        self.print_result(r)

    def status(self):
        """
        Print ch_master status.
        """
        r = self.get('status')
        self.print_result(r)

    def stop(self):
        """
        Stop ch_master.
        """
        r = self.get('stop')
        self.print_result(r)

    def switch_gains(self):
        """
        Change gains.
        """
        r = self.post('switchgains')
        self.print_result(r)

    def kotekan_start(self, yaml):
        """
        Start kotekan with specified config file.
        """
        if not yaml:
            raise ValueError('A YAML configuration filename must be specified')
        from pychfpga.fpga_array import load_yaml_config
        config = load_yaml_config(yaml.encode('ascii'))
        r = self.post('kotekan-start', **config)
        self.print_result(r)

    def get_frequency_map(self):
        """
        Print ch_master status.
        """
        m = self.get('get_frequency_map')
        self.print_result(m)


if __name__ == '__main__':
    m = ChMasterClient()  # create a CHMasterClient object instance for use in interctive python sessions
