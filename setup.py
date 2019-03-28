#from distutils.core import setup, Extension
from setuptools import setup, find_packages, Extension
import os, sys
from os import path

# #from Cython.Build import cythonize
# from Cython.Distutils import build_ext

# import numpy as np

# Paths and filenames.
init_dir    = "/etc/init"
chime_dir   = "/etc/CHIME"
bin_dir     = "/usr/sbin"
target      = "ch_master"
version     = "version"
init_in     = "ch_master_daemon.conf"

# ext_chrx = Extension("chrx", 
#                    ["chrx/acq.c", "chrx/chrx.c", "chrx/disc.c", \
#                     "chrx/frame.c", "chrx/gpu_acq.c", "chrx/util.c"],
#                    include_dirs = ['/home/chime/anaconda2/include'],
#                    libraries = ["hdf5", "hdf5_hl", "m", "pthread"],
#                    library_dirs = ['/home/chime/anaconda2/lib'])


here = path.abspath(path.dirname(__file__))

# Get the version number without loading dependencies 
with open(path.join(here,'.','_version.py')) as f:
    for line in f:
        if line.startswith('__version__'):
            exec(line)
            break
    else:
      raise RuntimeError('Cannot find version number')

# Load the requirements from requirements.txt while removing the environment marks
with open(path.join(here, 'requirements.txt')) as f:
    requirements = [line.split(';')[0].rstrip() for line in f]


# Install the python packages.
setup(name = "ch_acq",
      version = __version__,
      description='Control and monitors a array of CHIME X-Engine',
      url='https://bitbucket.org/winterlandcosmology/kotekan-master',
      author='McGill University',
      author_email='jfcliche@jfcliche.com',
      packages=find_packages(),
      install_requires=requirements,
      include_package_data=True,
      entry_points = {'fm': ['fm=ch_master:main']},
#      cmdclass = {'build_ext': build_ext},
     )

# # If we are installing, copy things to system folders.
# if len(sys.argv) == 2 and sys.argv[1] == "install":
#   # Make sure the CHIME directory exists.
#   if not os.path.exists(chime_dir):
#     os.makedirs(chime_dir)

#   # Get the git tag number.
#   fd = os.popen("git describe --tags")
#   if not fd:
#     print "Could not run \"git describe --tags\"."
#   else:
#     tag = fd.read().replace("\n", "")
#     fd.close()

#   # Record tag.
#   print "Recording git tag \"%s\" to %s/%s." % (tag, chime_dir, version)
#   path = "%s/%s" % (chime_dir, version)
#   fp = open(path, "w")
#   if not fp:
#     print "Could not write to file %s." % (path)
#   else:
#     fp.write("%s\n" % tag)
#     fp.close()
  
#   # Copy the configuration files to the CHIME directory.
#   print "Copying %s.conf and %s.spec to %s." % (target, target, chime_dir)
#   os.system("install -m 644 %s.conf %s.spec %s" % (target, target, chime_dir))
  
#   # Copy the init script to the init directory.
#   print "Copying the init script to %s." % (init_dir)
#   os.system("install -m 644 %s %s/%s.conf" % (init_in, init_dir, target))
  
#   # Copy ch_master to the path.
#   print "Copying %s.py to %s." % (target, bin_dir)
#   os.system("install -m 755 %s.py %s" % (target, bin_dir))
  
#   # Copy impact source to the CHIME directory.
# #print "Copying %s to %s." % (impact, chime_dir)
#   os.system("install -m 755 %s.py %s" % (target, bin_dir))
