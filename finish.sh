#!/usr/bin/env bash
# One fixed experiment: 2 protocols x 3 station counts x 5 runs = 30 cases.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
study_python="${PYTHON:-python3}"
if [ -z "${PYTHON:-}" ] && [ -x .venv/bin/python ]; then
  study_python="$PWD/.venv/bin/python"
fi
if ! "$study_python" -c 'import matplotlib; import scipy' >/dev/null 2>&1; then
  printf 'Thiếu thư viện vẽ. Chạy các lệnh sau một lần, rồi chạy lại finish.sh:\n'
  printf 'python3 -m venv .venv\n.venv/bin/python -m pip install -r requirements.txt\n'
  exit 1
fi
study_output="${1:-results/final-01}"
study_ns3="${2:-${NS3_ROOT:-$HOME/ns-allinone-3.35/ns-3.35}}"
"$study_python" scripts/run.py matrix --runs 5 --run-start 1 --duration 30 --cleanup 10 \
  --ns3 "$study_ns3" --out "$study_output" --resume
"$study_python" scripts/analyze.py "$study_output"
printf '\nĐÃ XONG. Mở biểu đồ: %s/comparison.png\n' "$study_output"
printf 'Đọc aggregate.csv và analysis_notes.txt cùng biểu đồ.\n'
