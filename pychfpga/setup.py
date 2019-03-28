from os import path
from setuptools import setup, find_packages

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

setup(
    name='pychfpga',
    version=__version__, #wtl.metrics.__version__,
    description='Handle arrays of ICE Hardware',
    url='https://bitbucket.org/chime/pychfpga',
    author='McGill University',
    author_email='jfcliche@jfcliche.com',
    packages=find_packages(),
    install_requires=requirements,
)
