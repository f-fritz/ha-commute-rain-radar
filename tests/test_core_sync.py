import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from sync_core import DST, synced_files  # noqa: E402


def test_integration_core_is_in_sync():
    expected = synced_files()
    actual = {p.name: p.read_text() for p in sorted(DST.glob("*.py"))}
    assert actual == expected, "run `python scripts/sync_core.py`"
