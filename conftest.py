"""Root conftest for odibi_anchor.

NOTE: On Databricks /Workspace FUSE mounts, Python cannot create __pycache__
directories. Always invoke pytest with PYTHONDONTWRITEBYTECODE=1:

    PYTHONDONTWRITEBYTECODE=1 python -m pytest

Or use the convenience script:

    ./run_tests.sh
"""
