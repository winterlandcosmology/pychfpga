======================================
:mod:`sphinx_paramlinks` documentation
======================================

This sphinx extension is used to allow cross-references to objects (such as parameters) within functions or methods, and allow making reference to those objects with minimal markup.

Example: with the default role set to ``:any:``, when autodoc (and the Napoleon extension) process the following method, the reference ```myparam``` below will result in a link that will point to the corresponding parameter definition::

   def mymethod(self, myparam):
    """ This method uses `myparam` to do awesome things.

    Arguments:
        myparam (float): A superb parameter
    """

How it works
------------

Python solves explicit objects refernce roles(:rst:role:`:mod:`, :rst:role:`:class:`, etc.) by using the current module and current class context information that is added by the Python objects to resolve ambiguities between class elements. This, however, does not allow unambiguous resolution of objects within a method or function, such as the function/method parameters documented by autodoc, without explicitely specifying the method where the target parameter resides.

To solve this issue, this module provides:

  - The `:currentobject: <currentobject_role>` role which is used to set extended context information (package.module.class.method) at the current position.
  - The `:paramtarget: <paramtarget_role>` role which adds a target node and sets-up all cross-reference and index information using the current extended context information. The internal reference to the object is build by separating the parent object name andthe target object with ':', (e.g. *package.module.class.method\:object*)  to prevent the standard Python resolvers from accidentally (and incorrectly) resolving those,  and rather pass the resolving task to our fallback resolver below.
  - A fallback resolver `lookup_reference` which uses the current extended context information to gradually narrow-down the scope of a object until only one solution is found. This resolver works with explicit types (e.g. ``:paramref:`name```), ``:any:`name``` references, or even standard references that were not solved by Sphinx.

In order to facilitate documentation, the module registers the docstring preprocessor `autodoc_process_docstring` which causes autodoc to automatically adds the ``:currentobject:`` statement at the beginning of each object docstring, and adds a ``:paramtarget:``  to each ``:param:`` entry found in the docstring.  By setting ``:any:`` as the default role, references to the parameters can be made  unambiguously with just ```param_name```. Explicit reference ``:paramref:`param_name``` and contextualized references such as ```mymethod.param_name``` can also be used.

The transform `LinkParams` is also registered with the package to relocate the target point of the reference before the parameter text (as opposed as just after) and add a permalink to HTML outputs.

This extension is complemented by the ``sphinx_paramlinks.css`` CSS which defines proper permalink behavior, and make the parameter references rendered with the *emphasis* role, as is suggested by the Matplotlib documentation standard.

Index entries created by ``:paramtarget:`` work adequately.

Usage
-----

- Add the sphinx_paramlinks extension *after* the Napoleon extension.
- optionally define the default role to be ``:any:``




Limitations
-----------

This extension is really useful only when autodoc directives (e.g. ``.. automethod::``) are used to generate the documentation as the proper markup is done automatically. It can also be used in basic Python domain directives (``.. method::``) but will require the user to manually add the `:currentobject: <currentobject_role>` and `:paramtarget: <paramtarget_role>` entries.

Example of manual method documentation::

   .. method:: mymethod(self, myparam):
      :currentobject:`mypackage.mymodule.myclass.mymethod` This method uses `myparam` to do awesome things.

      :param float myparam: :paramtarget:`myparam` A superb parameter


==============
Module summary
==============

.. currentmodule:: sphinx_paramlinks.sphinx_paramlinks

.. rubric:: Helper functions
.. ..............................

.. autosummary::

   _is_html
   param_name_from_param_text
   make_refid
   setup


.. rubric:: Doctring pre-processor
.. ................................

.. autosummary::

    autodoc_process_docstring

.. rubric:: Cross-reference resolver
.. ..................................

.. autosummary::

    lookup_reference

.. rubric:: Aesthetic link decorator
.. .................................

.. autosummary::

    LinkParams


.. rubric:: Custom Sphinx roles
.. ..............................
.. autosummary::

   currentobject_role
   paramtarget_role

.. rubric:: Stylesheet management

.. autosummary::

   add_stylesheet
   copy_stylesheet

--------------------

===============
Module elements
===============

.. automodule:: sphinx_paramlinks.sphinx_paramlinks
    :members:
