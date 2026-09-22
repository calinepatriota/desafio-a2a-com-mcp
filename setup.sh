#!/usr/bin/env bash
# Prepara o ambiente a partir de um clone limpo: cria a venv e instala as
# dependencias travadas (SDK oficial do MCP v2, revisao 2026-07-28 da spec).
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt
echo
echo "Pronto. Agora gere e exporte o segredo do requestState (uma vez):"
echo '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
