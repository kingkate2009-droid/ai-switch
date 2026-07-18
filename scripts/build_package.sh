#!/usr/bin/env bash
# Build single-file AI Switch binary for the current platform.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$(tr -d '[:space:]' < VERSION 2>/dev/null || echo "0.0.0")"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
esac
case "$OS" in
  darwin) OS="macos" ;;
  mingw*|msys*|cygwin*) OS="windows" ;;
esac

echo "==> Building AI Switch v${VERSION} for ${OS}-${ARCH}"

python3 -m pip install -q -r requirements.txt "pyinstaller>=6.0"

# Clean previous
rm -rf build dist packaging/dist

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "dist" \
  --workpath "build/pyinstaller" \
  packaging/ai-switch.spec

OUT_DIR="dist/packages"
mkdir -p "$OUT_DIR"

BIN="dist/ai-switch"
if [[ "$OS" == "windows" ]]; then
  BIN="dist/ai-switch.exe"
fi

if [[ ! -f "$BIN" ]]; then
  # onedir fallback name
  if [[ -f "dist/ai-switch/ai-switch" ]]; then
    BIN="dist/ai-switch/ai-switch"
  elif [[ -f "dist/ai-switch/ai-switch.exe" ]]; then
    BIN="dist/ai-switch/ai-switch.exe"
  else
    echo "Binary not found under dist/"
    ls -la dist || true
    exit 1
  fi
fi

ASSET="ai-switch-${VERSION}-${OS}-${ARCH}"
STAGE="$OUT_DIR/$ASSET"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp "$BIN" "$STAGE/"
cp README.md LICENSE VERSION "$STAGE/" 2>/dev/null || true
# launcher helpers
if [[ "$OS" == "windows" ]]; then
  cat > "$STAGE/start.bat" << 'BAT'
@echo off
cd /d %~dp0
ai-switch.exe
BAT
else
  cat > "$STAGE/start.sh" << 'SH'
#!/usr/bin/env bash
cd "$(dirname "$0")"
exec ./ai-switch
SH
  chmod +x "$STAGE/start.sh" "$STAGE/ai-switch" 2>/dev/null || true
fi

ARCHIVE="$OUT_DIR/${ASSET}.tar.gz"
if [[ "$OS" == "windows" ]]; then
  ARCHIVE="$OUT_DIR/${ASSET}.zip"
  (cd "$OUT_DIR" && zip -qr "${ASSET}.zip" "$ASSET")
else
  tar -C "$OUT_DIR" -czf "$ARCHIVE" "$ASSET"
fi

echo ""
echo "✅ Package ready: $ARCHIVE"
ls -lh "$ARCHIVE"
