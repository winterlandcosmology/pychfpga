-----------------------------
Branches, releases and tags
-----------------------------

One commit of ``pychfpga`` rarely works on every platform and firmware configuration at once.
Branches and release tags therefore name the platform and configuration they were validated for,
using the chFPGA names: ``ice_ct``, ``ice_corr16``, ``crs_ct``, ``crs_corr8``, ``crs_corr32``,
``crs_corr64``, ``zcu111_ct``, ``kc705_corr8``.

Branches
========

A branch that is known to work for one platform and configuration is named
``<prefix>/<platform>_<config>``, for example ``jfc/crs_ct``, ``jfc/dev/crs_ct`` or
``develop/crs_ct``.

The prefix must not itself be a branch. Git stores branches as files, so a name cannot be both a
branch and a folder: ``develop/crs_ct`` cannot be created while a branch ``develop`` exists, and
``jfc/dev/crs_ct`` cannot coexist with ``jfc/dev``. Use prefixes only as folders, or rename the
base branch first.

Release branch
==============

The release branch is ``deploy``.


Development process
===================


#. Branch from the latest release branch.
#. Make the changes. Commit often, with concise messages.
#. Run the tests for the target platform (see :doc:`../testing/firmware`).
#. Merge the latest release branch into yours if it has moved, and test again.
#. Merge into the release branch and push it if you are the release manager, otherwise issue a pull request.
#. Tag the release commit (see below) and push the tag.

Release tags
============

Every release is tagged ``x.y.z+<platform>_<config>``, for example ``2.2.0+crs_ct`` or
``2.2.0+ice_corr16``.

- The tag is a valid PEP 440 version with a *local* part. Python normalizes ``_`` to ``.``, so
  the installed package reports ``2.2.0+crs.ct``.
- No ``v`` prefix, and never a plain ``x.y.z`` tag: a plain tag could be installed without naming
  a platform.
- The version number increases across all platforms and configurations. After
  ``1.2.3+ice_ct``, a CRS release is ``1.2.4+crs_ct`` (patch) or ``1.3.0+crs_ct``.
- A commit validated for several platforms gets one tag per platform (``1.2.3+crs_ct`` and
  ``1.2.3+ice_ct``).
- Release candidates, e.g. tags on a work branch, are ``x.y.zrc1+<platform>_<config>``, then
  ``rc2``, and so on.
- No telescope or application names in tags.

.. code-block:: bash

    git tag 2.2.0+crs_ct
    git push origin 2.2.0+crs_ct

Older tags such as ``2.1.0+ice.co`` follow a previous convention and are kept as they are.

Package version
---------------

The package version is computed by setuptools-scm from the nearest tag. Version 9 or newer is
required: older versions drop the ``+<platform>_<config>`` part and warn
``tag '...' will be stripped of its suffix``.

The nearest tag can belong to any platform. A commit on ``develop/crs_ct`` whose nearest tag is
``1.3.3+ice_co`` reports an ``ice.co`` development version: the branch selects the code, it
does not change the reported version. On a commit with several tags, the version names only one
of the platforms; the tag used to install selects the commit.

Installing for an application
=============================

Always name the platform and configuration, with a tag or branch in a git URL:

.. code-block:: bash

    uv add "pychfpga @ git+https://github.com/winterlandcosmology/pychfpga@2.2.0+crs_ct"
    pip install "pychfpga @ git+https://github.com/winterlandcosmology/pychfpga@2.2.0+crs_ct"

or in ``pyproject.toml``:

.. code-block:: toml

    [tool.uv.sources]
    pychfpga = { git = "https://github.com/winterlandcosmology/pychfpga", tag = "2.2.0+crs_ct", lfs = true }

For a development snapshot, use the branch instead of the tag (``@develop/crs_ct``). From a
package index, pin the full version: ``pychfpga==2.2.0+crs.ct``.

A bare version number is not enough:

- pip and uv see package versions, not git tags.
- ``==1.2.3`` matches every ``1.2.3+...`` variant and the resolver takes the greatest one:
  ``1.2.3+ice.ct`` beats ``1.2.3+crs.ct`` because ``ice`` sorts after ``crs``, whatever platform
  you use. ``>=1.2.3`` has the same problem.
- A git URL without ``@ref`` installs the default branch and ignores the tags.

git-lfs must be installed on the machine that installs the package, otherwise the bitstreams and
ARM firmware are installed as small pointer files.
