from pathlib import Path

import pytest

from app.mp3_validator import inspect_mp3


def test_inspect_rejects_non_mp3(tmp_path: Path):
    f = tmp_path / "x.mp3"
    f.write_bytes(b"this is not an mp3")
    with pytest.raises(ValueError):
        inspect_mp3(f)
