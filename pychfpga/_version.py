# Try to get the version number from git first for in the case where we are working out of
# an editable install (-e), in which case the version file might be stale unless we manally
# reinstall periodically. If we are not in a git repo, which is the case when the installer moved the file in the package folder,
# use the version that is saved in the version file (_version_scm.py) during install.
try:
    import setuptools_scm
    __version__ = setuptools_scm.get_version(root='..', relative_to=__file__)
except (ImportError, LookupError):
    try:
        from ._version_scm  import version as __version__
    except (ImportError):
        raise RuntimeError('Cannot determine version')
