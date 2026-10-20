#!/usr/bin/env bash
# Instala o hogs em ~/.local/share/hogs e cria o comando ~/.local/bin/hogs.
#   curl -fsSL https://raw.githubusercontent.com/willlobato/hogs/main/install.sh | bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/willlobato/hogs/main"
APP_DIR="$HOME/.local/share/hogs"
BIN_DIR="$HOME/.local/bin"

say()  { printf '\033[1;35m🐷 %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "o hogs foi feito pro macOS."
command -v python3 >/dev/null || fail "python3 não encontrado. Instale com: xcode-select --install"

PY="$(python3 -c 'import sys; print(sys.executable)')"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail "precisa de Python 3.9 ou mais novo."

say "Baixando o hogs…"
mkdir -p "$APP_DIR" "$BIN_DIR"
curl -fsSL "$REPO_RAW/hogs.py" -o "$APP_DIR/hogs.py"

if ! "$PY" -c 'import rich' 2>/dev/null; then
  say "Instalando a biblioteca rich…"
  "$PY" -m pip install --user --quiet rich 2>/dev/null \
    || "$PY" -m pip install --user --quiet --break-system-packages rich \
    || fail "não consegui instalar o rich. Rode: $PY -m pip install --user rich"
fi

# Wrapper com o caminho absoluto do Python que tem o rich instalado.
cat > "$BIN_DIR/hogs" <<EOF
#!/bin/sh
exec "$PY" "$APP_DIR/hogs.py" "\$@"
EOF
chmod +x "$BIN_DIR/hogs"

say "Pronto! $("$BIN_DIR/hogs" --version) instalado em $BIN_DIR/hogs"
case ":$PATH:" in
  *":$BIN_DIR:"*) say "Rode: hogs" ;;
  *) printf '\n%s não está no seu PATH. Adicione ao ~/.zshrc:\n\n  export PATH="$HOME/.local/bin:$PATH"\n\n' "$BIN_DIR" ;;
esac
