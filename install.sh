#!/usr/bin/env sh
# Install the `token-stats` CLI launcher into ~/.local/bin (standard XDG user bin dir).
set -eu

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
TARGET="$BIN_DIR/token-stats"

mkdir -p "$BIN_DIR"

cat > "$TARGET" <<EOF
#!/usr/bin/env sh
exec python3 "$REPO_DIR/token_stats.py" "\$@"
EOF

chmod +x "$TARGET" "$REPO_DIR/token_stats.py"
printf 'Installed %s -> %s/token_stats.py\n' "$TARGET" "$REPO_DIR"
