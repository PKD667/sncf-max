#!/usr/bin/env bash
# Daily refresh of the TER and coach timetables used by the site.
#
#   refresh_timetables.sh OUT_DIR
#
# Builds both caches into OUT_DIR (atomic writes; each builder refuses to write
# an empty or short-window result, so a failed download keeps yesterday's file).
# The site prefers the newer of its packaged copy and OUT_DIR/*.json, by the
# "built" date, and reloads on change, so no restart is needed.
#
# Exit status is non-zero if either build failed. Nothing is written in that
# case, and the error goes to stderr for the journal.
set -euo pipefail

out="${1:?usage: refresh_timetables.sh OUT_DIR}"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$out"

status=0
for b in build_ter.py build_bus.py; do
  if ! python3 "$here/$b" --out "$out"; then
    echo "refresh: $b FAILED; keeping the previous $out copy" >&2
    status=1
  fi
done
exit "$status"
