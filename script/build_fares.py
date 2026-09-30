#!/usr/bin/env python3
"""Build the exact OD tariff table from SNCF open data.

Sources (data.sncf.com, no auth):
  - tarifs-tgv-inoui-ouigo  (36k rows: UIC pairs, profiles, min/max)
  - tarifs-intercites       (2.5k rows: UIC8 pairs, min/max)

Keeps 2nd-class "Tarif Normal" (the reference full fare; OUIGO rows give
exact Ouigo prices too) and writes ``src/network/fares.json``:

  {"<UIC8o>|<UIC8d>": [min_cents, max_cents, basis], ...}

consumed by ExactTariffProvider (first in the fare chain). UICs join to
station names via the TER cache's station_uic at runtime. Run:
python3 script/build_fares.py
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

NETWORK_DIR = Path(__file__).resolve().parent.parent / "src" / "network"

DATASETS = {
    "tarifs-tgv-inoui-ouigo": {
        "ou": "gare_origine_code_uic",
        "du": "gare_destination_code_uic",
        "lo": "prix_minimum",
        "hi": "prix_maximum",
        "profile": "profil_tarifaire",
        "classe": "classe",
        "carrier": "transporteur",
    },
    "tarifs-intercites": {
        "ou": "origine_uic8",
        "du": "destination_uic8",
        "lo": "prix_min",
        "hi": "prix_max",
        "profile": "profil_tarifaire",
        "classe": "classe",
        "carrier": None,  # all Intercités
    },
}


def _carrier_tag(dataset: str, raw: str) -> str:
    raw = (raw or "").upper()
    if "OUIGO" in raw:
        return "OUIGO"
    if dataset == "tarifs-intercites" or "INTERCIT" in raw:
        return "INTERCITES"
    return "TGV"

WANT_PROFILE = "Tarif Normal"


def _fetch(dataset: str, where: str, select: str) -> list:
    base = f"https://data.sncf.com/api/explore/v2.1/catalog/datasets/{dataset}/exports/json"
    qs = urllib.parse.urlencode({"where": where, "select": select, "limit": -1})
    req = urllib.request.Request(
        f"{base}?{qs}", headers={"User-Agent": "sncf-max/fares-builder"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def main() -> int:
    table: dict = {}
    for dataset, f in DATASETS.items():
        print(f"fetching {dataset} ...")
        where = f"{f['classe']} = 2 AND {f['profile']} = '{WANT_PROFILE}'"
        fields = [f["ou"], f["du"], f["lo"], f["hi"]]
        if f["carrier"]:
            fields.append(f["carrier"])
        try:
            rows = _fetch(dataset, where, ",".join(fields))
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            continue
        print(f"  {len(rows)} rows")
        n = 0
        for r in rows:
            try:
                uo, ud = str(r[f["ou"]]).strip(), str(r[f["du"]]).strip()
                lo, hi = float(r[f["lo"]]), float(r[f["hi"]])
            except (KeyError, TypeError, ValueError):
                continue
            if len(uo) != 8 or len(ud) != 8 or not (uo.isdigit() and ud.isdigit()):
                continue
            key = f"{uo}|{ud}"
            tag = _carrier_tag(dataset, r.get(f["carrier"]) if f["carrier"] else "")
            prev = table.get(key)
            # Same pair can have both TGV and OUIGO rows: keep per carrier.
            entry = prev if isinstance(prev, dict) else {}
            cur = entry.get(tag)
            new = [int(round(lo * 100)), int(round(hi * 100))]
            if cur is None or tuple(new) < tuple(cur):
                entry[tag] = new
                table[key] = entry
                n += 1
        print(f"  kept {n} pair-carriers")
    path = NETWORK_DIR / "fares.json"
    path.write_text(json.dumps(table, separators=(",", ":")))
    print(f"wrote {path} ({path.stat().st_size / 1e6:.1f} MB, {len(table)} pairs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
