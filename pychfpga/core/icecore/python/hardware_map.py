"""Hardware mapper.

This HardwareMapper actually returns a SQLAlchemy session object, which we've
extended slightly. Here is a sample session that creates a couple of new
hardware map entries:

    >>> sqc = SQUIDController(serial=123, type="MGNGSQC01")
    >>> mezz = DfmuxMezzanine(serial=234, type="MGMEZZ04", sqc=sqc)
    >>> d = Dfmux(serial=345, revision=1, mezz1=mezz)

We'll create an empty hardware map (instead of loading one, for now) and add
our elements to it:

    >>> hwm = HardwareMap()
    >>> hwm.add(d)

Since the iceboard object references the other two, we don't need to add them
explicitly.

You can now query a particular element in the hardware map:

    >>> ds = hwm.query(Dfmux)

You can ask them questions in bulk:

    >>> ds.get_frequency(...)
    (1.23e6, 2.34e6, 3.45e6)

Note that these calls were dispatched in parallel (multi-threaded), so this is
much faster than the equivalent Python code:

    >>> print [ d.get_frequency(...) for d in ds ]
    [1.23e6, 2.34e6, 3.45e6]

You can retrieve a single IceBoard, and interact with its properties
(including the structures it links with):

    >>> d = hwm.query(IceBoard).filter_by(serial=345).first()
    >>> print ib.get_frequency(...)
    1.23e6
    >>> m = ib.mezz1
    >>> print m.type
    U'MGMEZZ04'

...et cetera. Beneath the hood, the query language is provided by SQLAlchemy,
which allows rich queries and handles object mapping for us. It's slick, and
it's code we don't have to write or maintain.

TODO: Turn "echo" on when instantiating the HardwareMapper and see how many
SQL queries are generated -- lots. It is probably worth playing with
SQLAlchemy's "eager" options and ensuring there are indices on the right
columns.
"""

__all__ = [
    "Base", "HWMQueryException",
    "HWMQuery", "HWMResource",
    "macro", "algorithm", "HardwareMap",
    "Boolean",
    "Session",
    "set_session_class",
]

import tornado.gen
import tornado.ioloop

import inspect
import collections
import logging

import sqlalchemy
import sqlalchemy.orm
import sqlalchemy.ext.declarative
import sqlalchemy.types

import async
import ccoll

Base = sqlalchemy.ext.declarative.declarative_base()


class HWMQueryException(Exception):
    pass


class HWMQuery(sqlalchemy.orm.Query):
    '''HWMQuery object: A parallel-call extension to Query objects.

    This is also pretty well internal; you shouldn't have to use it directly.
    Please look at the DocStrings for HardwareMap instead.

    This is an extension to SQLAlchemy's Query object. A HWMQuery can be
    used to dispatch method calls on every class it contains.
    '''

    _algorithm_registry = {}

    def __getattr__(self, name):
        '''Teach a collection of query results how to parallelize.

        You would ordinarily retrieve a bunch of query results as
        follows (assuming "serial" is some property of the underlying
        object):

            >>> objects = hwm.query(ObjectClass)
            >>> for o in objects:
            ...     o.do_something()

        Since objects like "o" tend to be network resources, and calls
        like "o.do_something()" tend to be I/O-limited, we are
        interested in running this kind of function call in a
        multi-threaded context. We use green threads to avoid the
        overhead from threading -- which seems minimal, until we pile
        on the SQLAlchemy housekeeping that's required.

        This call allows you to do exactly that, as follows:

            >>> objects.do_something()

        It does so by trapping calls it doesn't recognize itself, and
        delegating them to each object in the Query results. It returns
        an array of results corresponding to each underlying object.
        '''

        # Since this is a SQLAlchemy "Query" subclass, we can use it
        # as a collection and call things like "count()" on it.

        # Refuse to parallellize access to special names
        if name in ('_orm_only_adapt', '_calls'):
            raise AttributeError()

        # First, try algorithms from the registry.
        for (cls, algs) in self._algorithm_registry.iteritems():
            if not issubclass(self.column_descriptions[0]['type'], cls):
                continue
            if name in algs:
                return algs[name](self)  # bind with this HWMQuery

        # Next, try attributes/methods from contained objects.
        # Special case: if the query returned no results, we can't reliably
        # ask it for attributes. Treat this as an error (we can sometimes
        # do the right thing, but not always.)
        if self.count() == 0:
            raise AttributeError("Can't request attribute '%s' from a Query "
                                 "with no results!" % name)

        # Generate an exception of only some of the objects have the
        # desired attribute. (If none of the objects have the attribute,
        # we want to raise the ordinary Python AttributeError. This
        # happens below.)
        attr_present = [hasattr(obj, name) for obj in self]
        if not any(attr_present):
            raise AttributeError(
                "Attribute '%s' does not exist on any element "
                "of the query" % name)
        elif not all(attr_present):
            raise AttributeError(
                "Attribute '%s' does not exist on *ALL* elements "
                "of the query" % name)

        # Get the specified attribute from all objects.
        attrs = [getattr(x, name) for x in self]

        return ccoll.Ccoll(attrs)

    def __dir__(self):
        """Retrieve a list of interesting attributes."""

        # ***JFC: This original following line just returns the dir of a super
        # object, which is not the dir of a Query object.
        # s = set(dir(super(HWMQuery, self)))

        # Get instance attributes
        # Add class attributes from all classes in the MRO
        # Add methods/propertiescommon to all query objects
        s = set(self.__dict__.keys()) | \
            set.union(*[set(dir(cls)) for cls in type(self).mro()]) | \
            set.intersection(*[set(dir(obj)) for obj in self])

        # Add algorithms from the registry.
        for (cls, algs) in self._algorithm_registry.iteritems():
            if issubclass(self.column_descriptions[0]['type'], cls):
                s.update([a.__name__ for a in algs])

        return [str(item) for item in s]  # Remove unicode stings

    def __call__(self, **kwargs):
        """ Apply the filter specified by the keywoard and return the resulting single instance.
        Example:
            c = hwp.query(IceBoard)
            c_slot1 = c(slot=1)

        An error will be generated if more than one board is generated by the query.
        """
        return self.filter_by(**kwargs).one()

    def call_with(self, func, *args, **kwargs):
        """Call some function across a collection of Query results.

        if 'func' possess the variable '_hwm_call_with_outer', that function
        is called with the whole HWMQuery passed as its first argument
        followed by the *args and **kwargs (as if 'func' was an unbound method
        of the HWMQuery object)

        Otherwise, the function is treated as an unbound method and will be
        concurrently called for each instance of the Query results by passing
        it that instance as its first argument, followed by the common
        arguments *args and **kwargs (as if 'func' was an unbound method of
        the objects selected by the query).

        Let's say you want to do "something" with a set of Query results:

            >>> [do_something(r, foo, bar) for r in results]

        ...but you'd like to parallelize the call (i.e. run it in a
        multi-threaded context.) Using this function, you can do so as
        follows:

            >>> results.call_with(do_something, foo, bar)
        """

        # If 'func' is an algorithm, it doesn't want a parallelized call.
        if isinstance(func, AlgMarker):
            return func(self, *args, **kwargs)

        return async.async_call([func] * self.count(), self, *args, **kwargs)

    def as_dict(self, keys=None, convert_fn=None):
        """
        Returns the query results as a dictionary-like HMWQueryAttribute
        object which is indexed with the specified keys. The collection can be
        used the same way as a HWMQuery, except that the results are will be
        indexed by the specified keys.

        if 'keys' is an Instrumented Attribute or a string representine the
        name of such an attribute, the dictionary will be indexed by the value
        of this attribute. If convert_fn is specified, the attribute values
        will be converted using that function.


        If 'keys' is a iterable, the values of 'keys' are used directly as an
        index.

        If the keys parameter is omitted or evaluates as False, the objects are
        indexed from 0 to len(x)-1 and the returned collection will behave
        similarly to a list.

        Note: once the object is converted to a HWMQueryAttribute,
        query operations can no longer be performed, and the collection will
        no longer track database changes.

        Example:
            >>> d = hwm.query(IceBoard).as_dict(IceBoard.serial, int)  # or .as_dict('serial', int)
            >>> d[7] # returns the iceboard with serial number 7
        """
        # If keys is a string, find the corresponding column object
        if isinstance(keys, str):
            keys = getattr(type(self[0]), keys, None)
            if not isinstance(keys, sqlalchemy.orm.attributes.InstrumentedAttribute):
                raise ValueError("Invalid attribute name")

        if isinstance(keys, sqlalchemy.orm.attributes.InstrumentedAttribute):
            keys = [key[0] for key in self.values(keys)]
            if convert_fn:
                keys = [convert_fn(key) for key in keys]
        return ccoll.Ccoll(self, keys)


class HWMResource(Base):
    '''Base class for Hardware Mapper resources to share.

    You should inherit from this class in order to create a Hardware-Mapped
    resource. It's declared for convenience, since "Base" is an instantiated
    class (see the top of this file.)
    '''
    __abstract__ = True

    @property
    def hwm(self):
        '''Retrieve the :class:`HardwareMap` that stores this object.'''
        return sqlalchemy.orm.object_session(self)

    def self_getter(self):
        """ Returns a getter function that returns a reference to the current ORM
        object. This function can safely be called anytime from anywhere to
        access this ORM object. The object will be loaded from the database if
        it is not already in memory.
        """
        keys = sqlalchemy.orm.object_mapper(self).primary_key_from_instance(self)
        session = sqlalchemy.orm.object_session(self)
        if all(keys) and session is not None:
            query = session.query(type(self))
            return lambda: query.get(keys)  # Closure: closes on query and keys
        raise RuntimeError('Cannot get a dynamic reference to an object '
                           ' that is not yet added to the database')

    def __init__(self, *args, **kwargs):

        # SQLAlchemy 's Base is not collaborative: it will break the MRO
        # access chain, so some subclass objects might never be initialized.
        # We explicitely call Base's __init__() and the next item in the MRO
        # chain to solve this problem.

        Base.__init__(self, *args, **kwargs)
        # Go to the next MRO object *after* Base. The Base Init will have
        # consumed all the parameters, so we pass none to the next level.
        super(Base, self).__init__()


class MacroMarker(object):
    '''Marker class so we can test macros using isinstance(..., MacroMarker)'''
    pass


class macro(object):
    '''Decorator for "macros" that performs some rudimentary typechecking.

    Macros are functions used with query objects that are parallelized
    directly, i.e.

    >>> @macro(ReadoutChannel)
    ... def print_channel(c):
    ...     print c.channel

    >>> hwm.query(ReadoutChannel).call_with(print_channel)

    The "print_channel" function ends up executing once for each
    ReadoutChannel in the query. See the "algorithm" decorator for an
    alternative.

    You do not need to use this macro to get this behaviour; it's the
    default case. We encourage use of the decorator anyway, for
    typechecking and to give context for the function being called.
    '''
    def __init__(dec, cls, register=False):
        if isinstance(cls, collections.Iterable):
            dec.__valid_classes = cls
        else:
            dec.__valid_classes = (cls,)
        dec.__register = register

    def __call__(dec, func):

        vcs = dec.__valid_classes

        class MacroProto(async.Parallelizable, MacroMarker):
            @tornado.gen.coroutine
            def __call_async__(self, obj, *args, **kwargs):

                # Check that the macro was called with an allowed class
                if vcs and not issubclass(obj.__class__, vcs):
                    raise TypeError(
                        "Macro called with wrong types! Expected %s, got %s" % (
                            ', '.join([cls.__name__ for cls in vcs]),
                            obj.__class__))

                # Say something about the call
                l = logging.getLogger(__name__)
                l.debug('%r: Invoking %s(...)' % (obj, func.__name__))

                # Invoke the macro.
                return func(obj, *args, **kwargs)

        p = MacroProto()
        p.__doc__ = inspect.getdoc(func)

        # Register this algorithm with the class it's used on.
        if dec.__register:
            for vc in vcs:
                setattr(vc, func.__name__, p)

        return p


class AlgMarker(object):
    '''Marker class so we can test algs using isinstance(..., AlgMarker)'''
    pass


class algorithm(object):
    '''Decorator for "algorithms".

    This decorator is used as follows:

    >>> @algorithm(ReadoutChannel)
    ... def print_channel(cs):
    ...     print cs.channel

    >>> hwm.query(ReadoutChannel).call_with(print_channel)

    Note that the "print_channel" function is called *once*, and is passed
    in a HWMQuery of ReadoutChannels that can be iterated over. When we
    bump into the "print" statement, an array of integers is assembled.

    If you do not use the @algorithm decorator, you get @macro behaviour
    (the print_channel function will be called once for each ReadoutChannel
    object in the query results.)
    '''
    def __init__(dec, cls, register=True):
        if isinstance(cls, collections.Iterable):
            dec.__valid_classes = cls
        else:
            dec.__valid_classes = (cls,)
        dec.__register = register

    def __call__(dec, func):

        name = func.__name__

        def wrapper(self, *args, **kwargs):
            if not issubclass(self.__class__, HWMQuery):
                raise TypeError("Algorithm called with non-HWMQuery!")
            vcs = dec.__valid_classes
            if vcs and not all([issubclass(x.__class__, vcs) for x in self]):
                raise TypeError(
                    "Algorithm called with wrong types! "
                    "Expected %s, got %s" % (
                        ', '.join([cls.__name__ for cls in vcs]),
                        ', '.join(set([x.__class__.__name__ for x in self]))
                    ))

            r = func(self, *args, **kwargs)

            # Say something about the call
            l = logging.getLogger(__name__)
            l.debug('%r: Invoked %s(...)' % (self, name))

            return r

        class AlgProto(async.Parallelizable, AlgMarker):
            __name__ = name

            @tornado.gen.coroutine
            def __call_async__(self, *args, **kwargs):
                return wrapper(*args, **kwargs)

        p = AlgProto()
        p.__doc__ = inspect.getdoc(func)

        # Register this algorithm with the HWMQuery.
        if dec.__register:
            for vc in dec.__valid_classes:
                try:
                    HWMQuery._algorithm_registry[vc][name] = AlgProto
                except KeyError:
                    HWMQuery._algorithm_registry[vc] = {name: AlgProto}

        return p


class Boolean(sqlalchemy.types.TypeDecorator):
    '''A Boolean lookalike for column definitions, accepting "true"/"false".

    Use this instead of sqlalchemy.Boolean when the column might be initialized
    with string values, e.g. from CSV entries.
    '''

    impl = sqlalchemy.types.Boolean

    def process_bind_param(self, value, dialect):
        if isinstance(value, str):
            if value.lower() == 'true':
                return True
            if value.lower() == 'false':
                return False
        return value


class Session(sqlalchemy.orm.Session):
    '''Subclass SQLAlchemy's 'Session' object.

    This subclass is not used here, but it provides a way for experiment code
    (dfmux.py) to extend it.'''

    pass


def set_session_class(cls_):
    '''Override the sqlalchemy.orm.Session class.'''
    global __session_class
    __session_class = cls_

__session_class = Session


def HardwareMap(uri='sqlite:///:memory:', echo=False, *args, **kwargs):
    '''Create a HardwareMap object (which is really a SQLAlchemy Session).'''

    # Connect to the database
    e = sqlalchemy.create_engine(uri, echo=echo)

    # It's possible we're operating on an empty, in-memory database
    # (that's one of the use cases we anticipate) -- so ensure all
    # of the relevant tables have been created.
    Base.metadata.create_all(e)

    return sqlalchemy.orm.sessionmaker(
        bind=e,
        query_cls=HWMQuery,
        class_=__session_class,
        *args, **kwargs)()

# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
