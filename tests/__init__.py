"""The tests. Run them from the repo root with `python -m unittest`."""
import os

# No test may pop up a real desktop notification (notifier.send honours this).
os.environ.setdefault("GEEKMAGIC_NO_NOTIFY", "1")
