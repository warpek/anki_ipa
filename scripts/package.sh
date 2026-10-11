#!/usr/bin/env bash
# Build anki_ipa.ankiaddon at the repo root from the CONTENTS of src/anki_ipa
# (manifest.json and __init__.py at the zip root, as Anki expects).
# Excluded: __pycache__/, *.pyc, meta.json (the user's config, may hold an
# API key) and user_files/ except README.txt (no ipa_cache.json).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$root/src/anki_ipa"
out="$root/anki_ipa.ankiaddon"

command -v zip >/dev/null || { echo "error: 'zip' is not installed" >&2; exit 1; }
[[ -f "$src/manifest.json" && -f "$src/__init__.py" ]] || { echo "error: $src is not the add-on folder" >&2; exit 1; }

rm -f "$out"
cd "$src"
zip -q -r -X "$out" . \
    -x '__pycache__/*' '*/__pycache__/*' '*.pyc' '*.pyo' \
    -x 'meta.json' 'user_files/*' \
    -x '.*' '*/.*' '*~' '*.swp'
if [[ -f user_files/README.txt ]]; then
    zip -q -X "$out" user_files/README.txt
fi

# Safety net: refuse to ship anything that must not be in the package.
forbidden="$(unzip -Z1 "$out" | grep -P '(^|/)__pycache__/|\.py[co]$|(^|/)meta\.json$|^user_files/(?!README\.txt$)' || true)"
if [[ -n "$forbidden" ]]; then
    echo "error: forbidden file(s) in $out:" >&2
    echo "$forbidden" >&2
    rm -f "$out"
    exit 1
fi

echo "Built $out ($(du -h "$out" | cut -f1), $(stat -c %s "$out") bytes)"
unzip -l "$out"
