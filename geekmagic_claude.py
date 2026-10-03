#!/usr/bin/env python3
"""Push Claude Code's or Codex's /usage limits to a GeekMagic SmallTV (stock firmware), once or on a loop.

    python geekmagic_claude.py --ip 192.168.1.18
    python geekmagic_claude.py --ip 192.168.1.18 --loop 60 --provider codex

The code lives in the `geekmagic` package; this is just the command-line launcher (`python tray.py` is the tray app).
"""

from geekmagic.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
