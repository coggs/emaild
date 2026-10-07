#!/usr/bin/env bash
# Put the `emaild` command on your PATH (WSL). Re-run any time.
# Writes a tiny wrapper in ~/.local/bin (files on /mnt/d can't reliably be made executable from WSL).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/emaild" <<WRAP
#!/usr/bin/env bash
EMAILD_REPO="$REPO" exec bash "$REPO/scripts/emaild" "\$@"
WRAP
chmod +x "$HOME/.local/bin/emaild"
echo "Installed: $HOME/.local/bin/emaild -> $REPO/scripts/emaild"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo 'Add to your ~/.bashrc:  export PATH="$HOME/.local/bin:$PATH"' ;; esac
echo "Try: emaild help"
