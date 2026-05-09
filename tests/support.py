"""Shared test helpers."""

import tempfile
from pathlib import Path

from bot import config


def isolate_data_dir(testcase):
    """Run a test with an empty temporary DATA_DIR."""
    previous = config.DATA_DIR
    tmp = tempfile.TemporaryDirectory()
    config.DATA_DIR = Path(tmp.name)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    def cleanup():
        config.DATA_DIR = previous
        tmp.cleanup()

    testcase.addCleanup(cleanup)
    return config.DATA_DIR
