#!/usr/bin/env python3
"""Report audit-agent progress by reading the filesystem (the ground truth).

Agent status summaries have lied before, so this never trusts them.
Usage: python3 scripts/audit_status.py [expected_max]
"""
import pathlib
import re
import sys

AUDITS = pathlib.Path(__file__).resolve().parent.parent / "docs" / "audits"
BRIEF = "BRIEF.md"

rows = []
for f in sorted(AUDITS.glob("*.md")):
    if f.name == BRIEF:
        continue
    m = re.match(r"^(\d{3})-", f.name)
    rows.append((m.group(1) if m else "---", f.stat().st_size, f.name))

rows.sort()
landed = len(rows)
total_bytes = sum(r[1] for r in rows)
print(f"LANDED {landed} reports / {total_bytes/1024:.0f} KB\n")

# flag suspiciously small reports (likely stub/failed)
THIN = 2500
thin = [r for r in rows if r[1] < THIN]
if thin:
    print(f"!! {len(thin)} suspiciously thin (<{THIN}B), treat as suspect:")
    for n, sz, name in thin:
        print(f"   {n}  {sz:>6}B  {name}")

present = {r[0] for r in rows}
gaps = []
if len(sys.argv) > 1:
    hi = int(sys.argv[1])
    for i in range(1, hi + 1):
        n = f"{i:03d}"
        if n not in present:
            gaps.append(n)

print()
if gaps:
    print(f"not yet landed ({len(gaps)}): {' '.join(gaps)}")
else:
    print("no gaps in requested range")

# sanity: every file should start with a markdown heading
print("\nintegrity check (first line of each report):")
bad = []
for _, _, name in rows:
    first = (AUDITS / name).read_text(encoding="utf-8", errors="replace")[:80].lstrip()
    if not first.startswith("#"):
        bad.append(name)
print("  all reports begin with a heading" if not bad else f"  !! malformed: {bad}")