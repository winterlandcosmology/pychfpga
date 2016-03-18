"""Ccoll: Concurrent (or Callable) collection: list-like or dict-like
container that allows attribute accesses and method calls to be performed on
concurrently on all its items.
"""

import collections
import logging
import itertools
import numpy as np

import async

__all__ = [
    "Ccoll"
]

class Ccoll(object):
    """
    Class representing a collection of objects that can be accessed and/or
    called concurrently at any level in a hierarchy of objects. This enable
    concurrent access in object-oriented programs.

    The collection is stored as a mapping, and is populated with the elements
    of the iterable 'objects' using the keys provided in 'keys'. If 'keys' is
    None, the keys are integers from 0 to len(objects)-1 to mimic a list.

    The mapping operates like an ordered dictionary and offers the same methods
    (.items(), .keys(), __len__() etc...) with the exception that the mapping
    itself returns an iterable to the objects, not their keys. This behavior is
    consistent with a HWMQuery object.

    Calling the mapping will concurrently call every object with the provided
    arguments and will return the result in another Ccoll with
    identical keys.

    Accessing an attribute of the mapping will return a new HWMQueryAttribute
    containing the that attribute for each of the element of the mapping.

    Indexing the mapping will return the object with the corresponding key.
    Slices are not supported.

    Calling .getitem(index) on the array will return another mapping where each
    element was indexed with index.

    As a convenience, the object masquerade as the first element of its
    collection if that object is callable, and therefore inherits its docstring
    and call signature, which allows ipython to provide useful hilts during
    interactive sessions.

    Examples:

    >>> class Obj(object):
    >>>     def __init__(self, x): self.x = x
    >>>     def fn(self, y): return (self.x,y)
    >>>     z=5
    >>>
    >>> coll = Ccoll([Obj(1), Obj(2), Obj(3), Obj(4)])
    >>> print coll[3]
    >>> <__main__.Obj object at 0x000000000BF68320>
    >>> print list(coll)
    [<__main__.Obj object at 0x000000000BF68278>, <__main__.Obj object at 0x000000000BF682B0>, <__main__.Obj object at 0x000000000BF682E8>, <__main__.Obj object at 0x000000000BF68320>]
    >>> print list(coll.z)
    [5, 5, 5, 5]
    >>> t1 = coll.fn(10)
    >>> print list(t1)
    [(1, 10), (2, 10), (3, 10), (4, 10)]
    >>> t1[3]
    (4, 10)
    >>> print list(t1.getitem(1))
    [10, 10, 10, 10]
    """

    # We define those so __setattr__ does not try to send them to objects
    # during __init__.
    _has_keys = None
    _proto = None
    _dict = None
    logger = None


    @classmethod
    def chain(cls, *iterables):
        """ Concatenates any number of iterables into a sincle Ccoll collection. """
        return cls(itertools.chain(*iterables))

    @classmethod
    def unique(cls, iterable):
        """ Create a Ccoll with only unique elements. """
        return cls(set(iterable))

    def __init__(self, objects, keys=None):
        # Do not define a docstring here: for some reason ipython will use it
        # instead of the dynamic __doc__ defined below.
        self.logger = logging.getLogger(__name__)
        if isinstance(objects, collections.Mapping):
            if keys is not None:
                raise TypeError('Cannot use the keys parameter when a mapping is provided')
            keys = objects.keys()
            objects = objects.values()
        self._has_keys = bool(keys)
        object_list = list(objects)  # in case object is a generator etc.
        # Get the object that this class will mimic if callble
        self._proto = object_list[0] if object_list else None
        keys = keys if keys is not None else range(len(object_list))
        if len(set(keys)) != len(object_list):
            raise ValueError('Keys are not unique')
        self._dict = collections.OrderedDict(sorted(zip(keys, object_list)))

    def __repr__(self):
        if self._has_keys:
            return '%s containing: {\n%s}' % (
                type(self).__name__,
                ',\n'.join('%s: %r' % (key, value) for
                           (key, value) in self.items())
                )
        else:
            return '%s containing: [\n%s]' % (
                type(self).__name__,
                ',\n'.join(['%r' % (value, ) for value in self])
                )

    def __dir__(self):
        """Retrieve a list of interesting attributes."""
        s = (set(self.__dict__.keys()) |
             set.union(*[set(dir(cls)) for cls in type(self).mro()]) |
             set.intersection(*[set(dir(obj)) for obj in self]))
        return [str(item) for item in s]

    # Mirror the main attributes of _proto if it is callable so we can mimic
    # its signature.
    __doc__ = property(lambda self: self._proto.__doc__ if callable(self._proto) else 'Collection of %s objects' % type(self._proto))
    __class__ = property(lambda self: self._proto.__class__ if callable(self._proto) else Ccoll)
    __name__ = property(lambda self: self._proto.__name__ if callable(self._proto) else Ccoll.__name__)
    im_func = property(lambda self: self._proto.im_func)
    func_code = property(lambda self: self._proto.func_code)
    func_defaults = property(lambda self: self._proto.func_defaults)

    # Offer a subset of OrderedDict methods. We could just have inherited dict,
    # but methods that change the dict would have been available, and it is
    # also tricky to redefine __iter__
    def __iter__(self): return self._dict.itervalues()

    def __len__(self): return self._dict.__len__()

    def __reversed__(self): return self._dict.__reversed__()

    def items(self): return self._dict.items()

    def iteritems(self): return self._dict.iteritems()

    def keys(self): return self._dict.keys()

    def iterkeys(self): return self._dict.iterkeys()

    def values(self): return self._dict.values()

    def itervalues(self): return self._dict.itervalues()

    def __getitem__(self, index): return self._dict.__getitem__(index)

    def __contains__(self, x):
        raise TypeError("Please explicitly specify the target: use 'x in ccoll.keys() or 'x in ccoll.values()' instead of 'x in ccoll'")

    def __call__(self, *args, **kwargs):
        # """ Concurrently calls every element of the collection with the
        # provided arguments, and return the results in a new Ccoll
        # object.

        # It is assumed that we have either functions or already bound
        # methods, so we don't have to pass those an object-specific
        # parameter.
        #
        # NOTE: Leave this docstring commented to allow ipython to
        # see the target object doc
        # """
        results = async.async_call(self.values(), None, *args, **kwargs)
        return Ccoll(results, self._dict.keys() if self._has_keys else None)

    def map(self, func, *args, **kwargs):
        return Ccoll((func(i, *args, **kwargs) for i in self.values()), self._dict.keys() if self._has_keys else None)

    def getitem(self, index):
        return self.__getattr__('__getitem__')(index)

    def get(self, *args, **kwargs):
        """
        ``get(key)`` returns the value with key and raises KeyError if not found.
        ``get(key, default) returns the value with the key and returns ``default`` if not found.
        ``get(attr1=value1, attr2=value2 ...)`` returns the first element where all the specified attributes match the specified values.
        """
        if (args and kwargs) or not (args or kwargs):
            raise AttributeError('Specify either a key or a key=value arguments')

        if len(args) == 0:
            value = (v for (k,v) in self._dict.items() if all(hasattr(v, kn) and getattr(v, kn) == kv for (kn, kv) in kwargs.items()))
            try:
                return value.next()
            except StopIteration:
                raise KeyError('No object match the specified key=value pair(s)')
        elif len(args) == 1:
            return self._dict[args[0]]
        elif len(args) == 2:
            return self._dict.get(*args)
        else:
            raise AttributeError

    def index_by(self, keys):
        """ Return a Ccoll object containing the same data but indexed with
        the specified key. Key can be the name of an attribute to be used as
        key, or a iterable to be used directly."""

        if isinstance(keys, str):
            self._check_collection_attributes(keys)
            return Ccoll(self._dict.values(), keys=[getattr(obj, keys) for obj in self])
        elif callable(keys):
            return Ccoll(self._dict.values(), keys=[keys(obj) for obj in self])
        else:
            return Ccoll(self._dict.values(), keys=keys)

    def __getattr__(self, name):
        """Return a collection of attribute 'name' from each of the current
        objects.
        """
        if not self:
            raise AttributeError("There are no objects in the list")
        self._check_collection_attributes(name)
        return Ccoll(
            [getattr(obj, name) for obj in self],
            self._dict.keys() if self._has_keys else None)

    def __setattr__(self, name, value):
        """ Sets a value on a collection of objects.
        """
        try:
            object.__getattribute__(self, name)  # check is attribute exists
            return object.__setattr__(self, name, value)
        except AttributeError:  # if attributes does not exist
            if self._dict:  # 'for obj in self' will call _dict.__len__()
                self._check_collection_attributes(name)
                for obj in self:
                    setattr(obj, name, value)

    def __array__(self, dtype=None):
        if dtype is None:
            dtype = type(self._dict.itervalues().next())
        return np.array(self._dict.values(), dtype=dtype)

    def _check_collection_attributes(self, name):
        """ Checks if all members of the collection has the specified
        attribute name, otherwise raise an exception"""
        attr_present = [hasattr(obj, name) for obj in self]
        if not any(attr_present):
            raise AttributeError("Attribute %s does not exist on any element "
                                 "of the current results" % name)
        elif not all(attr_present):
            raise AttributeError("Attribute %s must exist on all elements "
                                 "of the current results" % name)



# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
