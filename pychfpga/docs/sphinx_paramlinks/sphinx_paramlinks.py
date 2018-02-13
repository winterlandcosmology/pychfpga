#!coding: utf-8
import re
from docutils import nodes
from docutils.transforms import Transform
import os
from sphinx.util.osutil import copyfile
from sphinx.util.console import bold
from sphinx.roles import AnyXRefRole
from sphinx.domains.python import PythonDomain
from distutils.version import LooseVersion
from sphinx.util.nodes import make_refnode
from sphinx.util.compat import Directive
from sphinx import addnodes

from sphinx import __version__
from sphinx.domains import ObjType

PARAMLINK_CLASS_NAME = 'paramlink' # class applies to references and targets for use by the CSS
PARAMETER_TYPE = 'parameter'
OBJ_TYPE_KEY = 'py:obj_type'
OBJ_PATH_KEY = 'py:obj_path'


# Separator between parent object and patameter name:
#   - is used both to create HTML ref IDs and as a Sphinx cross-reference object table key
#   - Don't use '.', or the standard resolve_any_xref() will dig up all the references to parameter with that named across the whole documentation.
#   - Use a character allowed in HTML4 IDs (HTML5 accepts almost anything; for example, '>' works just fine)
#   - Don't use characters that might appear in python object names or parameter names
PARAM_REF_ID_SEPARATOR = ':'

# PARAM_OBJECT_SEPARATOR = ':' # Separator between the method and parameter names in object table.

NO_PARAM_LIST = ['None']

PARAM_MARKER = "_sphinx_paramlinks_"  # used to uniquely tag the parameter text so it can be unambiguously identified and re-processed.
verbose = 1 # Print messages: 0: never, 1: if errors, 2: if warnings, 3: info

######################################
# Support functions
######################################

def _is_html(app):
    """ *True* if we are building for a HTML target. """
    return app.builder.name in ('html', 'readthedocs')

def param_name_from_param_text(paramname, strip_markup=False):
    """ Extract the parameter name from the parameter text """
    # if the param name is in a literal block (e.g. ``\`\`param_name\`\```, extract just the name
    literal_match = re.match(r'^``(.+?)``$', paramname)
    if literal_match:
        paramname = literal_match.group(1)
    refname = paramname
    # If a default value was given in the form  param_name=some_value, extract just the parameter name
    eq_match = re.match(r'(.+?)=.+$', refname)
    if eq_match:
        refname = eq_match.group(1)
    # Remove backslashes if `strip_markup` is ``True``
    if strip_markup:
        refname = re.sub(r'\\', '', refname)
    return refname

def make_refid(parent_object_name, param_name):
    """ Build the reference ID of a parameter from the parent object name and its parameter name"""
    if PARAM_REF_ID_SEPARATOR in param_name :
        raise RuntimeError("Parameter names mush not contain the '%s' character " % (PARAM_REF_ID_SEPARATOR))
    return '%s%s%s' % (parent_object_name, PARAM_REF_ID_SEPARATOR, param_name)

# def refid_to_objectname(refid):
#     """ replace the last '.params.' with a '>' """
#     return PARAM_OBJECT_SEPARATOR.join(refid.rsplit(PARAM_REF_ID_SEPARATOR, 1))

# def objectname_to_refid(object_name):
#     """ replace '>' with '.params.' """
#     return object_name.replace(PARAM_OBJECT_SEPARATOR, PARAM_REF_ID_SEPARATOR)


#########################################################################
# Autodoc Docstring pre-processor: add :currentobject: and :paramtarget:
#########################################################################

def autodoc_process_docstring(app, what, name, obj, options, lines):
    """ *Autodoc* hook to pre-process docstrings and add current object context information and link
    targets to parameter entries. Also works with Napoleon-processed docstrings.

    The following is done:
        - Add the ``:currentobject:`` role to the first line of the docstrings to set information on
          the current object for the :paramtarget: and :any: roles used subsequently in the
          docstring. (see `lookup_reference`)
        - Add the ``:paramtarget:`` role to the docstring description text to allow `LinkParams`
          make the parameter into a target with the proper id. This automatically adds an entry in
          the object cross-reference table and an index entry.

    Example:

    The following docstring::

        This method returns something.

        :param int myparam: Number to be returned

    will become::

        :currentobject:`mypackage.mymodule.myclass.mymethod` This method returns something.

        :param int myparam: :paramtarget:`myparam` Number to be returned

    """
    # Add the ``:currentobject:`` role at the top of the description block to set the current object
    # info that will be used by the :paramtarget: and :any: roles.
    if lines and not lines[0].startswith(':currentobject:'):
        lines[0] = (':currentobject:`%s` '% name) + lines[0]
    elif verbose >= 2:
        print 'PARAMLINKS: Warning: object %s already has a currentobject directive. Skipping' % name


    def _cvt_param(object_name, line):
        # if object_name.endswith(".__init__"):
        #     # kill off __init__ if present, the links are always
        #     # off the class
        #     object_name = object_name[0:-9]


        def cvt(m):
            modifier, param_text, desc = m.group(1) or '', m.group(2), m.group(3)
            param_name = param_name_from_param_text(param_text, strip_markup=True)
            # skip if the docstrings get processed multiple times
            if param_name in NO_PARAM_LIST:
                return m.group()

            # Return modified param line, but don't modify if it's already modified
            if ':paramtarget:' in desc:
                if verbose >= 2:
                    print 'PARAMLINKS: Warning: param %s already processed. Skipping. line=%s' % (param_text, m.group())
                return m.group()

            return ":param %s%s: :paramtarget:`%s` %s" % (  # modifier includes trailing space(s)
                    modifier, param_text, param_name, desc)
        newline = re.sub(r'^:param ([^:]+? )?([^:]+?):(.*)', cvt, line)
        if newline != line and verbose >=3:
            print 'PARAMLINK (in %s): modified "%s" to "%s"' % (object_name, line, newline)

        return newline



    # Process docstring lines to add a :paramtarget:`param_name` at the beginning each :param: description text
    # This will be parsed into a pending_xref containing the info needed for LinkParams to make the parameter into a target.
    if what in ('function', 'method', 'class'):
    # if True or callable(obj):
        if verbose >= 3:
            print 'PARAMLINKS: processing autodoc strings on %s object %s in file %s' % (what, name, app.env.temp_data['docname'])
        lines[:] = [_cvt_param(name, line) for line in lines]


######################################
# Aesthetic link transformer
######################################



class LinkParams(Transform):
    """
    Moves the ``:paramtarget::` target nodes before the parameter name and add a permalink.

    The target node is moved *before* the parameter name, and the permalink is placed just before before the description (before the '--').

    This is a purely aestheric transform. The links work just fine without this.
    """
    default_priority = 210


    def apply(self):

        def find_field_param_node(xref):
            """
            Find the parent node that encompasses the parameter name, the separator '--' and the description

            In domain 'py', ':param:' is a TypedField.
            As per sphinx.utils.docfields.TypedField.make_field (and also GroupedField), the
            parameter name is the first node of the nodes.paragraph() that is above
            the parameter description text (where our target is originally located)
            """
            ref = xref
            while ref and not isinstance(ref, nodes.paragraph): # could also stop at list item, desc_name etc.
                ref = ref.parent
            if not ref:
                raise RuntimeError('PARAMLINKS.LinkParams: could not find parameter container paragraph...')
            return ref


        # Find all the 'paramtarget' target nodes
        for target_node in self.document.traverse(nodes.target):
            if 'paramtarget' not in target_node:
                continue
            ref = find_field_param_node(target_node)

            # Move target node before parameter
            target_node.parent.remove(target_node)
            ref.insert(0, target_node) # insert *after* remove

            # Add a permalink before the '--' separator
            if _is_html(self.document.settings.env.app): # add permalink if a HTML document
                seps = ref.traverse(lambda n: n.astext() ==' -- ')  # a Text (Node) node
                if not seps:
                    continue
                permalink = nodes.reference(
                    '', '',
                    nodes.Text(u"¶", u"¶"),
                    refid=target_node['ids'][0],
                    # paramlink is our own CSS class, headerlink
                    # is theirs.  Trying to get everything we can for
                    # existing symbols...
                    classes=[PARAMLINK_CLASS_NAME, 'headerlink'])
                newsep = nodes.Text('-- ')
                seps[0].parent.replace(seps[0], [permalink, newsep])

######################################
# Fallback Cross-reference resolver
######################################


def lookup_reference(app, env, node, contnode):
    """
    Resolves unresolved "pending_xref" using the current extended context object information.

    The context information should have been set by a previous ``currentobject`` statement.

    The resolver will handle both standard module/class references (e.g.
    package.module.class.method) and special  references (e.g. package.module.class.method:someobject).

    Resolution is performed by gradually narrowing down the search scope by adding current object information until there is
    only one solution.


    If an explicit path is provided with the reference, `lookup_reference` uses only that path.

    This mechanism also works for any standard object that fail because resolve_xref() and
    resolve_any_xref() found too many matches.

    The Sphinx BuildEnvironment then gives us one more chance to do this lookup by allowing
    registering `lookup_reference` to the "missing-reference" event using::

        app.connect('missing-reference', lookup_reference)
    """

    reftype = node['reftype']
    target = node['reftarget']
    refdoc = node.get('refdoc', None)
    obj_type = node.get(OBJ_TYPE_KEY, '')
    obj_path = node.get(OBJ_PATH_KEY, '')
    has_explicit_title= node['refexplicit']

    def find_objects(domain, targets):
        objects = env.domains[domain].data['objects']
        return {(objname, objdoc, objtype)
                for target_type, target_name in targets
                for objname, (objdoc, objtype) in objects.items()
                if ((not target_type or target_type == objtype)
                    and objname.endswith(target_name))}

    # if reftype in ['any', 'paramref']:

    # Extract the context info from the target name
    if '.' in target:  # if some context was provided
        context_name, param_name = target.rsplit('.', 1)
    else:
        context_name, param_name = ('', target)

    # Remove the leading '~' and set the display name (the text to be shown by the reference link)
    if context_name.startswith('~'):
        context_name = context_name[1:]
        display_name = param_name
    elif context_name:
        display_name = context_name + '.' + param_name
    else:
        display_name = param_name

    mess = []
    mess.append('PARAMLINKS LOOKUP :%s:`%s`' %  (reftype, target))
    mess.append('   Refnode attributes: %s' % node.attributes)
    mess.append('   Display name = %s' % display_name)

    # determine the search targets
    search_targets = []
    # If reftype is any or paramref, add a parameter search target which uses a '>' separator
    if reftype in ('any', 'paramref'):
        search_targets.append((PARAMETER_TYPE, context_name + PARAM_REF_ID_SEPARATOR + param_name))
    # if we search for any element, then also look for any object
    if reftype == 'any':
        search_targets.append(('', context_name + '.' + param_name))

    if not search_targets:
        mess.append("   Unsupported reference type %s. Don't know what to search. No search performed" % reftype)
        if verbose >= 2:
            print '\n'.join(mess)
        return None

    mess.append("   Looking for %s" % (' or '.join('(%s:*%s)' % (tt or '*', tn)  for tt, tn in search_targets)))

    #if a context was provided with the target, well, that's what the customer want and we'll stick to that, even if we find no or multiple targets
    # Also fall back to an explicit search if the node does not contain context info (obj_path, added by the ``currentobject`` directive)
    if context_name or not obj_path:
        mess.append('   Searching objects %s only with provided context (in any). There is no object context info with the reference.' % search_targets)
        objects = find_objects('py', search_targets)
    # otherwise, search from the least specific context (no context at all), and gradually add
    # context until we have only one solution or no more solutions.
    else:
        mess.append('   Searching objects %s with the context found within the reference (obj_type=%s, obj_path=%s) ' % (search_targets, obj_type, obj_path))
        context_elements = obj_path.split('.')
        for depth in range(len(context_elements) + 1):  # from 0 to N context elements
            context = '.'.join(context_elements[-depth:]) if depth else '' # Context is the `depth` last elements
            contexted_targets = [(typ, context + name) for typ, name in search_targets]
            objects = find_objects('py', contexted_targets)
            mess.append('      Trying depth=%i: %i results (context=%s, targets = %s)' % (depth, len(objects), context, contexted_targets))
            # If we found no match, it won't get better with more context. Give up.
            # If we found one match only, that's good enough for us! Stop here!
            if len(objects) <= 1:
                break
            for object_name, _ , _ in objects:
                mess.append('         --> %s' % object_name)

    # Bail out if we dont have satisfactory results
    if not len(objects): # If no result
        mess.append("   Found nothing!")
        if verbose >= 1:
            print '\n'.join(mess)
        return None
    elif len(objects) > 1:  # if multiple results
        mess.append("   Found %i results. We want only one. No link is generated." % len(objects))
        if verbose >= 1:
            print '\n'.join(mess)
        return None

    # if only one result

    object_path, object_doc, object_type = objects.pop()
    mess.append('   Found object: %s' % object_path)
    # ref = objectname_to_refid(object_path) # convert from mangled name back to link name
    ref = object_path
    mess.append('   Creating reference to: %s' % ref)

    # if the node does not have an explicit title, create a new content node with our display name
    # Add a specific class  name to the ref so we can style it
    if has_explicit_title:
        new_contnode = contnode
        new_contnode['classes'] += [PARAMLINK_CLASS_NAME]
    else:
        classes = contnode['classes'] + [PARAMLINK_CLASS_NAME]
        new_contnode = nodes.literal(text=display_name, classes= classes) # displayed content element inside the reference node


    if verbose >=3:
        print '\n'.join(mess)

    return make_refnode(app.builder, refdoc, object_doc, ref,
                        new_contnode, display_name)

#######################
# Custom roles
#######################



def currentobject_role(role, rawtext, text, lineno, inliner, options={}, content=[]):
    """ ``:currentobject:`` role that stores the specified text as the current object name (e.g.
    packagename.modulename.classname.methodname) in the current context. This will be used by
    ``:any:`` and ``paramtarget`` roles.
    """
    if verbose >= 3:
        print 'PARAMLINKS.currentobject_role: setting %s=%s' % (OBJ_PATH_KEY, text)
    env = inliner.document.settings.env
    env.ref_context[OBJ_PATH_KEY] = text
    return [], []


def paramtarget_role(role, rawtext, text, lineno, inliner, options={}, content=[]):
    """
    Sphinx role to create a reference target to the object named *text* using the object name information
    stored in the current context and create corresponding object cross-reference and index entries.
    """
    env = inliner.document.settings.env
    parent_object_name = env.ref_context[OBJ_PATH_KEY]
    param_name = text
    refid = make_refid(parent_object_name, param_name)
    if verbose >= 3:
        print 'PARAMLINKS.paramtarget_role: creating target for %s: %s' % (param_name, refid)
    node = nodes.target('', '', ids=[refid], paramtarget=True)

    # Add object referece entry
    objects = env.domaindata['py']['objects']
    object_entry = (env.docname, PARAMETER_TYPE)
    if object_entry in objects:
        if verbose >= 2:
            print 'PARAMLINKS.paramtarget_role: target %s already in the object table. Ignoring.' % (refid)
    else:
        objects[refid] = object_entry

    # Create index node
    index_name = '%s (%s parameter)' % (param_name, refid.rsplit('.', 1)[-1])
    index_entry = ('single', index_name, refid, '', None)
    indexnode = addnodes.index(entries=[index_entry])
    return [node, indexnode], []

# paramtarget_role.options = {'class': directives.class_option,
#                      'language': directives.unchanged}


#######################
# Stylesheet management
#######################

def add_stylesheet(app):
    """Registers the stylesheet associated with this extension"""
    app.add_stylesheet('sphinx_paramlinks.css')


def copy_stylesheet(app, exception):
    """ Copy the stylesheet from the *extension source folder* to the to destination build folder. By
    default, Sphinx only copies stylesheets from the _static folder located at the root of the documentation
    folder.
    """
    app.info(
        bold('The name of the builder is: %s' % app.builder.name), nonl=True)

    if not _is_html(app) or exception:
        return
    app.info(bold('Copying sphinx_paramlinks stylesheet... '), nonl=True)

    source = os.path.abspath(os.path.dirname(__file__))

    # the '_static' directory name is hardcoded in
    # sphinx.builders.html.StandaloneHTMLBuilder.copy_static_files.
    # would be nice if Sphinx could improve the API here so that we just
    # give it the path to a .css file and it does the right thing.
    dest = os.path.join(app.builder.outdir, '_static', 'sphinx_paramlinks.css')
    copyfile(os.path.join(source, "sphinx_paramlinks.css"), dest)
    app.info('done')



# Add the 'parameter' object type and associate it with the role name
# We don't seem to need this
PythonDomain.object_types[PARAMETER_TYPE] = ObjType(PARAMETER_TYPE, 'paramref')  # object_types[object_type_name] = ObjType(localized_name, role1, role2...)

def setup(app):
    """ Called at Sphinx initialization to register the extension """

    app.add_role_to_domain("py", "currentobject", currentobject_role)  # Sets the current object name context
    app.add_role_to_domain("py", "paramtarget", paramtarget_role)  # Create a target node and xref/index entries based on the context-based object name
    app.add_role_to_domain("py", "paramref", AnyXRefRole())  # Same as :any: role: Creates a pending_xref node augmented with all possible context info so we can solve intra-method references

    app.connect('autodoc-process-docstring', autodoc_process_docstring) # Modify parameter names for later identification and build parameter reference table
    app.add_transform(LinkParams) # Optional cosmetic changes to move parameter target links before the param name and add a permalink
    app.connect('missing-reference', lookup_reference) # process references that were not resolved, including parameter references

    # Handle custom stylesheet
    app.connect('builder-inited', add_stylesheet)
    app.connect('build-finished', copy_stylesheet)
