# import logging
import collections
import re
import yaml
import copy


re_dot = r"\."


class NameSpace(object):
    """ Wraps an iterable (list, dict) so their items can be accesed as attributes.

    Usage:

    n = NameSpace(a=1, b=2, c=dict(e=4, f=5))
    print n.c.e

    d=dict(a=1, b=2, c=dict(e=4, f=5)
    n = NameSpace(d)
    n2 = NameSpace(n)
    print n.c.e


    """
    def __init__(self, *args, **kwargs):
        if not args:
            obj = kwargs
        elif len(args) > 1 or kwargs:
            raise TypeError('Specify either a single object or keyword list')
        elif isinstance(args[0], NameSpace):
            obj = args[0]._obj
        else:
            obj = args[0]
        object.__setattr__(self, '_obj', obj)

    def _to_namespace(self, x):
        if isinstance(x, collections.Mapping):  # and not isinstance(x, basestring):
            return NameSpace(x)
        else:
            return x

    def __getattr__(self, name):
        try:
            # return self._to_namespace(getattr(self._obj, name)) # do we want attrbute-accessed objects to be converted into namespace?
            return getattr(self._obj, name)
        except AttributeError:
            if hasattr(self._obj, '__getitem__'):
                return self._to_namespace(self._obj[name])
            else:
                raise

    def __setattr__(self, name, value):
        try:
            setattr(self._obj, name, value)
        except AttributeError:
            if hasattr(self._obj, '__getitem__'):
                self._obj[name] = value
            else:
                raise

    def __getitem__(self, name):
            return self._to_namespace(self._obj[name])

    def __setitem__(self, name, value):
            self._obj[name] = value

    def __delitem__(self, name):
            del self._obj[name]

    def __iter__(self):
        for x in self._obj:
            yield self._to_namespace(x)

    def iteritems(self):
        for (k, v) in self._obj.iteritems():
            yield (k, self._to_namespace(v))

    def itervalues(self):
        for v in self._obj.itervalues():
            yield self._to_namespace(v)

    def items(self):
        return list(self.iteritems())

    def values(self):
        return list(self.itervalues())

    def get(self, name, default=None):
        return self._to_namespace(self._obj.get(name, default))

    def pop(self, *args, **kwargs):
            return self._to_namespace(self._obj.pop(*args, **kwargs))
    def popitem(self):
        k, v = self._obj.popitem()
        return (k, self._to_namespace(v))

    def setdefault(self, *args, **kwargs):
        return self._to_namespace(self._obj.setdefault(*args, **kwargs))

    def __len__(self):
        return len(self._obj)

    def __contains__(self, x):
        return x in self._obj

    def __str__(self):
        return str(self._obj)

    def __repr__(self):
        return 'NameSpace(%r)' % (self._obj, )

    def __dir__(self):
        attrs = set(dir(type(self)) + vars(self).keys() + dir(self._obj) +
            self._obj.keys() if isinstance(self._obj, collections.Mapping) else [] )
        return list(attrs)

    def deepcopy(self):
        return NameSpace(copy.deepcopy(self._obj))

    def as_dict(self):

        def todict(v):
            if is_mapping(v):
                return {k: todict(i) for k, i in v.items()}
            elif is_sequence(v):
                return [todict(i) for i in v]
            elif isinstance(v, NameSpace):
                return todict(v._obj)
            else:
                return v
        return todict(self._obj)

    def as_yaml(self):
        return yaml.safe_dump(self.as_dict(), default_flow_style=False)


    def iterfind(self, pattern='**', lastpath='', search_lists=False, verbose=0):
        """Generator that yield the full paths to every elements in obj, depth-first.

        Parameters:

            pattern (str): pattern to search for

            lastpath (str): base path used with relative paths.

            search_list (bool): If true, lists items and content and their contnts will be searched.
                Can significantly slow down searches if a cross-level wildcard is used early in the
                path.

        If the pattern is a relative path (i.e starting with any number of '.'), the path is based
        on lastpath. Otherwise we have an absolute path (i.e starting from the top of the object)
        and lastpath is ignored.

        Patterns:

            '*' matched any character of the label in the current level only
            '**' matches any characters across levels
            '[n]' selects a list item (of search_lists=True)

        """

        if pattern.startswith('.'):  # if a relative path
            while pattern.startswith('.'):
                lastpath = lastpath.rsplit('.', 1)[0]
                pattern = pattern[1:]

            if lastpath:
                pattern = lastpath + '.' + pattern

        # build the full path regular expression
        for sub, repl in (('**', '::'), ('.', re_dot), ('[', re_dot +r'\['), (']', r'\]'), ('*', '[^.]*'), ('::', '.*?')):
            pattern = pattern.replace(sub,repl)

        # if verbose:
        #     print('Full pattern is %s' % (pattern + '$'))
        full_pattern = re.compile(re_dot + pattern + '$') # to be used with .match()

        # Compute the pattern that will be used to determine if we descend into sub-elements.
        # It will save a lot of computational time if we don't have to descend into all possible branches

        # Stop path at wildcards that cross hierarchical levels. We have to check all the way down anyway once we got one...
        if '.*?' in pattern:
            pattern = pattern.split('.*?')[0] + '.*?'

        re_int_index = re.compile(r'\[(\d+)\]$')

        split_pattern = pattern.split(re_dot)
        not_wild = [int('*' not in s) for s in split_pattern]
        is_int_index = [re_int_index.match(s) for s in split_pattern]
        int_index = [int(r.group()) if r else None for r in is_int_index]
        number_of_levels = len(split_pattern)
        # if verbose:
        #     print("Split pattern elements are %s\n-------------" % (','.join("'%s'" % s for s in split_pattern)))

        # Mapping = collections.Mapping
        Sequence = collections.Sequence
        def paths(prefix, obj, level=0):
            """
            Search a specific node `obj` for the target pattern.

            Parameters:
                obj (dict): An object to get paths from.

                prefix (str): prefix to add to the names found in the current object. Used for recursion.

                level (int): The index of the pattern element we are now looking for. Used to accelerate searches when possible.
            """
            if level >= number_of_levels:
                return
            if hasattr(obj, 'iteritems'):# and isinstance(obj, Mapping): # the hasattr() test is much faster than isinstance. It is significantly faster not to check isinstance at all.
                lpat = split_pattern[level]

                if lpat in obj:  # test obj[lpat] instead of all obj's children if lpat is found in obj. This really helps only when we end up going down long lists
                    new_prefix = '%s.%s' % (prefix, lpat) # if not isinstance(child_prefix, str) else child_prefix)
                    child_obj = obj[lpat]
                    if full_pattern.match(new_prefix):
                        #print('match prefix=%s, lpat=%s' % (new_prefix, lpat))
                        yield new_prefix, obj, child_obj #self._to_namespace(child_obj) 28 ms for _to_namespace()
                    for item in paths(new_prefix, child_obj,  level + not_wild[level]):
                        yield item
                else:  # test every items if the dict, and their children
                    #print('testing items in %r' % obj)
                    for (child_prefix, child_obj) in obj.iteritems(): #9 ms gain by using obj.iteritems directly
                        new_prefix = '%s.%s' % (prefix, child_prefix) # if not isinstance(child_prefix, str) else child_prefix)
                        if full_pattern.match(new_prefix):
                            yield new_prefix, obj, child_obj #self._to_namespace(child_obj) 28 ms for _to_namespace()
                        for item in paths(new_prefix, child_obj, level + not_wild[level]):
                            yield item
            elif search_lists and not isinstance(obj, basestring) and isinstance(obj, Sequence):
                index = int_index[level]
                if index is not None:
                    new_prefix = '%s.[%i]' % (prefix, index) # if not isinstance(child_prefix, str) else child_prefix)
                    child_obj = obj[index]
                    yield new_prefix, obj, child_obj
                    for item in paths(new_prefix, child_obj, level + not_wild[level]):
                        yield item
                else:
                    for (index, child_obj) in enumerate(obj):
                        new_prefix = '%s.[%i]' % (prefix, index) # if not isinstance(child_prefix, str) else child_prefix)
                        if full_pattern.match(new_prefix):
                            yield new_prefix, obj, child_obj #self._to_namespace(child_obj) 28 ms for _to_namespace()
                        for item in paths(new_prefix, child_obj, level + not_wild[level]):
                            yield item

        return ((k[1:].replace('.[', '['), self._to_namespace(p), self._to_namespace(v)) for k, p, v in paths('', self._obj))

    def findall(self, pattern, lastpath=''):
        return dict(self.iterfind(pattern, lastpath))

    def findone(self, pattern, lastpath=''):
        gen = self.iterfind(pattern, lastpath)
        res = next(gen, None)
        if res is None:
            raise AttributeError('Cannot find pattern %s' % pattern)
        if next(gen, None) is not None:
            raise AttributeError('More than one entry matches the pattern %s' % pattern)
        return res

    def is_sequence(self):
        return isinstance(self._obj, collections.Sequence)

    def is_mapping(self):
        return isinstance(self._obj, collections.Mapping)

    def merge(self, dest, skip_none=False, add_lists=False):
        object.__setattr__(self, '_obj', merge_dict(self._obj, dest))
        return self


def is_sequence(obj):
    if isinstance(obj, NameSpace):
        obj = obj._obj
    return isinstance(obj, collections.Sequence) and not isinstance(obj, basestring)


def is_mapping(obj):
    if isinstance(obj, NameSpace):
        obj = obj._obj
    return isinstance(obj, collections.Mapping)

def is_namespace(obj):
    return isinstance(obj, NameSpace)

def merge_dict(src, dest, skip_none=False, add_lists=False):
    """ Merge a hierarchy of dictionnaries or lists.

    - Only a dict can be merged with a dict

    - Dicts are merged as follow:
        - If the destination item does not exist is it created from the source
        - If both the source and destination item is a dict then those are merged
        - If only one of the source or destination is a dict there is an error

    """

    #print(type(src), is_mapping(src), type(dest), is_mapping(dest))
    # logger = logging.getLogger('')
    if skip_none and dest is None:
        return
    if is_namespace(dest):
        dest = dest._obj
    if is_mapping(src):
        if not is_mapping(dest):
            raise TypeError('Only a mapping can be merged with another mapping. Types are src=%s, dest=%s' % (type(src), type(dest)))
        # print ' --- merge ', src, 'to', dest
        # src = src or {}
        new = type(src)()
        for k in set(src.keys()) | set(dest.keys()):
            if k in src and k in dest:
                new[k] = merge_dict(src[k], dest[k], skip_none=skip_none)
            elif k in src:
                new[k] = src[k]
            else:
                new[k] = dest[k]
    elif add_lists and is_sequence(src):
        if not is_sequence(dest):
            raise TypeError('Only a sequence can be merged with another sequence. Types are src=%s, dest=%s' % (type(src), type(dest)))
        new = src + dest
    else:
        # logger.warning('%.32s: Overriding  %s with %s' % ('merge_dict', src, dest))
        new = dest
    #print(new)
    return new
