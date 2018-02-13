""" Logging support functions
"""
import logging
from pychfpga import NameSpace


def get_logger(*names):
    """ get a logger whose hierarchical name elements are provided in `names`.

    If no names are provided, the root logger is returned.

    Examples:

        get_logger(): returns root logger
        get_logger(__package__): return the logger corresponding to the package in which the current module is located
        get_logger(__name__): return the logger for the current package.module
        get_logger(__name__, self): return the logger for the current package.module.class_name
        get_logger(__name__, self, 'my_method'): return the logger for the current package.module.class_name

    Notes:
        - Anywhere in a module, the global variable ``__package__`` represents the full hierarchical name of the package in which the module is located.
        - Anywhere in a module, the global variable ``__name__`` represents the full hierarchical name of the packageof the module followrd by the module name.

    """
    def getname(x):
        if isinstance(x, str):
            return x
        else:
            cls = x.__class__
            return cls.__module__ + '.' + cls.__name__

    if not names:
        return logging.getLogger()
    else:
        logger = logging.getLogger(getname(names[0]))
        for name in names[1:]:
            logger = logger.getChild(getname(name))
        #print('created logger %s' % logger.name)
        logger.disabled = False
        return logger


def get_parent_logger(module_name):
    """
    Return the parent logger of the specified module.

    If the module is not imported as part of a package (i.e. this module is named 'ch_master'
    instead if 'ch_acq.ch_master'), then return the root logger.

    Usage::

        log.get_parent_logger(__name__)
    """

    return logging.getLogger(module_name.rsplit('.', 1)[0] if '.' in module_name else '')



def setup_logging(dict_config={}, log_levels={}, base_package_name=None, script_name=None, actual_package_name=None, **kwargs):
    """

    Parameters:

        base_package_name (str): name of the package that contains all the loggers in the config file.

        actual_package_name (str): Actual name of the packages that correspond to the base package name, as
            returned by __package__. This will be used to adjust the full logger names to account for the
            package name. This is typically __name__.rpartition('.')[0], as __package__ is not set consistently.

        script_name (str): names of the module that is executed as a script, if any. If a logger with that name exists, a
            copy of that logger will be added under the name "__main__" (because when modules are
            executed as scripts, ``__name___ = "__main__"``).



    Logger names:
        ch_acq
        ch_acq.ch_master
        ch_acq.pychfpga.fpga_array

    We want to logging to be set-up properly for this package whether the module is used as a script
    or is imported as part of another parent package. It is assumed that loggers are created with
    the name  provided by ``__name__``. However, ``__name__`` changes depending on how it is loaded.
    Taking ch_master module as an example, ``__name__`` take the following values:

       __main__ if ch_master is launched as a script
       ch_master if loaded from ch_acq
       ch_acq.ch_master if loaded with from ch_acq import ch_master
       app_pkg.ch_acq.ch_master if nested more deeply

    For this reason, logger names need to be modufied by doing the following modifications:
    logger names modifications:
        - strip the package prefix used in the config
        - create __main__ logger as a copy of the base module, in case the base module is executed as a script
        - prepend all the logger names with the actual package path

    To do so, we need the following information:
        - base package used in the config ('ch_acq' or extracted from __file__)
        - name of module called as script
        - actual package prefix (__package__= 'app_pkg.ch_acq', __name__ = 'app_pkg.ch_acq.ch_master')

    This can be provided by two information:
        - base_package_name = 'ch_acq' (constant). Base package. Could be taken from the config file.
        - module_name = __package__ (__main__ if run as script, ch_master if from the package, ch_acq or my_pkg.ch_acq if imported from a parent
        - script_name = 'ch_master' (constant). Could be taken from the config file.



    """

    # print 'setting up logger with', base_package_name, actual_package_name, script_name
    dict_config = NameSpace(dict_config).deepcopy()
    log_levels = NameSpace(log_levels or {})

    # Add the version number if non-existent
    if 'version' not in dict_config:
        dict_config.version = 1

    # If there are log_level shortcuts, apply those log levels to the corresponding handler
    for handler_name, log_level in log_levels.items():
            if handler_name in dict_config.handlers:
                dict_config.handlers[handler_name]['level'] = log_level

    # if there are filenames arguments in handlers, process the name with format to add pathname
    for handler_name, handler_config in dict_config.handlers.items():
        if 'level' in handler_config and isinstance(handler_config.level, basestring):
            handler_config.level = handler_config.level.upper()

        filename = handler_config.get('filename', None)
        if isinstance(filename, str) and '%(' in filename:
            handler_config.filename = filename % kwargs

    # fix the case of the logging levels to uppercase
    for logger_name, logger_config in dict_config.loggers.items():
        if 'level' in logger_config and isinstance(logger_config.level, basestring):
            logger_config.level = logger_config.level.upper()


    # Add a logger named __main__ in case the module is run as as script
    if script_name in dict_config.loggers:
        prefix, sep, name = script_name.rpartition('.')
        new_logger_name = prefix + sep + '__main__'
        dict_config.loggers[new_logger_name] = dict_config.loggers[script_name]
        #print('Added logger %s to process logs from %s' % (new_logger_name, script_name))
    # fix the logger names for the current package
    new_loggers = {}
    for logger_name, logger_config in dict_config.loggers.items():

        new_logger_name = logger_name

        # Remove the base package prefix
        if base_package_name:
            if logger_name == base_package_name:
                new_logger_name = ''
            elif logger_name.startswith(base_package_name + '.'):
                new_logger_name = logger_name[(len(base_package_name) + 1): ]

        # prepend actual package path
        if actual_package_name:
            new_logger_name = actual_package_name + (('.' + new_logger_name) if new_logger_name else '')
        #print('Converted logger name from %s to %s' % (logger_name, new_logger_name))
        new_loggers[new_logger_name] = logger_config

    dict_config.loggers = new_loggers

    #print 'new loggers=', new_loggers

    # register the existing handlers for each logger
    old_handlers = {}
    for logger_name in dict_config.loggers:
        old_handlers[logger_name] = logging.getLogger(logger_name).handlers


    #print('new logging config is: %s' % dict_config.as_dict())

    logging.config.dictConfig(dict_config)
    #print('new logging after dictCconfig is: %s' % dict_config.as_dict())

    new_handlers = {}
    for logger_name in dict_config.loggers:
        logger = logging.getLogger(logger_name)
        logger.disabled = False # re-enable the logger if is was disabled by a previous config. dictConfig() doen not do that
        new_handlers[logger_name] = [handler for handler in logger.handlers if handler not in old_handlers[logger_name]]

    return new_handlers

def stop_logging(new_handlers):
    """
    Delete the specified handlers for the loggers defined in new_handlers.


    """

    for logger_name, handlers in new_handlers.items():
        logger = logging.getLogger(logger_name)
        for handler in handlers:
            if handler in logger.handlers:
                logger.handlers.delete(handler)  # remove the handlers that were added for that logger

def setup_basic_logging(level='INFO'):

    DEFAULT_LOGGING = {
        'formatters': {
             'std': {
                 'format': "%(asctime)s %(levelname)s %(name)s: %(message)s",
                 'datefmt': "%H:%M:%S"},
              },
        'handlers': {
            'stderr': {'class': 'logging.StreamHandler', 'formatter': 'std', 'level': level}
            },
        'loggers': {
            '': {'handlers': ['stderr'], 'level': level}  # root logger

            }
        }
    setup_logging(DEFAULT_LOGGING)


if __name__ == '__main__':
    pass
