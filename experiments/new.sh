#!/bin/sh
# Start an experiment: ./experiments/new.sh some-idea
set -eu

[ $# -eq 1 ] || { echo "usage: $0 <name>" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
dest="$here/$(date +%F)-$1"
[ -e "$dest" ] && { echo "$dest already exists" >&2; exit 1; }

cp -R "$here/_template" "$dest"
sed -i '' "s/{{name}}/$1/" "$dest/notes.md"
echo "$dest"
