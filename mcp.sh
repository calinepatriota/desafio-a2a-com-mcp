#!/usr/bin/env bash
# Sobe o servidor MCP (Streamable HTTP) na porta 7301. Deixe o stderr visivel.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .venv/bin/activate
: "${REQUEST_STATE_SECRET:?defina REQUEST_STATE_SECRET; veja o README (secao Como rodar)}"
exec python3 servidor-mcp/servidor.py
