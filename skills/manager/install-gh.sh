#!/usr/bin/env bash
#
# install-gh.sh — install the GitHub CLI with NO sudo and NO package manager:
# downloads the official release binary for this OS/arch into ~/.local/bin
# (override with GH_INSTALL_DIR) and wires up PATH for future shells.
#
# Used by install.md's prerequisite fix flow when brew/apt are unavailable,
# and any time a user wants to switch from local mode to PR mode later.
# Idempotent: exits 0 immediately if gh is already on PATH (GH_FORCE=1 to
# reinstall anyway).
#
# After it succeeds, the ONE manual step left is:   gh auth login
set -euo pipefail

DEST="${GH_INSTALL_DIR:-$HOME/.local}"
BIN="$DEST/bin"

if [ -z "${GH_FORCE:-}" ] && command -v gh >/dev/null 2>&1; then
  echo "gh already installed: $(command -v gh) · $(gh --version | head -1)"
  exit 0
fi

VER="$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest \
       | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -1)"
if [ -z "$VER" ]; then
  echo "ERROR: could not determine the latest gh version (is github.com reachable?)" >&2
  exit 1
fi

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)              ASSET="gh_${VER}_macOS_arm64.zip" ;;
  Darwin-x86_64)             ASSET="gh_${VER}_macOS_amd64.zip" ;;
  Linux-x86_64)              ASSET="gh_${VER}_linux_amd64.tar.gz" ;;
  Linux-aarch64|Linux-arm64) ASSET="gh_${VER}_linux_arm64.tar.gz" ;;
  *) echo "ERROR: unsupported platform: $(uname -s) $(uname -m) — install manually from https://cli.github.com" >&2; exit 1 ;;
esac

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "==> downloading gh v${VER} (${ASSET}) — official release binary, no sudo needed"
curl -fsSL -o "$TMP/$ASSET" "https://github.com/cli/cli/releases/download/v${VER}/${ASSET}"
cd "$TMP"
case "$ASSET" in
  *.zip) unzip -q "$ASSET" ;;
  *)     tar xzf "$ASSET" ;;
esac

mkdir -p "$BIN"
cp gh_*/bin/gh "$BIN/gh"
chmod +x "$BIN/gh"
echo "==> installed: $BIN/gh · $("$BIN/gh" --version | head -1)"

# PATH wiring: persist for future shells; print the export for THIS one.
case ":$PATH:" in
  *":$BIN:"*) ;;  # already on PATH
  *)
    SHELL_RC="$HOME/.zshrc"
    case "${SHELL:-}" in */bash) SHELL_RC="$HOME/.bashrc" ;; esac
    if ! grep -qs "$BIN" "$SHELL_RC" 2>/dev/null && ! grep -qs '\$HOME/.local/bin' "$SHELL_RC" 2>/dev/null; then
      printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$SHELL_RC"
      echo "==> added $BIN to PATH in $SHELL_RC (takes effect in new shells)"
    fi
    echo "==> for THIS shell, run:   export PATH=\"$BIN:\$PATH\""
    ;;
esac

echo "==> done. Last step (interactive, ~30s):   gh auth login"
