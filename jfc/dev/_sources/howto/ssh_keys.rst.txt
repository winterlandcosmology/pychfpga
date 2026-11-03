SSH keys
========

``pychfpga`` and many of its dependencies live in private Bitbucket git
repositories. Before you clone or ``pip install``, Git must present an SSH
key that those servers accept.

Before you start
----------------

- A Bitbucket account (`https://bitbucket.org <https://bitbucket.org>`_).
- An SSH public/private key pair, with the **public** key attached to that
  Bitbucket account.
- Permission to the ``pychfpga`` and dependency repositories (ask a McGill
  administrator if you do not have access).

Load the key in this shell
--------------------------

One reliable way is to load the private key into the local ``ssh-agent``::

    eval `ssh-agent -s`
    ssh-add path_to_bitbucket_key

You can also enable SSH agent forwarding, or add a ``Host bitbucket.org``
entry in ``~/.ssh/config`` that points at the right ``IdentityFile``.

Confirm that Git can talk to Bitbucket::

    ssh -T git@bitbucket.org

Then continue with :doc:`installation`.
