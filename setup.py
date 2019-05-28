from setuptools import setup, find_packages
from os import path



here = path.abspath(path.dirname(__file__))

# Get the version number without loading dependencies (i.e. `from pychfpga
# import _version` would execute __init__.py)
with open(path.join(here, 'pychfpga', '_version.py')) as f:
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
setup(name = "pychfpga",
      version = __version__,
      description=('Control and monitors a array of ICEBoard '
                   'running the chfpga (X-Engine + corner-turn) '
                   'or sifpga (F-Engine + 16-channel X-Engine) FGPA firmware'),
      url='https://bitbucket.org/winterlandcosmology/pychfpga',
      author='McGill University',
      author_email='jfcliche@jfcliche.com',
      packages=find_packages(),
      install_requires=requirements,
      include_package_data=True,
      entry_points = {'fm': ['fm=fpga_master:main']},
#      cmdclass = {'build_ext': build_ext},
     )
