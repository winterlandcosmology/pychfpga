from functools import partial 
from typing import Callable


import labpy
import pytest 

from wtl.namespace import NameSpace
from yaml import safe_load
class Maybe:
    """
    General monad class for uncertain operation handling, (harware request, file opening, etc.). Maybe<T> objects can 
    contain a value of unknown type T and can be given through run() a function of type T -> K and will return a Maybe<K>. If
    the function encounters some problem and returns a None value, this None will propagate down the chain without crash, allowing
    the user to handle the failure.
    """


    def __init__(self, value=None, fn_memory=None):

        """
        Constructor method

        :params value: value wrapped by Maybe class 
        :params fn_memory: memory of the function which generated the maybe's value, None if it didn't come from another maybe object
        """
        self._value  = value
        self._fn_memory = fn_memory
        

    def run(self, func, errhandle=None, _errlog='default'):
        """
        Returns Maybe<K> when given a func: T -> K applied on Maybe<T> 

        :params func: Function T -> K to be applied to Maybe<T>
        :params errhandle: Function () -> K that allows the error handling to be specified by the user, default to unused
        :return: Output of the function wrapped in the Maybe class
        :rtype: Maybe<K>

        """

        if self._value is None:
            if _errlog == 'default':
                print(f"Maybe monad returned None when running {self._fn_memory}") #Fn memory keeps a log of the function which generated the Maybe class 
                #(improvement consider a complete log of functions?)
            else:
                print(_errlog)

            if errhandle is not None:
                return Maybe(errhandle())
                

            return Maybe(None)
        else:
            return Maybe(func(self._value), func)

    def run_no_input(self, func, check_none=True, errhandle=None, _errlog='default'):
        """
        Returns Maybe<K> when given an IO func with no inputs: () -> K applied on Maybe<T> 

        :params func: Function () -> K with no inputs, used mainly to interact with IO or console 
        :params check_none: Bool specifies if the function should check if the Maybe object has a value of None before proceding with the func, defaults to True
        :params errhandle: Function () -> K that allows the error handling to be specified by the user, default to unused, doens't take input, use partial evaluation to give it input
        
        :return: Output of the function wrapped in a Maybe class
        :rtype: Maybe<K>
        """

        if self._value is None and check_none:
            if _errlog == 'default':
                print(f"Maybe monad returned None when running {self._fn_memory}") #Fn memory keeps a log of the function which generated the Maybe class 
                #(improvement consider a complete log of fucntions?)
            else:
                print(_errlog)

            if errhandle is not None:
                return Maybe(errhandle())

            return Maybe(None)

        else:
            return Maybe(func(), func)


    def orElse(self, default):
        """
        Set a default value for Maybe<T> object when it is None

        :params default: Default value
        """

        if self._value is None:
            return Maybe(default)
        else:
            return self

    def unwrap(self):
        """
        Removes the Maybe wrapper from object and returns its reference

        """

        return self._value

    def __or__(self, other):
        return Maybe(self._value or other._value)

    def __str__(self):
        if self._value is None:
            return 'Nothing'
        else:
            return 'Maybe ({})'.format(self._value)

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        if isinstance(other, Maybe):
            return self._value == other._value
        else:
            return False

    def __ne__(self, other):
        return not (self == other)

    def __bool__(self):
        return self._value is not None




class TestUtils:
    """ Some utility methods common to all tests. 
    """

    class SharedInstruments:
        def __init__(self):
            self.instruments = {}
    
    @pytest.fixture(scope='class')
    def instruments(self):
        return self.SharedInstruments().instruments

    def sub_test(self,
                setup_fn: Callable[[], bool],
                get_fn:   Callable[[], dict], 
                parse_fn: Callable[[dict], list], 
                teardown_fn: Callable[[], bool]) -> bool:

        """
        Self contained sub test function, allows for new tests to be constructed from different functions, returns True if passed, False otherwise

        :params setup_fn: Function () -> bool, function intended to setup test systems, (ex: open the board, open power supply), expected to return a True if setup is successfull
        :params get_fn: Function () -> dict, generic getter function, should contain test process and return a dict with matching keys to the expected machine output, ingested by parse_fn
        :params parse_fn: Function dict -> list(str), parsing function applied to the output of get_fn, should return human readable error messages as a list of strings 
        :params teardown_fn: Function () -> bool, tear down tests and ready for the following test, expected to return True if teardown is successfull

        :return: Did the subtest pass 
        :rtype: Bool
        """

        
        test_object = Maybe(None)


        if setup_fn():

            get_maybe = test_object.run_no_input(get_fn, check_none=False)
            parse_maybe = get_maybe.run(parse_fn)

            def check_error(errors):
                is_passed = True
                print("\nEncoutered the following error's during parsing: ")

                for error in errors:
                    is_passed = False
                    print(error)

                return is_passed

            test_passed = parse_maybe.run(check_error).orElse(False).unwrap() #check if the parser returned any errors to assert
            return test_passed # uses this function to simplify the pytest error messaging

            
        else:
            assert False, 'Error: Error in test setup '



    def load_config(self, filename):
        print(f'Loading config file {filename}')
        with open(filename, 'rb') as yamlfile:
            self.cfg = NameSpace(safe_load(yamlfile))
        return self.cfg  #return reference to self.cfg


    def open_instrument(self, name):
        """ Looks up the instrument name in the instrument table in configuration file and open it with the parameters specified in the table. 
            Open instrument will return a specific instrument object, if the instrument is already in the dictionnary it'll get it from there 
            if it's not it'll try to open it then add it to the dict for 

            :param name: str, name of the instrument requested as specified in the config file (not the labpy driver name)
        """

        def start_instrument(cfg, name):
            specified_instruments = cfg.get('instruments', default=None)

            if specified_instruments:
                try:
                    instr_params = specified_instruments[name].copy()
                    class_name = instr_params.pop('labpy_object')
                    return labpy.open_instrument(class_name, **instr_params)
                
                except KeyError:
                    assert False, f'Instrument {name}, could not be resolve in config file. The following instruments were specified {specified_instruments}'
                    
            else:
                assert False, f'Config file does not specify instruments '


        def is_responding(instrument): #(not very stable)
            return instrument.status() is not None

        #load_cfg_partial = partial(self.load_config, self.cfg_path)    #partial evaluation of load_config 
        


        # if name in self.instruments: #if the instrument is already opened and available we just use it but we check if it still answers
        #     if is_responding(self.instruments[name]):
        #         return self.instruments[name]
        #     else:
        #         print(f"Lost connection with instrument: {name}. Retrying connection: ")


        #config_maybe = Maybe(self.cfg, errhandle=load_cfg_partial) #get the config object if it doens't exist try to load the file
        config_maybe = Maybe(self.cfg) #get the config object if it doens't exist try to load the file


        if config_maybe:
            try:
                instrument = start_instrument(config_maybe.unwrap(), name)
                # self.instruments.update({name: instrument})
                return instrument
            except Exception as e:
                  print("********************************************************************")
                  print(f"Failed to open or locate instrument {name} \nFailed with error: {e}")
                  print(f"Please try to power cycle instrument: {name} and restart test.")
                  print("*******************************************************************")
                  return None
        else:
            #print(f"Failed to load cfg or open config yaml file with path {self.cfg_path}")
            return None

            
        #generate a dictionary of instruments that persists across tests, maybe as a curried labpy function? 



    def open_ps(self, name='ps16v', voltage=None, current=None):
        """ Opens a power supply and configures it

        The `voltage` and `current` to be programmed can be specified. If `None`, the global values from
        the config file in ``motherboard_tests.global_settings`` will be
        used.
        """
        
        # open power supply instrument if it is in the list of instruments and if we don't force manual operation /self.cfg.get('manual_ps', False)/
        if True and name in self.cfg.instruments:
            print("Tried to connect")
            self.ps = self.open_instrument(name)
        else:
            self.ps = None


        # initialize power supply if we have one
        if name == 'ps18v' and self.ps:
            voltage = voltage if voltage is not None else self.cfg.motherboard_tests.global_settings.ps_voltage
            current = current if current is not None else self.cfg.motherboard_tests.global_settings.ps_current
            self.ps.set_output(state=False) #Ensuring power on N5764A is off
            self.ps.clear() #Clearing any previous protection
            self.ps.set_voltage(voltage=voltage) #Setting voltage to 18V, power still off
            self.ps.set_current_limit(current=current, ocp=True) #Setting current limit and turning on ocp feature


        if name == 'ps16v' and self.ps:
            voltage = voltage if voltage is not None else self.cfg.motherboard_tests.global_settings.ps_voltage
            current = current if current is not None else self.cfg.motherboard_tests.global_settings.ps_current
            self.ps.set_output(state=False)
            #self.ps.set_current_limit(current=current)


        return self.ps
