#!/bin/bash
#
# release.sh - Tag a version and create GitHub Release (triggers multi-platform build)
#
# Usage:
#   ./release.sh <version> [--yes]
#
# Example:
#   ./release.sh v1.3.0
#   ./release.sh v1.3.0 --yes
#
# After the tag is pushed, GitHub Actions builds Windows/macOS/Linux packages
# and attaches them to the Release automatically.
#

set -euo pipefail
cd "$(dirname "$0")"

GIT_NAME="${GIT_NAME:-kingkate}"
GIT_EMAIL="${GIT_EMAIL:-kingkate@users.noreply.github.com}"
git config user.name "$GIT_NAME" 2>/dev/null || true
git config user.email "$GIT_EMAIL" 2>/dev/null || true

if [ -z "${1:-}" ]; then
  echo "Please provide a version tag"
  echo "Usage: ./release.sh <version> [--yes]"
  echo "Example: ./release.sh v1.3.0"
  exit 1
fi

VERSION="$1"
YES="${2:-}"

if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  echo "Version should look like v1.3.0 (got: $VERSION)"
  exit 1
fi

if git rev-parse "$VERSION" >/dev/null 2>&1; then
  echo "Tag $VERSION already exists"
  exit 1
fi

# Keep VERSION file in sync (without leading v)
echo "${VERSION#v}" > VERSION

if [[ "$YES" != "--yes" && "$YES" != "-y" ]]; then
  echo "About to release: $VERSION"
  echo "  - update VERSION file"
  echo "  - create git tag + push"
  echo "  - GitHub Actions will build Win/macOS/Linux packages"
  read -r -p "Confirm? (y/n) " REPLY
  if [[ ! "$REPLY" =~ ^[Yy]$ ]]; then
    echo "Cancelled"
    exit 0
  fi
fi

# Commit VERSION if dirty
if ! git diff --quiet VERSION 2>/dev/null; then
  git add VERSION
  git commit -m "chore: bump version to ${VERSION#v}" || true
  git push origin HEAD || true
fi

echo "Creating tag $VERSION ..."
git tag -a "$VERSION" -m "Release $VERSION"
git push origin "$VERSION"

if command -v gh >/dev/null 2>&1; then
  echo "Creating GitHub Release (assets come from CI) ..."
  gh release create "$VERSION" \
    --title "$VERSION" \
    --generate-notes \
    --notes "Multi-platform packages (Windows / macOS / Linux) will appear here after CI finishes." \
    || true
else
  echo "gh CLI not found — tag pushed; create release on GitHub UI if needed."
fi

echo ""
echo "Release $VERSION started"
echo "CI builds: https://github.com/kingkate2009-droid/ai-switch/actions"
echo "Packages:  https://github.com/kingkate2009-droid/ai-switch/releases/tag/$VERSION"
