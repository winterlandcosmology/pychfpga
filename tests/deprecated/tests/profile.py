import cProfile
import sys
import runsnakerun.runsnake as runsnake
import tempfile
import os

def profile(cmd, filename=None):
    """ Profiles a python statement and shows the profile in a graphical way using the RunSnakeRun packege.
    """
    if filename is None:
        filename = tempfile.gettempdir() + os.sep + 'profile.dat'
        print 'Profile file will be saved in %s' % filename
        sys.stdout.flush()
    cProfile.run(cmd, filename)
    sys.argv = [__file__, filename]
    runsnake.main()
