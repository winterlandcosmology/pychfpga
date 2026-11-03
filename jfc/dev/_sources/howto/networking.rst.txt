Accessing remote servers via ssh
--------------------------------

`pychfpga` is typically run on remote servers that are located at the telescope sites.
Access to these machines is usually made through ``ssh``, which allows opening terminal sessions with encrypted communications.

You can sometimes use a username and passwords to authenticate your connection with the server, but that is not the preferred way. First, it can quickly becomes tedious to always have to enter the password (some SSH clients like MobaXterm on Windows can remember passwords for you, but that won't work for souble-ssh sessions). Second, user-selected passwords are typically weak and a robust SSH server will not allow passsword authentication at all.

The best alternative to authentication is to use SSH keys.

Use ``ssh-keygen`` to generate a pair of public and private keys. The super secret private key shall stay with you, on a trusted machine from which you access all other machines. The private key shall not be copied on other machines. Instead, you should use the key-forwarding feture of SSH.

To use the ssh key to their full convenience and secirity, you should use an ssh agent on you local, trusted machine, which will hold your private keys and will provide them on demand to any ssh clients when needed so they never have to be stored there. Also,  if you use agent-forwarding, all the ssh session and sub-sesssions (including git) that are started from another ssh session will have access to the agent that runs on your trusted machine. In other words, your private key files  never leave your trusted machine.

Many ssh clients software you use on you trusted work machine offer key management. On Windows, you can use Paegent to load and provide keys to Putty ssh sessions, or use MobaXterm which will also automatically load and offer your keys to any ssh sessions started with it. If you run from a linux machine,  you can start the agent with:

	eval `ssh-agent` # if not already running
	ssh-add ~/.ssh/myprivatekey # to add your key


If a server is not already set-up to recognize your key, you do:

	ssh-copy-id user@host

This will allow *all* the keys loaded in the ssh-agent (as listed by ssh-add -L) to authenticate on the target account. So if you use ``ssh-copy-id``, make sure you have loaded only the keys you want to propagate. And don't worry! Only the public key will actually be sent by ``ssh-copy-id``. The private key loaded in ssh-agent embeds an copy of the public key, and it is only the public part that will be sent over and appended to the target account's ``~/.ssh/authorized_keys``.

Then you can log directly to any client  with:

    ssh -A user@host

Repeat the ssh-copy-id step from there until you have  configured all the accounts on all the servers you use.

The `-A` indicated you want to forward (make available) your agent within your new ssh session. This means that other ssh sessions, ssh-copy-id  or even git will be able to use your keys automatically, even if you ar etwo or three ssh sessions deep.

If you use git on bitbucket, you will want to log in on their web site and install your public key so git will be able to authenticate to the server.

You can automate a double-ssh to get from the outside world:

ssh -A user@liberty ssh -A chime@carillon

te first -A allows the ssh to carillon to use the key from your trusted machine's agent, and the -A in the ssh to carillon ensures that git push and pulls on carillon will have their bitbucket keys from there as well.


