#!/bin/zsh
# Launch locally. API keys are read without echo and never written to a file.
set -euo pipefail
cd "${0:A:h:h}"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements-ais.txt
if [[ -z "${AISSTREAM_API_KEY:-}" ]]; then
  read -rs "AISSTREAM_API_KEY?AISStream API key (hidden; press Enter after pasting): "
  printf '\n'
fi
if [[ -z "${AISSTREAM_API_KEY:-}" ]]; then
  print -u2 'No key entered. Run python3 -m whaleguard --scenario santa-barbara for the archive-only demo.'
  exit 1
fi
export AISSTREAM_API_KEY
exec .venv/bin/python -m whaleguard --scenario santa-barbara --aisstream --port "${1:-8767}"
