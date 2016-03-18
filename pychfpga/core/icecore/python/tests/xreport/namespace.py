import sys
import collections
import yaml

class NameSpace(collections.OrderedDict):

    # def __init__(self, *args, **kwargs):

    def __getattr__(self, name):
        try:
            return self.__getitem__(name)
        except KeyError:  # Re-raise, but preserve traceback info
            raise AttributeError, sys.exc_info()[1], sys.exc_info()[2]

    def __setattr__(self, name, value):
        if name.startswith('_' + collections.OrderedDict.__name__ + '__'):  # don't put special names in the dict. Needed for Ordereddict to initialize properly.
            super(NameSpace, self).__setattr__(name, value)
        else:
            self.__setitem__(name, value)

    def __setitem__(self, key, value):
        _setitem = super(NameSpace, self).__setitem__
        if isinstance(value, collections.Mapping) and not isinstance(value, NameSpace):
            _setitem(key, NameSpace(value))
        else:
            _setitem(key, value)

    def __dir__(self):
        return list(
            set(self.__dict__.keys()) |
            set.union(*[set(dir(cls)) for cls in type(self).mro()]) |
            set(self.keys()))

    def as_dict(self):
            def todict(v):
                if isinstance(v, collections.Mapping):
                    return {k: todict(i) for k, i in v.items()}
                elif isinstance(v, list):
                    return [todict(i) for i in v]
                else:
                    return v
            return todict(self)

    def as_yaml(self):
        return yaml.safe_dump(self.as_dict(), default_flow_style=False)
