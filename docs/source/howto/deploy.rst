Code development and versioning guide
=====================================

The main development branch, as of July 3, 2024 is the :code:`deploy` branch.
For synchronizing separate develoment efforts, all the new branches must be
forked from :code:`deploy` branch and, after implementing desired changes,
merged back to :code:`deploy`.

Development process
-------------------

#. Create a new branch from the latest commit on :code:`deploy` branch.

#. Make the desired changes.

#. When developing, commit often and write concise commit messages - this will help other people
   debug the future compability problems.

#. Run the necessary tests to make sure the code work for your target platform.

#. [TBD] Run the tests to ensure there is no problems with other platforms.

#. **Important**: If there are any new commits on :code:`deploy` since your branch was created -
   merge :code:`deploy` into your branch. This is needed to maintain synchronization
   between different development branches. Merging :code:`deploy` into your branch may break
   the code, therefore you must re-run all available tests and fix the problems.

#. Merge your branch into :code:`deploy`.

#. Push all the changes. You need to push changes before putting a tag, so the newest changes will appear in
   :code:`deploy`. If you put a tag before pushing, the latest commit will not be added to :code:`deploy` history.

#. Put a tag on the new commit specifying the version and the target platform of the commit. This
   is needed to ensure you always get the working code for any platform, because in general the
   latest version of the :code:`deploy` may not work on all platforms. See the versioning guide below.

#. Push your tag to the remote by running :code:`git push origin deploy --tags`.

**Note**: If you are in rush, you can skip steps 5-8 and put a tag on your branch - see the guide below.

Versioning guide
----------------

#. The general format of the tag is :code:`x.y.z+[platform].[config]`. The `x`, `y` and `z` are
   version numbers for major releases, minor releases and patches correspondingly. The :code:`[platform]`
   should be a three-symbol word indicating the targed platform. For example, :code:`crs` for CRS boards
   or :code:`ice` for the ICE board. The :code:`[config]` is the target configuration for which the code was tested. For
   example, :code:`co` is for configurations that support on-board correlation and :code:`ct` is for configurations that
   support corner-turn.

#. The version number must always increase. For example, if the latest tag is :code:`1.2.3+ice.co` and you are
   deploying a :code:`crs.ct`, your tag must be either :code:`1.2.4+crs.ct` if it is a small patch or
   :code:`1.3.0+crs.ct` if it is a significant change.

#. If you are tagging a branch that is not :code:`deploy` copy the latest tag on :code:`deploy`, change
   the platform and the mode, and update the version number. Also, add the :code:`rc1` to the tag after the last number
   to indicate that this tag is release candidate. For example, if the latest :code:`deploy` tag was
   :code:`1.2.3+ice.co`, you would change it to something like :code:`1.2.4rc1+crs.ct`.

   When merging your changes into :code:`deploy` follow the regular procedure (steps 1-2) and increase the version number.

   **Note**: If you need to put several consecutive tags on your dirty branch - advance the number in the :code:`rc`
   flag, e.g. :code:`rc2`, :code:`rc3`, etc. This number represents the increasing
   level of shame you accumulate before merging changes into the main branch.

