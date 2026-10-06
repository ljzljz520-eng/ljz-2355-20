import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(__import__("os").path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import Env  # noqa


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
