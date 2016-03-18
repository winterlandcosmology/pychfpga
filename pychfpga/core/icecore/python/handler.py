"""Handler management classes.

A Handler is a user-defined, persistent Python object that is dynamically
linked to a volatile object in order to into extend its attributes and
methods. They are typically used to provide stateful methods linked to
SQLAlchemy ORM objects

Handlers behave as normal Python objects.  All their instance attributes will
persist until the object is no longer used, as opposed to ORM objects that
might have their non-database attributes erased as part of
the background operation of the ORM engine. This, for instance, allows the
handler to store hardware state information, opened sockets or other runtime-
specific information that should not live in the database.

Handlers instances are only created once, and are reattached to the
parent object if the handler link has been erased by the parent.

Multiple Handler classes can be registered to an ORM class, allowing the ORM
object to dynamically switch functionality. For example, different handlers
can be activated depending on what firmware has been loaded on the hardware
represented by the parent object.

This module exposes the following classes:

    - HandlerObject: is inherited by the parent object to which we want to
      associate a handler. The class ensures the handler is reconnected to the
      object, and forwards attribute accesses to the handler.

    - Handler: is inherited by the application-specific handler. The class
      registers the handler to the parent object, and provides means for the
      handler to grab information from the parent.
"""

import logging
import sys


class HandlerObject(object):

    """Automatically create and persistently associate a python class (a
    handler) to a 'volatile' parent object and forwards attribute accesses to
    it.

    When a handler needs to be accessed on a parent object instance,
    HandlerObject looks for an already existing one in its handler instance
    registry using the unique parent instance key provided by the 'handler_id'
    property which is provided by the parent class.

    If no handler is found, HandlerObject looks into its handler class
    registry to see if there is a handler class that matches the handler name
    provided by the 'handler_name' attribute. If a matching handler class is
    found, a new handler instance is created and registered. Otherwise, an
    error is raised.

    The user defines a handler by subclassing the Handler class. The Handler
    class (or to be precise, its meta-class)  will automatically register the
    handler class to the specified parent object class.

    The 'handler' property always refers to the handler instance if it exist
    or can be created.

    HandlerObject defines __getattr__ and __dir__ to give the parent object
    access to all of the handler's attributes. Setting an attribute always
    sets it in the parent object, so use obj.handler.x=value instead of
    obj.x=value to set the attribute x in the handler.

    To have a handler, the parent object only need to inherit from this class
    and optionally define 'handler_id' and 'handler_name' to uniquely identify
    the parent instance and which of the registered handler is to be used with
    this parent instance.

    If not specified, the default 'handler_id' assumes the parent is a
    SQLAlchemy ORM object and will use its primary keys as a unique instance
    id (which exist only once the object has been added to the database).

    'handler_name' defaults to None, meaning that this object does not use a
    handler.
    """

    # Class attributes
    _handler_instance_registry = {}
    _handler_class_registry = {}
    # Default instance attribute values to prevents recursive __getattr__ calls
    _handler = None
    _handler_disable = False
    # Default non-data descriptors: can be overriden by instance attributes
    handler_name = None
    handler_id = property(lambda self: tuple(
        [self._sa_instance_state.session_id] +  # Make handler unique to each session
        self.__mapper__.primary_key_from_instance(self)))

    @property
    def handler(self):
        """ Return the handler object associated with this parent instance
        based on the value of handler_id and handler_name.

        The value of the handler is cached in the _handler attribute for fast
        access. Recursive calls are prevented.

        NOTE: If this property raises an AttributeError when accessed, Python
        will automatically (and annoyingly) call __getattr__('handler').
        """

        if self._handler_disable:
            return None
        if self._handler:  # Promptly return cached value if available
            return self._handler
        try:
            self._handler_disable += 1  # Prevent recursion

            handler_id = self.handler_id  # Note: Property may raise exception
            handler_name = self.handler_name  # idem
            if not handler_id or not handler_name:  # No unique id = no handler
                return None

            logger = logging.getLogger(__name__)
            handler_key = (self.handler_id, self.handler_name)

            # If the key exists in the handler registry, retreive it
            if handler_key in self._handler_instance_registry:
                logger.info("%r: Reusing handler for handler key (%s, %s)" %
                            (self, handler_id, handler_name))
                self._handler = self._handler_instance_registry[handler_key]
                return self._handler

            # If not, let's create and register a new handler
            if handler_name in self._handler_class_registry:
                handler_class = self._handler_class_registry[handler_name]
                logger.info("%r: Handler key %s does not exist. Creating %s" %
                            (self, handler_id, handler_name))
                self._handler = handler_class(parent_getter=self.self_getter())
                type(self)._handler_instance_registry[handler_key] = self._handler
                return self._handler

            raise NameError("No handler '%s' for %r" % (handler_name, self))

        finally:  # make sure we re-enable handler upon exit
            self._handler_disable -= 1

    def __getattr__(self, name):
        """ Return the value of an attribute if it exists in the handler """
        # If *properties* fail with AttributeError, they end up here. Catch.
        if name in ['_sa_instance_state', 'handler', 'handler_id',
                    'handler_name']:
            raise AttributeError
        try:  # AttributeError in handler are bad. Promote to RuntimeError
            h = self.handler
        except AttributeError:  # Re-raise, but preserve traceback info
            raise RuntimeError, sys.exc_info()[1], sys.exc_info()[2]
        if h:
            result = getattr(h, name)  # Will raise AttributeError if needed
            if callable(result):
                setattr(self, name, result)
            return result
        try:  # Not found, pass request to next subclass
            return super(HandlerObject, self).__getattr__(name)
        except AttributeError:  # if there is no _getattr_ or _getattr_ fails
            raise AttributeError("Unknown attribute '%s' in %s" %
                                 (name, object.__repr__(self)))

    def __dir__(self):
        """ Return the list of attributes of this class and of those of the
        handler.
        """
        h = self.handler
        return list(
            set(self.__dict__.keys()) |
            set.union(*[set(dir(cls)) for cls in type(self).mro()]) |
            set(dir(h) if h else []))

    def update_handler(self):
        """ Invalidates a cached handler. """
        self._handler = None


class HandlerMeta(type):
    """ Automatically register a Handler to its associated parent object when
    the Handler class is defined.

    Every Handler class must define the '__handler_for__' and assign to it the
    parent object to which this handler is associated with. The parent object
    must be a subclass of HandlerObject. If __handler_for__ is None, the
    handler is not registered.

    The Handler class can also define '__handler_name__' and assigning it a
    string defining the handler name. If not specified, the Handler class name
    is used.
    """
    def __init__(cls, classname, bases, dict_):
        logger = logging.getLogger(__name__)
        type.__init__(cls, classname, bases, dict_)

        # Check if __handler_for__ is defined and get its value.
        if not hasattr(cls, '__handler_for__'):
            raise AttributeError(
                "'__handler_for__' must be specified in %r" % classname)
        base = cls.__handler_for__
        if base is not None and not issubclass(base, HandlerObject):
            raise AttributeError(
                "%s defined '__handler_for__'=%r, but the target class "
                "is not inheriting from HandlerObject" %
                (classname, base))

        # Get the handler name. If not defined, use the class name
        if '__handler_name__' in dict_ and cls.__handler_name__:
            name = cls.__handler_name__
        else:
            cls.__handler_name__ = name = classname

        # Register the handler class to the HWM object
        if base and name:
            logger.info("%s: Registering to parent object '%s' as '%s'"
                        % (cls.__name__, base.__name__, name))
            if '_handler_class_registry' not in base.__dict__:
                base._handler_class_registry = {}  # Create a class-local class registry
            if '_handler_instance_registry' not in base.__dict__:
                base._handler_instance_registry = {}  # Create a class-local instance registry
            base._handler_class_registry[name] = cls  # Add the class
        else:
            logger.info("%s: Was not registered" % (cls.__name__))


class HandlerParentAttribute(object):
    """ Non-data descriptor that provides access to parent attributes but is
    overridable by local attributes. """
    def __init__(self, getter, local_default=None):
        self._getter = getter
        self._default = local_default

    def __get__(self, obj, objtype=None):
        if not obj:  #  Do not generate errors if we access this as a class attribute so we can test its presence with getattr.
            return self
        parent = obj.parent
        try:  # Elevate AttributeError to RuntimeError, otherwise weird
            return self._getter(parent) if parent else self._default
        except AttributeError:  # Re-raise, but preserve traceback info
            raise RuntimeError, sys.exc_info()[1], sys.exc_info()[2]

class Handler(object):
    """ Basic generic handler base class. All handlers should be derived from this
    class.

    If the handler is provided with a 'parent_getter' function, the parent
    object can be accessed through the 'parent' property.

    Define all attributes that can be initialized with keywords arguments in
    the class so they won't be passed on to other subclasses.
    """
    __metaclass__ = HandlerMeta  # Allows automatic registration
    __handler_for__ = None  # Do not register this handler
    __handler_name__ = None

    @classmethod
    def get_handler_name(cls):
        return cls.__handler_name__

    def __init__(self, parent_getter=None, **kwargs):
        """ Create the handler with optional parent object. The optional
        keyword paramaters are used to set the Handler attributes with the
        same name.
        """
        # Pass attributes that are not used here to other subclasses (not necessarily 'object', depending on the order of subclasses)
        super(Handler, self).__init__(**{k:v for k,v in kwargs.items() if not hasattr(type(self), k)})
        self.logger = logging.getLogger(__name__)
        self.logger.debug("%s: Creating handler with parameters %s" %
                          (self.__class__.__name__, kwargs))
        self._parent_getter = parent_getter
        for k, v in kwargs.items():
            if hasattr(type(self), k):
                setattr(self, k, v)

    @property
    def parent(self):
        return self._parent_getter() if self._parent_getter else None
