#!/usr/bin/env bash
# Sobe o agente (servidor A2A + host MCP) na porta 7300.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .venv/bin/activate
exec python3 agente/agente.py
