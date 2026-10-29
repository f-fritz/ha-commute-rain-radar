"""Copy the core package into the Home Assistant integration.

Home Assistant can't install the local `commute_rain` package, so the
integration ships a copy in `custom_components/commute_rain/core/`.
`tests/test_core_sync.py` fails if the copy is out of date.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "commute_rain"
DST = ROOT / "custom_components" / "commute_rain" / "core"
HEADER = "# Copied from commute_rain/ by scripts/sync_core.py. Do not edit here.\n"


def synced_files() -> dict[str, str]:
    return {p.name: HEADER + p.read_text() for p in sorted(SRC.glob("*.py"))}


def main() -> None:
    if DST.exists():
        shutil.rmtree(DST)
    DST.mkdir(parents=True)
    for name, text in synced_files().items():
        (DST / name).write_text(text)
    print(f"synced {len(synced_files())} files to {DST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
