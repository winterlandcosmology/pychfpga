Developing Web Content
======================

This document contains hints for developing web content on the IceBoard.

References
----------

Bootstrap CSS Framework
	* `Bootstrap CSS Framework <http://getbootstrap.com>`_

Angular.js JavaScript framework
	* `Angular.js JavaScript Framework <http://angularjs.org>`_
	* `Routing framework (our top-level organizational principle) <https://docs.angularjs.org/tutorial/step_07>`_

D3/Radian
	* `D3.js + Radian (for plots) <http://d3js.org>`_

tuber.js
	* `Source code on BitBucket <https://bitbucket.org/winterlandcosmology/icecore/src/HEAD/www/js/tuber.js>`_

NFS Mounting
------------

Because prototyping HTML/JS/CSS content benefits from constant, rapid
feedback, it is *strongly* recommended that you set up an NFS server on your
desktop and mount your web interface from on your IceBoard. Here are some
hints.

#. `sudo apt-get install nfs-kernel-server`
#. `sudo vi /etc/exports`::

   /home/gsmecher/winterland/iceboard/icecore/icecore/www *(ro,sync,no_subtree_check)
   /home/gsmecher/winterland/iceboard/icecore/www *(ro,sync,no_subtree_check)

#. `ssh root@iceboard004.local`::

   # mount 192.168.1.1:/home/gsmecher/winterland/iceboard/icecore/icecore/www /home/www -o tcp,nolock
   # mount 192.168.1.1:/home/gsmecher/winterland/iceboard/icecore/www # /home/www/Dfmux -o tcp,nolock

On the IceBoard, the "Base" IceBoard interface lives at `/home/www`, and the
Dfmux specializations live at `/home/www/Dfmux`. Because of how these trees
are organized in the icecore-dfmux git repository, the mount points look a
little strange -- they're correct.

.. vim: sts=3 ts=3 sw=3 tw=78 smarttab expandtab
