"""Guards `ops import` against Zip-Slip: a tarball with a path-traversal member
must not write outside the target dir (uses tarfile filter='data', PEP 706)."""
from __future__ import annotations

import io
import tarfile

import pytest


def _malicious_tar(path):
    """A tar whose member escapes the extraction dir via `../`."""
    buf = io.BytesIO(b"pwned")
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(name="../../escaped.txt")
        info.size = len(buf.getvalue())
        tf.addfile(info, io.BytesIO(buf.getvalue()))


def test_traversal_member_is_blocked(tmp_path):
    tar = tmp_path / "evil.tar.gz"
    _malicious_tar(tar)
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(tar, "r:gz") as tf:
        # The same call `ops import` makes — the data filter must refuse it.
        with pytest.raises(tarfile.OutsideDestinationError):
            tf.extractall(dest, filter="data")
    # Nothing escaped to the parent.
    assert not (tmp_path / "escaped.txt").exists()


def test_benign_tar_still_extracts(tmp_path):
    tar = tmp_path / "ok.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        data = b"hello"
        info = tarfile.TarInfo(name="data/ops.db")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(tar, "r:gz") as tf:
        tf.extractall(dest, filter="data")
    assert (dest / "data" / "ops.db").read_bytes() == b"hello"
