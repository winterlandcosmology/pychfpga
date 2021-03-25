from setuptools import setup, find_packages
from os import path

import versioneer


here = path.abspath(path.dirname(__file__))

# Load the requirements from requirements.txt while removing the environment marks
with open(path.join(here, 'requirements.txt')) as f:
    requirements = [line.split(';')[0].rstrip() for line in f]

# Load the long description from the README file
with open("README", "r", encoding='utf-8') as f:
    long_description = f.read()

# Install the python packages.
setup(
      name="pychfpga",
      version=versioneer.get_version(),
      cmdclass=versioneer.get_cmdclass(),
      # cmdclass = {'build_ext': build_ext},
      description=('Control and monitors a array of ICEBoard '
                   'running the chfpga (X-Engine + corner-turn) '
                   'or sifpga (F-Engine + 16-channel X-Engine) FPGA firmware'),
      long_description=long_description,
      long_description_content_type="text/x-rst",
      url='https://bitbucket.org/winterlandcosmology/pychfpga',
      author='McGill University',
      author_email='jfcliche@jfcliche.com',
      packages=find_packages(),
      classifiers=[
              "Programming Language :: Python :: 3",
              "Operating System :: OS Independent",
              "Topic :: Scientific/Engineering :: Astronomy",
              "Intended Audience :: Science/Research",
          ],
      python_requires='>=3.7',
      install_requires=requirements,
      include_package_data=True,
      entry_points={
          "console_scripts": [
              "fpga_master=pychfpga.fpga_master:main",
              "chime_gps=pychfpga.gps:main",
              "chime_raw_acq=pychfpga.raw_acq:main",
              "chime_rx_ps=pychfpga.ps:main",
          ]
      },
     )
