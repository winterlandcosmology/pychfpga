This folder contains the code used to perform hardware Quality Control (QC) on the Ice Hardware, including:

- Motherboard: IceBoard (Model MGK7MB) 
- CHIME ADC Mezzanine (Model MGADC08)
- 16-slot backplane (Model MGK7BP16)
- Single-crate F-Engine for pre-deployment


This code is used on the Quality control test setup at the board manufacturer for acceptance tests, and is further used at McGill for in depth system tests.

The scripts use pytest to run the test. The McGll's wtl.pytest-xreport package and xreport plugin is used to provide a simple text-based menu interface, gather and aggregate the test data, and generate HTML and PDF reports.

The test equipment is used through the McGill's labpy instrument library, which is an extension of the standard pymeasure package.
