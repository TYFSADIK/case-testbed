# Offline runs

`tools/offline.py` runs the same engine and detector in one process (no
network), for tuning and as a regression check. Exercise 1 was used for
tuning, with four traffic/noise seeds; exercise 2 was written afterwards and
held out. `exercise-2-heldout-before-area-wide-rule.json` is its first run,
before the one change made after seeing it (docs/04-findings.md).
