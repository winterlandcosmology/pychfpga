SSH keys
========

``pychfpga`` lives on GitHub. Some private dependencies still live on
Bitbucket. Before you clone or ``pip install``, Git must present an SSH key
that those servers accept.

Before you start
----------------

- A GitHub account (`https://github.com <https://github.com>`_), and a
  Bitbucket account (`https://bitbucket.org <https://bitbucket.org>`_) if you
  need the private ``wtl.*`` / related packages.
- An SSH public/private key pair, with the **public** key attached to those
  accounts.
- Permission to the ``pychfpga`` and dependency repositories (ask a McGill
  administrator if you do not have access).

Load the key in this shell
--------------------------

One reliable way is to load the private key into the local ``ssh-agent``::

    eval `ssh-agent -s`
    ssh-add path_to_ssh_key

You can also enable SSH agent forwarding, or add ``Host github.com`` /
``Host bitbucket.org`` entries in ``~/.ssh/config`` that point at the right
``IdentityFile``.

Confirm that Git can talk to the hosts you need::

    ssh -T git@github.com
    ssh -T git@bitbucket.org

Then continue with :doc:`installation`.
