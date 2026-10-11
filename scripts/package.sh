#!/usr/bin/env bash
# Build anki_ipa.ankiaddon at the repo root from the CONTENTS of src/anki_ipa
# (manifest.json and __init__.py at the zip root, as Anki expects), plus
# LICENSE (as LICENSE.txt) and NOTICE.txt from the repo root.
#
# Excluded: __pycache__/, *.pyc, meta.json (the user's config, may hold an
# API key), dotfiles and user_files/ except README.txt (no ipa_cache.json).
#
# Usage: scripts/package.sh [--dirty]
#   Refuses to build when git reports modified or untracked files in
#   src/anki_ipa, LICENSE or NOTICE.txt (a release must match a commit);
#   --dirty builds anyway (for local testing).
#
# Portable: bash 3.2+, BSD or GNU userland (macOS, Linux), Info-ZIP zip/unzip.
set -euo pipefail

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

dirty=0
for arg in "$@"; do
    case "$arg" in
        --dirty) dirty=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$root/src/anki_ipa"
out="$root/anki_ipa.ankiaddon"
shipped=(src/anki_ipa LICENSE NOTICE.txt)

command -v zip >/dev/null || { echo "error: 'zip' is not installed" >&2; exit 1; }
command -v unzip >/dev/null || { echo "error: 'unzip' is not installed" >&2; exit 1; }
[[ -f "$src/manifest.json" && -f "$src/__init__.py" ]] || { echo "error: $src is not the add-on folder" >&2; exit 1; }
for f in LICENSE NOTICE.txt; do
    [[ -f "$root/$f" ]] || { echo "error: $root/$f is missing" >&2; exit 1; }
done

# Clean-tree check: ignored files (cache, meta.json, __pycache__) don't count;
# they are excluded below anyway.
if [[ $dirty -eq 0 ]]; then
    if ! command -v git >/dev/null || ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "error: not a git checkout (or git missing), can't check that the files are committed." >&2
        echo "       Pass --dirty to build anyway." >&2
        exit 1
    fi
    changes="$(git -C "$root" status --porcelain --untracked-files=all -- "${shipped[@]}")"
    if [[ -n "$changes" ]]; then
        echo "error: refusing to build, these shipped files are modified or untracked:" >&2
        echo "$changes" >&2
        echo "       Commit them first (a release should match a commit), or pass --dirty to build anyway." >&2
        exit 1
    fi
fi

stage="$(mktemp -d "${TMPDIR:-/tmp}/anki_ipa_pkg.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
cp "$root/LICENSE" "$stage/LICENSE.txt"
cp "$root/NOTICE.txt" "$stage/NOTICE.txt"

rm -f "$out"
cd "$src"
zip -q -r -X "$out" . \
    -x '__pycache__/*' '*/__pycache__/*' '*.pyc' '*.pyo' \
    -x 'meta.json' 'user_files/*' \
    -x '.*' '*/.*' '*~' '*.swp' '*.tmp'
if [[ -f user_files/README.txt ]]; then
    zip -q -X "$out" user_files/README.txt
fi
(cd "$stage" && zip -q -X "$out" LICENSE.txt NOTICE.txt)

fail() {
    echo "error: $1 in $out:" >&2
    echo "$2" >&2
    rm -f "$out"
    exit 1
}

# Safety net: refuse to ship anything that must not be in the package
# (POSIX ERE only; no grep -P on macOS).
listing="$(unzip -Z1 "$out")"
forbidden="$(grep -E '(^|/)__pycache__/|\.py[co]$|(^|/)meta\.json$|^user_files/|(^|/)\.|\.tmp$|~$' <<<"$listing" \
    | grep -v -x 'user_files/README\.txt' || true)"
[[ -z "$forbidden" ]] || fail "forbidden file(s)" "$forbidden"

missing=""
for f in manifest.json __init__.py config.json LICENSE.txt NOTICE.txt user_files/README.txt; do
    grep -q -x -F "$f" <<<"$listing" || missing="$missing$f"$'\n'
done
[[ -z "$missing" ]] || fail "required file(s) missing" "$missing"

bytes="$(wc -c <"$out" | tr -d ' ')"
echo "Built $out ($(du -h "$out" | cut -f1), $bytes bytes)"
unzip -l "$out"
