#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# Override by passing the ns-3.35 directory as the first argument.
python3 scripts/run.py smoke --ns3 "${1:-$HOME/ns-allinone-3.35/ns-3.35}"
