"""Identifies the code that draws the screens.

The device keeps the images it was sent, so after an update (new labels, colours, animations...) what's stored is out of
date; a changed id makes the app re-upload every view once.
"""

import hashlib

from geekmagic.paths import ROOT

_PACKAGE = ROOT / "geekmagic"


def compute() -> str:
    sources = sorted([*(_PACKAGE / "render").rglob("*.py"), *(_PACKAGE / "insights").glob("*.py")])
    return hashlib.sha1(b"".join(path.read_bytes() for path in sources)).hexdigest()[:12]


RENDER_ID = compute()
