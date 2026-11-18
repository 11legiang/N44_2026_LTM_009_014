#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AP Dual-Stack Socket Collector (UDP + TCP) & Real-time Web Telemetry Dashboard.
- Listens simultaneously on UDP (--udp-port, default 9100) and TCP (--tcp-port, default 9101)
  for 24-byte N44 binary-framed packets (from sta_socket_clients.py or ns-3 --liveUdpPort).
- Serves interactive Web Dashboard at http://127.0.0.1:8080 showing:
    1) Live 50x50m AP/STA mobility radar
    2) Live TCP vs UDP Socket metrics (Frames, CRC32 OK, Out-of-order, App E2E Delay, Jitter, JFI)
    3) Raw 24-byte Binary Header hex breakdown
    4) Saved ns-3.35 matrix explorer (aggregate.csv & fairness_summary.csv)
"""

import argparse
import collections
import csv
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from n44_protocol import try_unpack_stream

ROOT = Path(__file__).resolve().parents[1]


class CollectorState:
    def __init__(self, results_dir: Path):
        self.lock = threading.Lock()
        self.results_dir = results_dir
        self.start_time = time.time()
        self.stations: Dict[str, Dict[str, Any]] = {}
        self.recent_frames = collections.deque(maxlen=40)
        self.totals = {
            "udp": self._empty_proto(),
            "tcp": self._empty_proto(),
        }

    @staticmethod
    def _empty_proto() -> Dict[str, Any]:
        return {
            "frames_rx": 0,
            "bytes_rx": 0,
            "crc_ok": 0,
            "crc_bad": 0,
            "out_of_order": 0,
            "delay_sum_ms": 0.0,
            "jitter_sum_ms": 0.0,
            "jitter_samples": 0,
            "window_bytes": collections.deque(),  # (ts, bytes) for 2s rolling goodput
        }

    def record_frame(self, proto: str, frame: Dict[str, Any], rx_ns: int) -> None:
        now = time.time()
        sta_id = int(frame["station_id"])
        key = f"{proto}:{sta_id}"
        payload = frame.get("payload")
        if isinstance(payload, dict) and "t" in payload:
            # Packet forwarded from ns-3 simulation
            delay_ms = float(payload.get("delay_ms", 0.0))
            x = float(payload.get("x", 0.0))
            y = float(payload.get("y", 0.0))
        else:
            delay_ms = max(0.0, (rx_ns - int(frame["tx_time_ns"])) / 1e6)
            x = float(payload.get("x", 0.0)) if isinstance(payload, dict) else 0.0
            y = float(payload.get("y", 0.0)) if isinstance(payload, dict) else 0.0

        with self.lock:
            pt = self.totals[proto]
            pt["frames_rx"] += 1
            pt["bytes_rx"] += int(frame["frame_bytes"])
            if frame["crc_ok"]:
                pt["crc_ok"] += 1
            else:
                pt["crc_bad"] += 1
            pt["delay_sum_ms"] += delay_ms
            pt["window_bytes"].append((now, int(frame["frame_bytes"])))
            cutoff = now - 2.0
            while pt["window_bytes"] and pt["window_bytes"][0][0] < cutoff:
                pt["window_bytes"].popleft()

            st = self.stations.setdefault(
                key,
                {
                    "proto": proto,
                    "station_id": sta_id,
                    "x": x,
                    "y": y,
                    "frames_rx": 0,
                    "bytes_rx": 0,
                    "expected_seq": 0,
                    "out_of_order": 0,
                    "last_delay_ms": None,
                    "delay_sum_ms": 0.0,
                    "jitter_sum_ms": 0.0,
                    "jitter_samples": 0,
                    "last_seen": now,
                    "window_bytes": collections.deque(),
                },
            )
            st["x"] = x
            st["y"] = y
            st["frames_rx"] += 1
            st["bytes_rx"] += int(frame["frame_bytes"])
            st["last_seen"] = now
            st["delay_sum_ms"] += delay_ms
            seq = int(frame["seq_num"])
            if seq < st["expected_seq"]:
                st["out_of_order"] += 1
                pt["out_of_order"] += 1
            else:
                st["expected_seq"] = seq + 1
            if st["last_delay_ms"] is not None:
                j = abs(delay_ms - st["last_delay_ms"])
                st["jitter_sum_ms"] += j
                st["jitter_samples"] += 1
                pt["jitter_sum_ms"] += j
                pt["jitter_samples"] += 1
            st["last_delay_ms"] = delay_ms
            st["window_bytes"].append((now, int(frame["frame_bytes"])))
            while st["window_bytes"] and st["window_bytes"][0][0] < cutoff:
                st["window_bytes"].popleft()

            self.recent_frames.appendleft(
                {
                    "proto": proto.upper(),
                    "sta": sta_id,
                    "seq": seq,
                    "len": int(frame["frame_bytes"]),
                    "delay_ms": round(delay_ms, 2),
                    "crc_ok": bool(frame["crc_ok"]),
                    "crc": frame["rx_crc32"],
                    "hex": frame["header_hex"],
                }
            )

    def reset_live(self) -> None:
        with self.lock:
            self.stations.clear()
            self.recent_frames.clear()
            self.totals = {"udp": self._empty_proto(), "tcp": self._empty_proto()}

    def snapshot_live(self) -> Dict[str, Any]:
        now = time.time()
        cutoff = now - 2.0
        with self.lock:
            proto_summary = {}
            for p, pt in self.totals.items():
                while pt["window_bytes"] and pt["window_bytes"][0][0] < cutoff:
                    pt["window_bytes"].popleft()
                kbps = sum(b for _, b in pt["window_bytes"]) * 8.0 / (2.0 * 1000.0)
                active_gps = []
                for st in self.stations.values():
                    if st["proto"] == p and now - st["last_seen"] < 5.0:
                        while st["window_bytes"] and st["window_bytes"][0][0] < cutoff:
                            st["window_bytes"].popleft()
                        active_gps.append(sum(b for _, b in st["window_bytes"]) * 8.0 / (2.0 * 1000.0))
                sq = sum(v * v for v in active_gps)
                jfi = (sum(active_gps) ** 2) / (len(active_gps) * sq) if active_gps and sq > 0 else 0.0
                proto_summary[p] = {
                    "frames_rx": pt["frames_rx"],
                    "bytes_rx": pt["bytes_rx"],
                    "crc_ok": pt["crc_ok"],
                    "crc_bad": pt["crc_bad"],
                    "out_of_order": pt["out_of_order"],
                    "mean_delay_ms": round(pt["delay_sum_ms"] / pt["frames_rx"], 2) if pt["frames_rx"] else 0.0,
                    "mean_jitter_ms": round(pt["jitter_sum_ms"] / pt["jitter_samples"], 2) if pt["jitter_samples"] else 0.0,
                    "goodput_kbps": round(kbps, 1),
                    "active_stations": len(active_gps),
                    "jfi": round(jfi, 4),
                }
            sta_list = []
            for st in self.stations.values():
                if now - st["last_seen"] <= 10.0:
                    while st["window_bytes"] and st["window_bytes"][0][0] < cutoff:
                        st["window_bytes"].popleft()
                    kbps = sum(b for _, b in st["window_bytes"]) * 8.0 / (2.0 * 1000.0)
                    sta_list.append(
                        {
                            "proto": st["proto"],
                            "station_id": st["station_id"],
                            "x": round(st["x"], 2),
                            "y": round(st["y"], 2),
                            "frames_rx": st["frames_rx"],
                            "goodput_kbps": round(kbps, 1),
                            "mean_delay_ms": round(st["delay_sum_ms"] / st["frames_rx"], 2) if st["frames_rx"] else 0.0,
                        }
                    )
            return {
                "uptime_s": round(now - self.start_time, 1),
                "totals": proto_summary,
                "stations": sta_list,
                "recent_frames": list(self.recent_frames)[:15],
            }

    def snapshot_matrix(self) -> Dict[str, Any]:
        def read_csv(name: str) -> List[Dict[str, str]]:
            p = self.results_dir / name
            if not p.exists():
                return []
            with p.open(encoding="utf-8") as f:
                return list(csv.DictReader(f))

        return {
            "results_dir": str(self.results_dir),
            "aggregate": read_csv("aggregate.csv"),
            "fairness": read_csv("fairness_summary.csv"),
            "paired": read_csv("paired_differences.csv"),
        }


def udp_listener(host: str, port: int, state: CollectorState) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    print(f"[UDP] Listening on {host}:{port}", flush=True)
    while True:
        data, _ = sock.recvfrom(65535)
        rx_ns = time.time_ns()
        buf = bytearray(data)
        while len(buf) >= 24:
            frame, consumed = try_unpack_stream(buf)
            if consumed:
                del buf[:consumed]
            if frame is not None:
                state.record_frame("udp", frame, rx_ns)
            elif consumed == 0:
                break


def tcp_client_handler(conn: socket.socket, state: CollectorState) -> None:
    buf = bytearray()
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            rx_ns = time.time_ns()
            buf.extend(chunk)
            while len(buf) >= 24:
                frame, consumed = try_unpack_stream(buf)
                if consumed:
                    del buf[:consumed]
                if frame is None:
                    break
                state.record_frame("tcp", frame, rx_ns)
    except OSError:
        pass
    finally:
        conn.close()


def tcp_listener(host: str, port: int, state: CollectorState) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(256)
    print(f"[TCP] Listening on {host}:{port}", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=tcp_client_handler, args=(conn, state), daemon=True).start()


DASHBOARD_HTML = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>N44 Wi-Fi Socket Telemetry & ns-3 Matrix Monitor</title>
<style>
  :root { --bg:#0f172a; --card:#1e293b; --border:#334155; --txt:#e2e8f0; --muted:#94a3b8; --tcp:#38bdf8; --udp:#fb923c; --ok:#34d399; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--txt); padding:18px; }
  h1 { margin:0 0 4px; font-size:20px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:16px; }
  .grid { display:grid; grid-template-columns: 360px 1fr; gap:16px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; }
  .kpis { display:grid; grid-template-columns: repeat(2, 1fr); gap:12px; margin-bottom:12px; }
  .kpi-title { font-size:12px; color:var(--muted); margin-bottom:4px; font-weight:600; }
  .kpi-row { display:flex; justify-content:space-between; font-size:13px; padding:3px 0; border-bottom:1px dashed #293548; }
  .tcp { color:var(--tcp); font-weight:700; }
  .udp { color:var(--udp); font-weight:700; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); }
  th { color:var(--muted); font-weight:600; }
  .mono { font-family:ui-monospace,Consolas,monospace; font-size:11px; }
  .badge { padding:2px 6px; border-radius:4px; font-size:11px; font-weight:600; }
  .ok { background:#064e3b; color:var(--ok); }
  .bad { background:#7f1d1d; color:#fca5a5; }
  canvas { background:#0b1120; border:1px solid var(--border); border-radius:8px; width:100%; height:auto; display:block; }
  .btn { background:#334155; color:var(--txt); border:none; padding:6px 12px; border-radius:6px; cursor:pointer; font-size:12px; }
  .btn:hover { background:#475569; }
</style>
</head>
<body>
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <div>
      <h1>N44 — Wi-Fi TCP vs UDP Socket Collector & ns-3.35 Monitor</h1>
      <div class="sub">24-byte Big-Endian Binary Header (Magic N4 + SeqNum + TxTimeNs + CRC32) | 50×50m AP/STA Topology</div>
    </div>
    <button class="btn" onclick="fetch('/api/reset',{method:'POST'})">Reset Live Counters</button>
  </div>

  <div class="grid">
    <div class="card">
      <div class="kpi-title">LIVE 50×50m TOPOLOGY RADAR (AP at 0,0)</div>
      <canvas id="radar" width="320" height="320"></canvas>
      <div style="font-size:11px;color:var(--muted);margin-top:8px;">
        <span class="tcp">● TCP STA</span> &nbsp;&nbsp; <span class="udp">■ UDP STA</span> &nbsp;&nbsp; <span style="color:#ef4444">▲ AP (0,0)</span>
      </div>
    </div>

    <div class="card">
      <div class="kpis">
        <div class="card" style="background:#0f172a">
          <div class="kpi-title tcp">TCP SOCKET STREAM (Port 9101)</div>
          <div id="tcp-kpi"></div>
        </div>
        <div class="card" style="background:#0f172a">
          <div class="kpi-title udp">UDP SOCKET DATAGRAM (Port 9100)</div>
          <div id="udp-kpi"></div>
        </div>
      </div>

      <div class="kpi-title">RECENT DECODED 24B BINARY FRAMES</div>
      <table>
        <thead><tr><th>Proto</th><th>STA</th><th>Seq</th><th>E2E Delay</th><th>CRC32</th><th>Header Hex (24 Bytes)</th></tr></thead>
        <tbody id="frames-body"></tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <div class="kpi-title">NS-3.35 VALIDATED MATRIX SUMMARY (30 RUNS — results/final-01) &amp; JAIN'S FAIRNESS INDEX</div>
    <table>
      <thead><tr><th>STAs</th><th>Proto</th><th>IP PDR (%)</th><th>IP Loss (%)</th><th>IP Delay (ms)</th><th>App Goodput (Kbps)</th><th>Jain Fairness (JFI)</th></tr></thead>
      <tbody id="matrix-body"></tbody>
    </table>
  </div>

<script>
function renderKpi(elId, d) {
  document.getElementById(elId).innerHTML = [
    ['Frames Rx', d.frames_rx],
    ['Active STAs', d.active_stations],
    ['Goodput', d.goodput_kbps + ' Kbps'],
    ['App E2E Delay', d.mean_delay_ms + ' ms'],
    ['App Jitter', d.mean_jitter_ms + ' ms'],
    ['CRC32 OK / Bad', d.crc_ok + ' / ' + d.crc_bad],
    ['Out-of-Order', d.out_of_order],
    ['Live Jain Index', d.jfi]
  ].map(([k,v]) => `<div class="kpi-row"><span>${k}</span><b>${v}</b></div>`).join('');
}

function drawRadar(stations) {
  const c = document.getElementById('radar');
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height, pad = 20;
  ctx.clearRect(0,0,W,H);
  const toPx = (x,y) => [W/2 + (x/25)*(W/2-pad), H/2 - (y/25)*(H/2-pad)];
  ctx.strokeStyle = '#1e293b'; ctx.lineWidth = 1;
  [10, 20, 25].forEach(r => {
    ctx.beginPath(); ctx.arc(W/2, H/2, (r/25)*(W/2-pad), 0, Math.PI*2); ctx.stroke();
  });
  ctx.strokeRect(pad, pad, W-2*pad, H-2*pad);
  // AP
  ctx.fillStyle = '#ef4444';
  ctx.beginPath(); ctx.moveTo(W/2, H/2-7); ctx.lineTo(W/2-7, H/2+6); ctx.lineTo(W/2+7, H/2+6); ctx.closePath(); ctx.fill();
  for (const s of stations) {
    const [px, py] = toPx(s.x, s.y);
    ctx.fillStyle = s.proto === 'tcp' ? '#38bdf8' : '#fb923c';
    if (s.proto === 'tcp') { ctx.beginPath(); ctx.arc(px, py, 4.5, 0, Math.PI*2); ctx.fill(); }
    else { ctx.fillRect(px-4, py-4, 8, 8); }
    ctx.fillStyle = '#94a3b8'; ctx.font = '10px sans-serif';
    ctx.fillText('S'+s.station_id, px+6, py+3);
  }
}

async function pollLive() {
  try {
    const res = await fetch('/api/live');
    const data = await res.json();
    renderKpi('tcp-kpi', data.totals.tcp);
    renderKpi('udp-kpi', data.totals.udp);
    drawRadar(data.stations);
    document.getElementById('frames-body').innerHTML = data.recent_frames.map(f => `
      <tr>
        <td class="${f.proto==='TCP'?'tcp':'udp'}">${f.proto}</td>
        <td>STA-${f.sta}</td>
        <td>#${f.seq}</td>
        <td>${f.delay_ms} ms</td>
        <td><span class="badge ${f.crc_ok?'ok':'bad'}">${f.crc}</span></td>
        <td class="mono">${f.hex}</td>
      </tr>`).join('');
  } catch (e) {}
}

async function loadMatrix() {
  const res = await fetch('/api/matrix');
  const m = await res.json();
  const byKey = {};
  for (const r of m.aggregate) {
    byKey[`${r.stations}-${r.transport}`] = byKey[`${r.stations}-${r.transport}`] || {};
    byKey[`${r.stations}-${r.transport}`][r.metric] = parseFloat(r.mean).toFixed(2);
  }
  for (const f of m.fairness) {
    const k = `${f.stations}-${f.transport}`;
    if (byKey[k]) byKey[k].jfi = parseFloat(f.mean).toFixed(4);
  }
  const keys = Object.keys(byKey).sort((a,b) => {
    const [na, pa] = a.split('-'), [nb, pb] = b.split('-');
    return parseInt(na)-parseInt(nb) || pa.localeCompare(pb);
  });
  document.getElementById('matrix-body').innerHTML = keys.map(k => {
    const [n, p] = k.split('-'), d = byKey[k];
    return `<tr><td>${n}</td><td class="${p}">${p.toUpperCase()}</td>
      <td>${d.pdr_ip_pct||'-'}</td><td>${d.loss_ip_pct||'-'}</td>
      <td>${d.delay_ip_ms||'-'}</td><td>${d.goodput_active_kbps||'-'}</td><td>${d.jfi||'-'}</td></tr>`;
  }).join('');
}

loadMatrix();
pollLive();
setInterval(pollLive, 500);
</script>
</body>
</html>
"""


def make_handler(state: CollectorState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _json(self, obj: Any, code: int = 200):
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/api/live":
                self._json(state.snapshot_live())
            elif self.path == "/api/matrix":
                self._json(state.snapshot_matrix())
            else:
                raw = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        def do_POST(self):
            if self.path == "/api/reset":
                state.reset_live()
                self._json({"ok": True})
            else:
                self._json({"error": "Not found"}, 404)

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--udp-port", type=int, default=9100)
    ap.add_argument("--tcp-port", type=int, default=9101)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--results", type=Path, default=ROOT / "results" / "final-01")
    args = ap.parse_args()

    state = CollectorState(args.results)
    threading.Thread(target=udp_listener, args=(args.host, args.udp_port, state), daemon=True).start()
    threading.Thread(target=tcp_listener, args=(args.host, args.tcp_port, state), daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.http_port), make_handler(state))
    print(f"[HTTP] Dashboard ready at http://{args.host}:{args.http_port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
