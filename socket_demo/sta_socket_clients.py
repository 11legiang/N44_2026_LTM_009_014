#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simulated N Mobile Stations (STA 0..N-1) sending real OS TCP/UDP Socket traffic
using the 24-byte N44 Binary Protocol to ap_collector_server.py.

Supports:
  --profile clean          : Direct localhost OS sockets (no extra impairment)
  --profile ns3-udp-20sta  : Replays empirical 20-STA UDP impairment from results/final-01
                             (PDR ~53.33%, loss ~46.67%, delay ~384 ms, distance-dependent loss)
  --profile ns3-tcp-20sta  : Replays empirical 20-STA TCP throttling (~11.97 Mbps aggregate)
"""

import argparse
import math
import random
import socket
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from n44_protocol import pack_frame


def run_station(
    sta_id: int,
    transport: str,
    host: str,
    port: int,
    duration_s: float,
    rate_kbps: float,
    packet_size: int,
    profile: str,
    total_stations: int,
    seed: int,
) -> None:
    rng = random.Random(seed + sta_id * 101)
    x = rng.uniform(-24.75, 24.75)
    y = rng.uniform(-24.75, 24.75)
    angle = rng.uniform(0, 2 * math.pi)
    speed = 1.5
    last_turn = time.time()

    effective_kbps = rate_kbps
    if profile == "ns3-tcp-20sta":
        # Empirical 20-STA TCP unfairness from final-01 (~11.97 Mbps split with distance correlation)
        dist = math.hypot(x, y)
        scale = max(0.28, min(1.0, 0.95 - 0.021 * dist))
        effective_kbps = rate_kbps * scale

    interval_s = (packet_size * 8.0) / (effective_kbps * 1000.0)
    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM if transport == "tcp" else socket.SOCK_DGRAM,
    )
    if transport == "tcp":
        sock.connect((host, port))

    start_ts = time.time()
    seq = 0
    try:
        while time.time() - start_ts < duration_s:
            now = time.time()
            dt = now - last_turn
            if dt >= 1.0:
                angle = rng.uniform(0, 2 * math.pi)
                x = max(-25.0, min(25.0, x + speed * math.cos(angle)))
                y = max(-25.0, min(25.0, y + speed * math.sin(angle)))
                last_turn = now
                if profile == "ns3-tcp-20sta":
                    dist = math.hypot(x, y)
                    scale = max(0.28, min(1.0, 0.95 - 0.021 * dist))
                    interval_s = (packet_size * 8.0) / ((rate_kbps * scale) * 1000.0)

            tx_ns = time.time_ns()
            frame = pack_frame(
                station_id=sta_id,
                seq_num=seq,
                payload_obj={"x": round(x, 2), "y": round(y, 2), "spd": speed},
                pad_to_bytes=packet_size,
                tx_time_ns=tx_ns,
            )
            seq += 1

            if profile == "ns3-udp-20sta" and transport == "udp":
                # Empirical 20-STA UDP drop (~46.67% mean) + heavy queuing delay (~384 ms)
                dist = math.hypot(x, y)
                drop_prob = min(0.85, max(0.05, 0.15 + 0.017 * dist))
                if rng.random() < drop_prob:
                    time.sleep(interval_s)
                    continue
                extra_delay = max(0.01, rng.gauss(0.384, 0.047))
                def _delayed_send(f=frame):
                    try:
                        sock.sendto(f, (host, port))
                    except OSError:
                        pass
                t = threading.Timer(extra_delay, _delayed_send)
                t.daemon = True
                t.start()
            elif transport == "udp":
                sock.sendto(frame, (host, port))
            else:
                sock.sendall(frame)

            time.sleep(interval_s)
    except OSError:
        pass
    finally:
        sock.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stations", type=int, default=20)
    ap.add_argument("--transport", choices=["tcp", "udp", "both"], default="both")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--udp-port", type=int, default=9100)
    ap.add_argument("--tcp-port", type=int, default=9101)
    ap.add_argument("--duration", type=float, default=15.0)
    ap.add_argument("--rate-kbps", type=float, default=200.0)
    ap.add_argument("--packet-size", type=int, default=256)
    ap.add_argument("--profile", choices=["clean", "ns3-udp-20sta", "ns3-tcp-20sta"], default="clean")
    ap.add_argument("--seed", type=int, default=12345)
    args = ap.parse_args()

    protos = ["tcp", "udp"] if args.transport == "both" else [args.transport]
    threads = []
    print(f"Starting {args.stations} STAs x {protos} -> {args.host} (profile={args.profile}, duration={args.duration}s)", flush=True)
    for proto in protos:
        port = args.tcp_port if proto == "tcp" else args.udp_port
        prof = args.profile
        if args.transport == "both" and args.profile == "clean":
            prof = "ns3-tcp-20sta" if (proto == "tcp" and args.stations >= 20) else ("ns3-udp-20sta" if (proto == "udp" and args.stations >= 20) else "clean")
        for i in range(args.stations):
            th = threading.Thread(
                target=run_station,
                args=(i, proto, args.host, port, args.duration, args.rate_kbps, args.packet_size, prof, args.stations, args.seed),
                daemon=True,
            )
            th.start()
            threads.append(th)

    for th in threads:
        th.join()
    print("All STA socket streams finished.", flush=True)


if __name__ == "__main__":
    main()
