This folder contains the code used to perform hardware Quality Control (QC) on the ICE and CRS Hardware, including:

- ICE Hardware:
  - ICE Motherboard: IceBoard (Model MGK7MB) 
  - ICE ADC Mezzanine (Model MGADC08)
  - ICE 16-slot backplane (Model MGK7BP16)
  - ICE F-Engine pre-deployment Crate-level tests

- CRS Hardware:
    - CRS F-Engine pre-deployment Crate-level tests


This code is used on the Quality control test setup at the board manufacturer for acceptance tests, and is further used at McGill for in depth system tests.

The scripts use pytest to run the test. The McGll's wtl.pytest-xreport package and xreport plugin is used to provide a simple text-based menu interface, gather and aggregate the test data, and generate HTML and PDF reports.

The test equipment is used through the McGill's labpy instrument library, which is an extension of the standard pymeasure package.
