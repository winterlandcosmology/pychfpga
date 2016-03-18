"""Top-level packaging for 'icecore'.

Code using icecore should use top-level imports. For example:

    >>> from icecore import HardwareMap  # good!

...rather than relative imports:

    >>> from pydfmux.hardware_map import HardwareMap  # bad!

This allows __init__ to structure the package's API separately from Python
code, which means we can evolve it more freely.
"""
__path__= [__path__[0] + '/python']

from hardware_map import (
    Base,
    HWMQueryException,
    HWMQuery,
    HWMResource,
    macro, algorithm,
    HardwareMap,
    Boolean,
    Session,
    set_session_class
)

from async import (
    async,
    async_return,
    asynchronously
    )

from ccoll import (
    Ccoll
    )

from tuber import (
    TuberError,
    TuberRemoteError,
    TuberCategory,
    TuberObject,
)

from handler import (
    HandlerObject,
    Handler
)

from hwm_assets import (
    IceBoard,
    IceBoardHandler,
    IceCrate,
    IceCrateHandler,
    FMCMezzanine,
    FMCMezzanineHandler
)

from hwm_extra_assets import (
    IceBoardPlus,
    IceBoardPlusHandler,
    mdns_discover
)

from session import (
    YAMLLoader,
    HWMConstructor,
    HWMCSVConstructor,
    IncludedYAMLValue,
    load_session,
    set_yaml_loader_class,
    register_yaml_object
)

from session import load_session as load_yaml

from tests.xreport import (XReport, TestReport, NameSpace)

# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
