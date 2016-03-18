Icecore Framework
=================

This folder contains preview code of the ICE software/firmware development
framework.

Icecore provides:

   * Hardware map management of arrays of Iceboards, Icecrates, and FMC
     mezzanines
   * Multithreaded dispatch of function calls and variable accesses across
     multiple resources in the system
   * Python API to control and monitor basic hardware functions
   * A VHDL framework for the FPGA resources on which a project is built
   * Basic system monitoring
   * Logging and exception management

Requirements
------------

The Python code runs on Python 2.7 and requires the following packages:

   * numpy
   * SQLAlchemy (the object-oriented database interface used to manage the
     hardware map)
   * Tornado (Python web framework and asynchronous networking library used to
     send commands to multiple boards simultaneously)
   * yaml (PyYAML, to process text-based YAML hardware map files)

Optional:
   * pybonjour (used to automatically discover Iceboards on the local network
     through mDNS (experimental feature))
   * lxml (used to generate HTML reports with the Quality Control scripts)

Usage
-----

The Icecore repository is to be used as a component of an application project
and should not be modified unless you are an administrator of this repository.

The top-level directory structure of this repository is::

    icecore/
    |- c/          Contains the C sources do build the code running on the ARM processor on the IceBoard
    |- docs/
    |- opkg/
    |- python/     Contains the Python code used to access the ICE hardware and firmware
    |- rtl/ VHDL   source code for the core FPGA firmware that allows the Python code to talk to the FPGA, including an example project
    |- www/ Web    server pages, served by the ARM

The base IceCore code is kept in the '/icecore' subfolder of the
application-specific project. The project directory should follow the same
structure as the icecore directory.

Adding icecore to your project
--------------------------------------

Assuming you are using GIT to manage tour application repository, you can add
icecore to your project by following the steps below:

1. Create a git remote that represent this repository. This is merely a
   convenience so you don't have to specify the whole path on every git subtree
   commands by using the standard *git remote* command:

   ``git remote add –f {remote_name} {repository_url}``

2. Add the Icecore repository as a subfolder of your project
   'myproject/icecore' with the *git subtree add* command:

   ``git subtree add --prefix={path/to/subdir} {remote} [branch] --squash``

The --squash option is generally what you want since it will create a single
commit for the full commit history of the remote repository - it avoid to
somehow pollute your local commit history. For this to work, your project must
not have uncommited changes, and '/path/to/subdir' must not exist.

Example::

$ git remote add -f icecore_remote  https://jfcliche@bitbucket.org/jfcliche/icecore-preview.git
$ git subtree add --prefix=my_project/icecore  icecore_remote  master --squash


Pulling changes from the remote repository
------------------------------------------

If you are informed of wonderful new features that have been added to icecore
and you want to bring them into your project, you can get the changes with *git
subtree pull*:

``git subtree pull --prefix={path/to/subdir} {remote} [branch] --squash``

Here again, --squash prevents the whole history of commits that led to the new
Icecore features to pollute your own project history, but you can omit it if
you think the commit comments will provide useful informations. For example::

$ git subtree pull --prefix=myproject/icecore icecore_remote master --squash
