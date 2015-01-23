#from distutils.core import setup, Extension
from setuptools import setup, Extension
import os, sys
#from Cython.Build import cythonize
from Cython.Distutils import build_ext

import numpy as np

# Paths and filenames.
init_dir    = "/etc/init"
chime_dir   = "/etc/CHIME"
bin_dir     = "/usr/sbin"
target      = "ch_master"
version     = "version"
init_in     = "ch_master_daemon.conf"

ext_chrx = Extension("chrx", 
                   ["chrx/acq.c", "chrx/chrx.c", "chrx/disc.c", \
                    "chrx/frame.c", "chrx/gpu_acq.c", "chrx/util.c"],
                   include_dirs = ['/opt/anaconda/include'],
                   libraries = ["hdf5", "hdf5_hl", "m", "pthread"],
                   library_dirs = ['/opt/anaconda/lib'])


#ext_post_trans = Extension("post_acq.transpose",
#                     ["post_acq/transpose.pyx", "post_acq/ctranspose.c"],
#                     libraries = ["gomp"],
#                     include_dirs=[np.get_include(), '/opt/anaconda/include' ],
#                     # '-Wa,-q' is max specific and only there because
#                     # soemthing is wrong with my gcc. It switches to the
#                     # clang assembler.
#                     ##extra_compile_args=['-fopenmp', '-O3', '-march=native',
#                     ##'-Wa,-q', '-std=c99'],
#                     extra_compile_args=['-fopenmp', '-march=native', '-std=c99'],
#                     library_dirs = ['/opt/anaconda/lib'],
#                     )

#ext_post_trunc = Extension("post_acq.truncate",
#                     ["post_acq/truncate.pyx"],
#                     include_dirs=[np.get_include(), '/opt/anaconda/include'],
#                     library_dirs = ['/opt/anaconda/lib'],
#                     )

# Install the python packages.
setup(name = "ch_acq",
      version = "1.0",
      packages = ["pychfpga", "pychfpga.core", "pychfpga.common", \
                  "pychfpga.icecore", "pychfpga.MGADC08", \
                  "post_acq"
                  ],
      ext_modules = [ext_chrx], #, ext_post_trunc, ext_post_trans],
      cmdclass = {'build_ext': build_ext},
     )

# If we are installing, copy things to system folders.
if len(sys.argv) == 2 and sys.argv[1] == "install":
  # Make sure the CHIME directory exists.
  if not os.path.exists(chime_dir):
    os.makedirs(chime_dir)

  # Get the git tag number.
  fd = os.popen("git describe --tags")
  if not fd:
    print "Could not run \"git describe --tags\"."
  else:
    tag = fd.read().replace("\n", "")
    fd.close()

  # Record tag.
  print "Recording git tag \"%s\" to %s/%s." % (tag, chime_dir, version)
  path = "%s/%s" % (chime_dir, version)
  fp = open(path, "w")
  if not fp:
    print "Could not write to file %s." % (path)
  else:
    fp.write("%s\n" % tag)
    fp.close()
  
  # Copy the configuration files to the CHIME directory.
  print "Copying %s.conf and %s.spec to %s." % (target, target, chime_dir)
  os.system("install -m 644 %s.conf %s.spec %s" % (target, target, chime_dir))
  
  # Copy the init script to the init directory.
  print "Copying the init script to %s." % (init_dir)
  os.system("install -m 644 %s %s/%s.conf" % (init_in, init_dir, target))
  
  # Copy ch_master to the path.
  print "Copying %s.py to %s." % (target, bin_dir)
  os.system("install -m 755 %s.py %s" % (target, bin_dir))
  
  # Copy impact source to the CHIME directory.
#print "Copying %s to %s." % (impact, chime_dir)
  os.system("install -m 755 %s.py %s" % (target, bin_dir))
