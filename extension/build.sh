#!/bin/sh
# Build the Claude Desktop extension: dist/switchboard-claude-<version>.mcpb
# Copies src/switchboard_agent into the bundle (so the bundle is exactly this version), locks `cryptography`,
# validates the manifest with Anthropic's mcpb CLI and packs it. Needs uv and npx.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(dirname "$here")
ver=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$repo/src/switchboard_agent/__init__.py")
for f in "$here/claude-desktop/manifest.json" "$here/claude-desktop/pyproject.toml"; do
  grep -q "\"$ver\"" "$f" || { echo "version $ver missing in $f" >&2; exit 1; }
done
work=$(mktemp -d "${TMPDIR:-/tmp}/sb-mcpb.XXXXXX")
trap 'rm -rf "$work"' EXIT
cp -R "$here/claude-desktop/." "$work/"
rm -rf "$work/src/switchboard_agent"
cp -R "$repo/src/switchboard_agent" "$work/src/switchboard_agent"
cp "$repo/LICENSE" "$repo/NOTICE" "$work/"
find "$work" -name __pycache__ -type d -prune -exec rm -rf {} +
(cd "$work" && uv lock --quiet)
npx -y @anthropic-ai/mcpb@2.1.2 validate "$work/manifest.json"
mkdir -p "$repo/dist"
out="$repo/dist/switchboard-claude-$ver.mcpb"
rm -f "$out"
npx -y @anthropic-ai/mcpb@2.1.2 pack "$work" "$out"
cp "$work/uv.lock" "$here/claude-desktop/uv.lock"
shasum -a 256 "$out"
