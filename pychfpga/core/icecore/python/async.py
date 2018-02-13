'''
Asynchronous helpers used to bridge pydfmux with the Tornado framework.
'''

import tornado.ioloop, tornado.gen
import inspect
import functools

class Parallelizable(object):
    """Base class for calls that can be emitted asynchronously.

    You can invoke instances of this class as if they were functions:

        >>> p = Parallelizable()
        >>> p(1,2,3)

    Calling a Parallelizable this way does something "synchronous", i.e. it
    acts like an ordinary Python function and returns something simple (say,
    an integer.) However, you can *also* invoke Parallelizables via a sneaky,
    asynchronous route:

        >>> p = Parallelizable()
        >>> p.__call_async__(1,2,3)

    This asynchronous method returns a Tornado Future that's a placeholder
    for a result (again, say, an integer). In fact, the "synchronous" call
    above is just a wrapper to make the asynchronous method behave nicely.

    This class is intended to be subclassed to do something useful; you can
    see examples in tuber.py.

    The '__get__' and instance bumpf in here is required so that we can
    emulate class methods (which have an associated 'self'.)
    """

    instance = None  # starts out unbound # JFC: Never used?

    def __init__(self, coroutine=None, doc=None):
        # """ Create  the Paralleizable object that wraps the Tornado coroutine
        # (i.e. when called, the coroutine returns a Future).

        # We assume that 'coroutine' is an unbound method or function.
        #  """
        if coroutine:
            self.__call_async__ = coroutine
        if doc is None:
            self.__doc__ = inspect.getdoc(coroutine)
        else:
            self.__doc__ = doc

    def __get__(self, instance, owner):
        """Descriptor allowing us to behave as bound or unbound methods.

        This method is called only if an object is a class attribute and
        therefore is not bound to a specific instance. In this case, when we
        access thhis object though an instance (e.g. d.set_frequencY(...)),
        then return a new Parallelizable object where the coroutine is bound
        to that instance.
        """
        if instance is None:
            return self  # Unbound, with nothing to bind to.

        if self.instance:  # JFC: This is never true
            return self.instance.__get__(instance, owner)  # Bound.

        # Otherwise, we're an unbound instance with a binding provided.
        # Generate a captive, bound, subclass and return it.
        bound = self.__class__(
            coroutine=functools.partial(self.__call_async__, instance),
            doc=self.__doc__)
        return bound

    def __call__(self, *args, **kwargs):
        # """ Call the coroutine as a normal synchronous call. This launches an
        # IOLoop that runs until the coroutine has returned its value.
        # """
        old_loop = tornado.ioloop.IOLoop.current()
        try:
            io_loop = tornado.ioloop.IOLoop()
            io_loop.make_current()
            future = self.__call_async__(*args, **kwargs)
            io_loop.run_sync(tornado.gen.coroutine(lambda: (yield future)))
            return future.result()
        finally:
            io_loop.close()
            old_loop.make_current()

    @tornado.gen.coroutine
    def __call_async__(self, *args, **kwargs):
        '''Stub for asynchronous call that returns a Future'''
        raise NotImplementedError()


    async = property(lambda self: self.__call_async__)

    def async_map(self, vararg_list, *args, **kwargs):
        """ Call this asynchronous function concurrently for with every value
        of the iterable vararg_list as first parameter. """
        arg_list = list(vararg_list)
        return async_call([self]*len(arg_list), arg_list, *args, **kwargs)


def asynchronously(p, *args, **kwargs):
    '''Helper function to call a Parallelizable and get a Future.

    This is useful when writing parallel code. The following:

        @pydfmux.algorithm(pydfmux.Dfmux)
        def my_alg(ds):
            yield [asynchronously(d.set_frequency, a, b, c) for d in ds]

    ...will dispatch 'set_frequency' on all of the 'ds' objects
    asynchronously. It will run much faster than the following:

        @pydfmux.algorithm(pydfmux.Dfmux)
        def my_alg(ds):
            for d in ds:
                d.set_frequency(a, b, c)
    '''

    if not isinstance(p, Parallelizable):
        raise TypeError("asynchronously() must receive a Paralellizable as "
                        "its first argument!")
    return p.__call_async__(*args, **kwargs)


def async(func):
    """ Decorator that converts a function or method in a future-returning coroutine
    and wraps it into a Parallelizable object that can be called synchronously
    (as a normal blocking call, using a=func(...)) or asynchronously (using
    a=yield func.async(...)).
    """
    return Parallelizable(coroutine=tornado.gen.coroutine(func))


def async_return(value):
    """ Return a value from a coroutine. """
    raise tornado.gen.Return(value)


def async_call(func_list, variable_arg_list, *args, **kwargs):
    """ Concurrently call all functions or methods listed in 'func_list'. If
    'variable_arg_list' is not none, each function will be called with its
    first argument taken from the corresponding element in that list. Position
    and keyword arguments that are common to all calls can also be passed.
    """

    if variable_arg_list is not None:
        func_list = [functools.partial(func, arg) for (func, arg)
                     in zip(func_list, variable_arg_list)]

    if all([isinstance(f, Parallelizable) for f in func_list]): # If all the functions are coroutines
        @async        #  Create a Parallelizable object that runs all coroutines in parallel
        def p(*args, **kwargs):
            async_return((yield [f.async(*args, **kwargs) for f in func_list]))
        return p(*args, **kwargs)
    else:  # Otherwise, fall back on a looped invocation.
        return [f(*args, **kwargs) for f in func_list]


# commonly used Tornado asyncronous calls

async_sleep = tornado.gen.sleep  # eturn a Future that resolves after the given number of seconds. Usage: yield async_sleep(0.5)
async_moment = tornado.gen.moment # allow the IOLoop to run for one iteration. Usage: yield async_moment

# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
