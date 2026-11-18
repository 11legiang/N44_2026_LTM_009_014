#!/usr/bin/env bash
# Áp dụng suy hao mạng thực tế lên interface loopback (lo) trên Ubuntu/Linux bằng tc netem,
# khớp với số liệu đo được từ ns-3.35 tại mốc 20 trạm (results/final-01):
#   - UDP 20 STA: loss 46.67%, mean delay 384 ms (±47 ms), jitter
#   - TCP 20 STA: bottleneck ~12 Mbps, delay ~5.3 ms
set -euo pipefail

MODE="${1:-status}"
IFACE="${2:-lo}"

case "$MODE" in
  udp-20sta)
    sudo tc qdisc replace dev "$IFACE" root netem delay 384ms 47ms distribution normal loss 46.67%
    echo "[netem] Đã áp dụng profile UDP 20 STA (delay 384±47ms, loss 46.67%) trên $IFACE"
    ;;
  tcp-20sta)
    sudo tc qdisc replace dev "$IFACE" root netem delay 5.3ms 1.5ms loss 0.24% rate 12mbit
    echo "[netem] Đã áp dụng profile TCP 20 STA (rate 12Mbps, delay 5.3ms, loss 0.24%) trên $IFACE"
    ;;
  clear)
    sudo tc qdisc del dev "$IFACE" root 2>/dev/null || true
    echo "[netem] Đã xóa cấu hình suy hao trên $IFACE"
    ;;
  status)
    tc qdisc show dev "$IFACE"
    ;;
  *)
    echo "Cách dùng: bash socket_demo/apply_netem.sh [udp-20sta|tcp-20sta|clear|status] [iface]"
    exit 1
    ;;
esac
