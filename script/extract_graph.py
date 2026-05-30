#!/usr/bin/env python3
"""Extract TGV network topology from SNCF NETEX data.

Line-by-line scanning of 544 MB NETEX XML to find station connections
embedded as <!-- comments --> before FromStopPointRef / ToStopPointRef.

Usage:  python3 script/extract_graph.py
"""

import json
import zipfile
import re
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
ZIP_PATH = ROOT / "export-opendata-sncf-netex.zip"
OUT_PATH = ROOT / "src" / "network" / "tgv_graph.json"


def extract() -> dict[str, list[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    count = 0
    prev_comment: str | None = None
    from_station: str | None = None

    # Match comment and FromStopPoint/ToStopPoint
    comment_re = re.compile(rb"<!--\s*(.*?)\s*-->")
    from_re = re.compile(rb"<FromStopPointRef")
    to_re = re.compile(rb"<ToStopPointRef")
    jp_end_re = re.compile(rb"</JourneyPart>")

    with zipfile.ZipFile(ZIP_PATH) as zf:
        names = [n for n in zf.namelist() if n.endswith(".xml")]
        assert names, "No XML in zip"
        print(f"Reading {names[0]} ...")

        with zf.open(names[0]) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue

                # Comment line: capture station name
                cm = comment_re.match(line)
                if cm:
                    raw = cm.group(1).decode("utf-8", errors="replace").strip()
                    # Strip leading dash prefix that NETEX uses: "- Station Name"
                    if raw.startswith("- "):
                        raw = raw[2:]
                    elif raw.startswith("-"):
                        raw = raw[1:]
                    prev_comment = raw.strip()
                    continue

                # FromStopPointRef
                if from_re.search(line):
                    if prev_comment:
                        from_station = prev_comment
                    prev_comment = None
                    continue

                # ToStopPointRef
                if to_re.search(line):
                    to_station = prev_comment if prev_comment else None
                    if from_station and to_station and from_station != to_station:
                        graph[from_station].add(to_station)
                        count += 1
                        if count % 500000 == 0:
                            print(f"  {count} edges, {len(graph)} stations ...")
                    # Reset for next JourneyPart
                    prev_comment = None
                    continue

                # JourneyPart end: reset
                if jp_end_re.search(line):
                    prev_comment = None
                    from_station = None

    print(f"\nDone: {count} edges, {len(graph)} stations")
    return {k: sorted(v) for k, v in graph.items()}


def main():
    graph = extract()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(graph, f, ensure_ascii=False)

    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"Graph written to {OUT_PATH} ({size_kb:.0f} KB)")

    sample = sorted(graph.items(), key=lambda x: -len(x[1]))[:15]
    print(f"\nTop {len(sample)} most-connected:")
    for name, neighbors in sample:
        print(f"  {name}: {len(neighbors)}")


if __name__ == "__main__":
    main()
