#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
ns3_root="${1:-$HOME/ns-allinone-3.35/ns-3.35}"
output_dir="results/visual-$(date +%Y%m%d-%H%M%S)"
python3 scripts/run.py single --stations 5 --transport udp --duration 10 --visual --ns3 "$ns3_root" --out "$output_dir"
animation_file="$PWD/$output_dir/udp-n5-r1/result-animation.xml"
printf '\nĐã tạo hoạt ảnh: %s\n' "$animation_file"
netanim_app="$(dirname "$ns3_root")/netanim-3.108/NetAnim"
if [ -x "$netanim_app" ]; then
  printf 'Đang mở NetAnim. Chọn Open XML và mở đường dẫn ở trên, rồi nhấn Play.\n'
  "$netanim_app" > "$output_dir/netanim-gui.log" 2>&1 &
else
  printf 'Mở ứng dụng NetAnim đã cài, chọn Open XML và mở file ở trên.\n'
fi
