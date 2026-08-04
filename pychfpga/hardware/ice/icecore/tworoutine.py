#!/usr/bin/python3

'''
Double-entry style asynchronous coding.

The following example coroutine sleeps asynchronously, then returns double its
argument:

    >>> @tworoutine
    ... async def double_slowly(x):
    ...     await asyncio.sleep(1)
    ...     return 2*x

Unlike traditional coroutines, we can call tworoutines synchronously:

    >>> double_slowly(2)
    4

(This is cheating, and intended only as a shortcut! You *cannot* use the
synchronous invocation from within an asynchronous event loop, since event
loops cannot nest.)

We can also, using a little sugar, call it asynchronously:

    >>> c = (~double_slowly)(2)
    >>> print(c) #doctest: +ELLIPSIS
    <coroutine object double_slowly at 0x...>
    >>> asyncio.run(c)
    4

or

    >>> asyncio.run(double_slowly.__acall__(2))

...which devolves to the usual asyncio case.

Tworoutines can also be used as class methods:

    >>> class Foo:
    ...     @tworoutine
    ...     async def multiply_slowly(self, a, b):
    ...         await asyncio.sleep(1)
    ...         return a * b
    >>> f = Foo()
    >>> f.multiply_slowly(3, 4)
    12
    >>> c = (~f.multiply_slowly)(4, 4)
    >>> asyncio.run(c)
    16

We can also implement these things via inheritance:

    >>> class TripleSlowly(tworoutine):
    ...     async def __acall__(self, x):
    ...         await asyncio.sleep(1)
    ...         return 3*x

This works the same way:

    >>> ts = TripleSlowly()
    >>> ts(2)
    6
    >>> c = (~ts)(3)
    >>> print(c) #doctest: +ELLIPSIS
    <coroutine object TripleSlowly.__acall__ at 0x...>
    >>> asyncio.run(c)
    9
'''

import asyncio
import functools

__all__ = ["tworoutine"]

# Loops for which nest_asyncio has already been applied (keyed by loop object).
_nested_loops = {}


def get_nested_loop():
    """Return the current event loop with nest_asyncio applied, creating one if needed.

    nest_asyncio is applied at most once per loop, tracked in ``_nested_loops``.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop not in _nested_loops:
        import nest_asyncio
        nest_asyncio.apply(loop)
        _nested_loops[loop] = True
    return loop


class tworoutine:
    '''
    Base class for double-entry style asynchronous coding.

    Wraps a coroutine (async function) so it can be invoked as a synchronous function.

    For client code, this class provides two points of entry:

    - tworoutine(args) Synchronous, via tworoutine(args), and
    - Asynchronous, via ~(tworoutine)(args).

    The synchronous entry is


    The object that is created here takes the place of the function or method that it decorates.

    If we decorate a class method, this object lives as a class attribute, and
    is passed the unbound method. The class can act as a non-data description.
    Any access to this object calls __get__, which returns another
    `twofunction` object that now points to the bound method.
    '''

    _used_loops = set()

    # __instance = None

    def __init__(self, coroutine=None, instance=None):
        """
        Parameters:

            coroutine (function / unbound method): coroutine to wrap into this object.

            instance: instance of the object to which the coroutine belongs.
                If specified, this objects represent the bound method.
        """

        # print(f'coroutine={coroutine}')

        # we can't check if `coroutine` is a coroutine because asyncio.iscoroutingfunction() does not work when we rewrap the function into a bound method.

        if not asyncio.iscoroutinefunction(coroutine):
            raise TypeError('Decorator can only be used with coroutines function or method')


        # self.__instance = instance

        if instance is None:
            self.__acall__ = coroutine
        else:
            self.__acall__ = functools.partial(coroutine, instance)

        # Have this object mimic the decorated one
        functools.update_wrapper(self, coroutine)

    def __get__(self, instance, owner):
        '''Descriptor allowing us to behave as bound methods.'''
        # print(f'Getting {instance} from {owner}')

        # We pass the instance separately (i.e we don't functool.partial() the
        # instance right away so the coroutine can still be recognized as
        # one.
        return self.__class__(self.__acall__, instance)

    def __call__(self, *args, **kwargs):
        '''Stub for ordinary, serial call.'''

        # By default, presume a fancy asynchronous version has been coded and
        # invoke it synchronously. This is often all the serial version needs
        # to do anyways.
        loop = get_nested_loop()

        coroutine = (~self)(*args, **kwargs)
        self._used_loops.add(loop)
        try:
            result = loop.run_until_complete(coroutine)
        except Exception as e:
            print(f'Exception during run_until_complete(): {e}')
            raise e
        return result

    def __invert__(self):
        """ Use the invert operator "~" as a shortcut to return the original async function/method

        Usage:

        >>> result = await (~self.mymethod)()

        """
        return self.__acall__

    def __mul__(self, other):
        print(f'{other}')
        return self.__acall__(other)

    @property
    def cr(self):
        """ Return the original coroutine.

        Usage:

        >>> result = await self.mymethod.cr()

        """
        return self.__acall__

    async def __acall__(self, *args, **kwargs):
        '''Stub for a coroutine function.

        This get overriden by the instance variable __acall__ that is set with the original async function at class  instantiation.
        '''
        raise NotImplementedError()


# vim: sts=4 ts=4 sw=4 tw=78 smarttab expandtab
