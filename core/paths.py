"""Filesystem locations — overridable via the DATA_DIR environment variable.

Locally, DATA_DIR is empty, so everything sits in the project folder exactly as
before. On a cloud host with a persistent volume, set DATA_DIR=/data so the
database, invoice PDFs and generated staff documents all live on the volume and
survive redeploys (the container's own disk is wiped on every deploy).

Shipped assets that travel with the code (e.g. document TEMPLATES) deliberately
do NOT use this — they stay next to the code.
"""
import os

DATA_DIR = os.environ.get("DATA_DIR", "")


def data_path(*parts: str) -> str:
    """Join a path under DATA_DIR. With DATA_DIR unset this returns the plain
    relative path (unchanged local behaviour)."""
    return os.path.join(DATA_DIR, *parts) if DATA_DIR else os.path.join(*parts)
