#!/usr/bin/env python3
"""
Gói 2: Chạy ma trận đối chứng cô lập biến (Ablation Study) tại N=20 STA trên ns-3.35:
  1. Baseline Socket (moving, NewReno, RTS/CTS off) -> đo True App E2E Delay & Jitter (schema v3)
  2. Static Mobility (static, NewReno, RTS/CTS off) -> cô lập tác động di chuyển vs tranh chấp MAC
  3. RTS/CTS Enabled (moving, NewReno, RTS/CTS on)  -> kiểm chứng hiệu quả chống hidden-node/va chạm
  4. TCP Cubic (moving, Cubic, RTS/CTS off)         -> so sánh thuật toán kiểm soát tắc nghẽn hiện đại
  5. TCP Bbr   (moving, Bbr, RTS/CTS off)           -> so sánh BBR với NewReno/Cubic

Đồng thời hỗ trợ --plot-only để vẽ biểu đồ so sánh trực quan (ablation_comparison.png/.pdf).
"""
import argparse
import csv
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    ("baseline_moving", dict(mobility="moving", tcp_variant="NewReno", rts_cts=False)),
    ("static_baseline", dict(mobility="static", tcp_variant="NewReno", rts_cts=False)),
    ("rts_cts_on",      dict(mobility="moving", tcp_variant="NewReno", rts_cts=True)),
    ("tcp_cubic",       dict(mobility="moving", tcp_variant="Cubic",   rts_cts=False)),
    ("tcp_bbr",         dict(mobility="moving", tcp_variant="Bbr",     rts_cts=False)),
]


def run_ablation(ns3: Path, out: Path, runs: int, stations: int, jobs: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for label, cfg in CASES:
        sub_out = out / label
        cmd = [
            sys.executable, str(ROOT / "scripts" / "run.py"), "matrix",
            "--ns3", str(ns3),
            "--out", str(sub_out),
            "--stations", str(stations),
            "--runs", str(runs),
            "--app-mode", "socket",
            "--mobility", cfg["mobility"],
            "--tcp-variant", cfg["tcp_variant"],
            "--jobs", str(jobs),
            "--resume",
        ]
        if cfg["rts_cts"]:
            cmd.append("--rts-cts")
        print(f"\n=== Running ablation scenario: {label} ===", flush=True)
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            raise RuntimeError(f"Ablation scenario {label} failed with exit code {rc}")


def plot_ablation(out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import statistics

    rows = []
    for label, _ in CASES:
        runs_csv = out / label / "runs.csv"
        if not runs_csv.exists():
            continue
        with runs_csv.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(dict(scenario=label, **r))
    if not rows:
        raise FileNotFoundError(f"No ablation runs.csv found under {out}")

    scenarios = [lbl for lbl, _ in CASES if any(r["scenario"] == lbl for r in rows)]
    metrics = [
        ("goodput_active_kbps", "App Goodput (Kbps)", 1.0),
        ("app_delay_ms", "True App E2E Delay (ms)", 1.0),
        ("app_pdr_pct", "App Frame Delivery (%)", 1.0),
        ("jains_fairness_active", "Jain Fairness Index", 1.0),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5), constrained_layout=True)
    x = list(range(len(scenarios)))
    w = 0.36
    colors = {"tcp": "#165DAD", "udp": "#DE6C25"}

    summary_rows = []
    for ax, (m, title, scale) in zip(axes.flat, metrics):
        for offset, proto in [(-w / 2, "tcp"), (w / 2, "udp")]:
            means, errs = [], []
            for sc in scenarios:
                vals = [float(r[m]) * scale for r in rows if r["scenario"] == sc and r["transport"] == proto and r.get(m, "") != ""]
                mu = statistics.mean(vals) if vals else 0.0
                sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
                means.append(mu)
                errs.append(sd)
                if m == metrics[0][0]:
                    summary_rows.append(dict(scenario=sc, transport=proto, metric=m, mean=mu, sd=sd))
            ax.bar([xi + offset for xi in x], means, width=w, yerr=errs, capsize=3,
                   color=colors[proto], alpha=0.88, label=proto.upper())
        ax.set_title(title, fontsize=10.5)
        ax.set_xticks(x)
        ax.set_xticklabels(scenarios, rotation=15, ha="right", fontsize=8.5)
        ax.grid(axis="y", alpha=0.25)
        ax.legend()

    fig.suptitle("Ablation Study (N=20 STAs, Custom 24B-Framed Socket App)", fontsize=13)
    fig.savefig(out / "ablation_comparison.png", dpi=180)
    fig.savefig(out / "ablation_comparison.pdf")
    plt.close(fig)
    print(f"Saved {out / 'ablation_comparison.png'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ns3", type=Path, default=Path.home() / "ns-allinone-3.35" / "ns-3.35")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "ablation-n20")
    ap.add_argument("--stations", type=int, default=20)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--plot-only", action="store_true")
    args = ap.parse_args()

    if not args.plot_only:
        run_ablation(args.ns3, args.out, args.runs, args.stations, args.jobs)
    plot_ablation(args.out)


if __name__ == "__main__":
    main()
