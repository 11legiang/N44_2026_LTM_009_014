#!/usr/bin/env python3
"""Aggregate independent runs; never turn missing delay into zero."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics
import sys
from run import validate

METRICS = {
    'pdr_ip_pct': ('IP packet delivery ratio', '%'),
    'loss_ip_pct': ('IP packet loss ratio', '%'),
    'unreceived_ip_packets': ('IP packets not received by cutoff', 'packets'),
    'delay_ip_ms': ('Mean delay of received IP packets', 'ms'),
    'throughput_ip_kbps': ('Received IP throughput (sending + drain window)', 'Kbps'),
    'goodput_active_kbps': ('Application goodput (sending window)', 'Kbps'),
}


def summarize(values, t):
    n = len(values)
    if not n:
        return dict(n_valid=0, mean='', sd='', ci95_half='', ci95_low='', ci95_high='')
    mean = statistics.mean(values)
    if n == 1:
        return dict(n_valid=1, mean=mean, sd='', ci95_half='', ci95_low='', ci95_high='')
    sd = statistics.stdev(values)
    half = float(t.ppf(0.975, n-1)) * sd / math.sqrt(n)
    return dict(n_valid=n, mean=mean, sd=sd, ci95_half=half, ci95_low=mean-half, ci95_high=mean+half)


def jain_index(values):
    s = sum(values)
    sq = sum(v * v for v in values)
    if not values or sq <= 0:
        return 0.0
    return (s * s) / (len(values) * sq)


def pearson_r(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    denom = math.sqrt(sum(a * a for a in dx) * sum(b * b for b in dy))
    if denom <= 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / denom


def write_csv(path, rows):
    if not rows:
        return
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('--allow-partial', action='store_true', help='Explicitly allow failed/missing runs in descriptive plots')
    a = p.parse_args()
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from scipy.stats import t
    except ImportError:
        p.error('Install plotting dependencies: python3 -m pip install -r requirements.txt')
    folder = a.directory.resolve()
    config = json.loads((folder/'experiment.json').read_text())
    statuses = json.loads((folder/'status.json').read_text())
    expected = len(config['stations']) * len(config['transport']) * config['runs']
    incomplete = len(statuses) != expected or any(x['status'] != 'ok' for x in statuses)
    if incomplete and not a.allow_partial:
        p.error('Missing/failed runs: inspect status.json. Fix them before analysis, or explicitly use --allow-partial.')
    with (folder/'runs.csv').open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        p.error('No valid runs')
    keys = [(r['transport'], r['stations'], r['seed'], r['run']) for r in rows]
    if len(keys) != len(set(keys)):
        p.error('Duplicate run IDs')
    expected_keys = {(proto, str(n), str(config['seed']), str(run))
                     for proto in config['transport'] for n in config['stations']
                     for run in range(config['run_start'], config['run_start']+config['runs'])}
    if set(keys) - expected_keys or (not incomplete and set(keys) != expected_keys):
        p.error('Run identities do not match experiment.json')
    if len({s['case'] for s in statuses}) != len(statuses):
        p.error('Duplicate case IDs in status.json')
    if {r['case'] for r in rows} != {s['case'] for s in statuses if s['status']=='ok'}:
        p.error('Case identities do not match status.json')
    for r in rows:
        checked = validate(folder/r['case']/'result', int(r['stations']))
        if any(r.get(k) != value for k,value in checked.items()):
            p.error('runs.csv differs from its original case: '+r['case'])
    constants = ['seed', 'mobility', 'start_s', 'duration_s', 'cleanup_s', 'rate_mbps_per_sta',
                 'packet_size', 'half_side_m', 'speed_mps', 'tx_power_dbm', 'phy_mode', 'tcp_variant']
    for key in constants:
        if len({r[key] for r in rows}) != 1:
            p.error('Mixed experimental conditions: ' + key)
    if len(rows) != sum(x['status'] == 'ok' for x in statuses):
        p.error('runs.csv does not match status.json')
    groups = {}
    for r in rows:
        groups.setdefault((r['transport'], int(r['stations'])), []).append(r)
    aggregates = []
    for (proto, stations), records in sorted(groups.items()):
        for metric in METRICS:
            values = [float(r[metric]) for r in records if r[metric] != '']
            if any(not math.isfinite(v) for v in values):
                p.error('Non-finite values in ' + metric)
            aggregates.append(dict(transport=proto, stations=stations, metric=metric,
                n_runs=len(records), n_missing=len(records)-len(values),
                zero_rx_runs=sum(int(r['rx_ip_packets']) == 0 for r in records),
                **summarize(values, t)))
    write_csv(folder/'aggregate.csv', aggregates)
    # TCP and UDP use paired mobility realizations. CI of their difference
    # is more useful than deciding significance by overlapping marginal CIs.
    lookup = {key:r for key,r in zip(keys, rows)}
    pairs = []
    for stations in sorted({r['stations'] for r in rows}, key=int):
        for metric in METRICS:
            diffs = []
            for r in rows:
                if r['transport'] != 'tcp' or r['stations'] != stations:
                    continue
                udp = lookup.get(('udp', stations, r['seed'], r['run']))
                if udp and r[metric] != '' and udp[metric] != '':
                    diffs.append(float(r[metric])-float(udp[metric]))
            pairs.append(dict(stations=stations, metric=metric, difference='TCP minus UDP', **summarize(diffs, t)))
    write_csv(folder/'paired_differences.csv', pairs)
    plt.rcParams.update({'font.size':10, 'axes.spines.top':False, 'axes.spines.right':False})
    fig, axes = plt.subplots(3, 2, figsize=(12, 11), constrained_layout=True)
    colors = {'tcp':'#165DAD', 'udp':'#DE6C25'}
    for ax, (metric, (title, unit)) in zip(axes.flat, METRICS.items()):
        for proto in ['tcp', 'udp']:
            points = sorted([r for r in aggregates if r['transport']==proto and r['metric']==metric and r['mean']!=''], key=lambda r:r['stations'])
            if not points:
                continue
            # Plot mean alone when only one run exists; CI is undefined.
            xs = [r['stations'] for r in points]
            ys = [r['mean'] for r in points]
            ax.plot(xs, ys, marker='o' if proto == 'tcp' else 's', linestyle='-' if proto == 'tcp' else '--', fillstyle='none' if proto == 'udp' else 'full', label=proto.upper(), color=colors[proto])
            for r in points:
                if r['ci95_half'] != '':
                    ax.errorbar(r['stations'], r['mean'], yerr=r['ci95_half'], fmt='none', capsize=4, color=colors[proto])
        ax.set_title(title, fontsize=10)
        ax.set_xlabel('Number of sending stations (AP excluded)')
        ax.set_ylabel(unit)
        ax.set_xticks(sorted({int(r['stations']) for r in rows}))
        bounds = [r for r in aggregates if r['metric'] == metric and r['mean'] != '']
        if bounds:
            low = min(float(r['ci95_low']) if r['ci95_low'] != '' else float(r['mean']) for r in bounds)
            high = max(float(r['ci95_high']) if r['ci95_high'] != '' else float(r['mean']) for r in bounds)
            bottom = min(0, low * 1.08)
            top = max(1, high * 1.08)
            if metric == 'pdr_ip_pct':
                top = max(105, top)
            ax.set_ylim(bottom, top)
        else:
            ax.text(.5, .5, 'Undefined: no received IP packets', ha='center', va='center', transform=ax.transAxes, fontsize=9)
        ax.grid(alpha=.2)
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend()
    mode = config['mode'].upper()
    ci_label = 'Mean and approximate 95% Student-t confidence intervals' if any(r['n_valid'] > 1 for r in aggregates) else 'Single run per configuration — confidence intervals unavailable'
    fig.suptitle(f'Wi-Fi TCP/UDP — {mode}' + (' — PARTIAL DATA' if incomplete else '') + '\n' + ci_label, fontsize=13)
    fig.savefig(folder/'comparison.png', dpi=180)
    fig.savefig(folder/'comparison.pdf')
    plt.close(fig)

    # --- Deep Per-Station Analysis: Jain's Fairness Index & Distance-to-AP Correlation ---
    fairness_rows = []
    station_points = []
    for r in rows:
        case_dir = folder/r['case']
        with (case_dir/'result-flows.csv').open(encoding='utf-8') as ff:
            fl = list(csv.DictReader(ff))
        with (case_dir/'result-positions.csv').open(encoding='utf-8') as pf:
            pos_map = {p['station']: p for p in csv.DictReader(pf)}
        goodputs = [float(x['goodput_active_kbps']) for x in fl]
        jfi = jain_index(goodputs)
        fairness_rows.append(dict(case=r['case'], transport=r['transport'],
                                  stations=int(r['stations']), run=int(r['run']), jfi=jfi))
        for x in fl:
            st = x['station']
            p = pos_map[st]
            d_start = math.hypot(float(p['x_start_m']), float(p['y_start_m']))
            d_end = math.hypot(float(p['x_end_m']), float(p['y_end_m']))
            d_mean = 0.5 * (d_start + d_end)
            station_points.append(dict(
                transport=r['transport'], stations=int(r['stations']), run=int(r['run']),
                station=int(st), dist_mean_m=d_mean,
                goodput_active_kbps=float(x['goodput_active_kbps']),
                pdr_ip_pct=float(x['pdr_ip_pct']) if x['pdr_ip_pct'] != '' else None,
            ))

    fairness_agg = []
    for (proto, n), grp in sorted({(x['transport'], x['stations']): [y for y in fairness_rows if y['transport']==x['transport'] and y['stations']==x['stations']] for x in fairness_rows}.items()):
        jfi_vals = [x['jfi'] for x in grp]
        sub_pts = [pt for pt in station_points if pt['transport'] == proto and pt['stations'] == n]
        r_gp = pearson_r([pt['dist_mean_m'] for pt in sub_pts], [pt['goodput_active_kbps'] for pt in sub_pts])
        pdr_pts = [pt for pt in sub_pts if pt['pdr_ip_pct'] is not None]
        r_pdr = pearson_r([pt['dist_mean_m'] for pt in pdr_pts], [pt['pdr_ip_pct'] for pt in pdr_pts])
        fairness_agg.append(dict(
            transport=proto, stations=n,
            pearson_dist_goodput=f'{r_gp:.6f}' if r_gp is not None else '',
            pearson_dist_pdr=f'{r_pdr:.6f}' if r_pdr is not None else '',
            **summarize(jfi_vals, t),
        ))
    write_csv(folder/'fairness_summary.csv', fairness_agg)

    max_n = max(int(r['stations']) for r in rows)
    fig_deep, (ax_jfi, ax_ecdf, ax_dist) = plt.subplots(1, 3, figsize=(15, 4.6), constrained_layout=True)
    for proto in ['tcp', 'udp']:
        pts = sorted([x for x in fairness_agg if x['transport'] == proto], key=lambda x: x['stations'])
        if pts:
            xs = [x['stations'] for x in pts]
            ys = [x['mean'] for x in pts]
            ax_jfi.plot(xs, ys, marker='o' if proto == 'tcp' else 's',
                        linestyle='-' if proto == 'tcp' else '--',
                        fillstyle='none' if proto == 'udp' else 'full',
                        color=colors[proto], label=proto.upper())
            for x in pts:
                if x['ci95_half'] != '':
                    ax_jfi.errorbar(x['stations'], x['mean'], yerr=x['ci95_half'], fmt='none', capsize=4, color=colors[proto])
    ax_jfi.set_title("Jain's Fairness Index (Goodput)", fontsize=10)
    ax_jfi.set_xlabel('Number of sending stations')
    ax_jfi.set_ylabel('JFI (0..1, 1 = perfectly fair)')
    ax_jfi.set_ylim(0.5, 1.05)
    ax_jfi.set_xticks(sorted({int(r['stations']) for r in rows}))
    ax_jfi.grid(alpha=0.2)
    ax_jfi.legend()

    for proto in ['tcp', 'udp']:
        gps = sorted(pt['goodput_active_kbps'] for pt in station_points if pt['transport'] == proto and pt['stations'] == max_n)
        if gps:
            ys = [(idx + 1) / len(gps) for idx in range(len(gps))]
            ax_ecdf.step(gps, ys, where='post', color=colors[proto],
                         linestyle='-' if proto == 'tcp' else '--',
                         label=f"{proto.upper()} (n={len(gps)} flows)")
    ax_ecdf.set_title(f'Per-Station Goodput ECDF (N={max_n})', fontsize=10)
    ax_ecdf.set_xlabel('Station active goodput (Kbps)')
    ax_ecdf.set_ylabel('Cumulative fraction')
    ax_ecdf.grid(alpha=0.2)
    ax_ecdf.legend()

    for proto in ['tcp', 'udp']:
        sub = [pt for pt in station_points if pt['transport'] == proto and pt['stations'] == max_n]
        if sub:
            dx = [pt['dist_mean_m'] for pt in sub]
            gy = [pt['goodput_active_kbps'] for pt in sub]
            ax_dist.scatter(dx, gy, alpha=0.55, s=22, color=colors[proto],
                            marker='o' if proto == 'tcp' else 'x', label=proto.upper())
            # Linear fit via ordinary least squares
            mx, my = statistics.mean(dx), statistics.mean(gy)
            denom = sum((x - mx) ** 2 for x in dx)
            if denom > 0:
                slope = sum((x - mx) * (y - my) for x, y in zip(dx, gy)) / denom
                intercept = my - slope * mx
                x0, x1 = min(dx), max(dx)
                ax_dist.plot([x0, x1], [slope * x0 + intercept, slope * x1 + intercept],
                             color=colors[proto], linestyle='-' if proto == 'tcp' else '--', linewidth=1.6)
    ax_dist.set_title(f'Distance to AP vs Station Goodput (N={max_n})', fontsize=10)
    ax_dist.set_xlabel('Mean distance to AP (m)')
    ax_dist.set_ylabel('Station active goodput (Kbps)')
    ax_dist.grid(alpha=0.2)
    ax_dist.legend()

    fig_deep.suptitle(f'Per-Station Fairness & Spatial Dependency — {mode}', fontsize=12)
    fig_deep.savefig(folder/'deep_analysis.png', dpi=180)
    fig_deep.savefig(folder/'deep_analysis.pdf')
    plt.close(fig_deep)

    # --- Optional Schema v3 Custom Socket App-Layer Plot ---
    has_v3 = all(r.get('schema_version') == '3' and r.get('app_mode') == 'socket' for r in rows)
    if has_v3:
        v3_metrics = {
            'app_pdr_pct': ('Application Frame Delivery Ratio', '%'),
            'app_delay_ms': ('True Application E2E Delay (Reassembled)', 'ms'),
            'app_jitter_ms': ('Application Delay Jitter', 'ms'),
        }
        v3_agg = []
        for (proto, stations), records in sorted(groups.items()):
            for m in v3_metrics:
                vals = [float(r[m]) for r in records if r.get(m, '') != '']
                v3_agg.append(dict(transport=proto, stations=stations, metric=m, **summarize(vals, t)))
        write_csv(folder/'app_layer_aggregate.csv', v3_agg)
        fig_app, axes_app = plt.subplots(1, 3, figsize=(15, 4.4), constrained_layout=True)
        for ax, (m, (title, unit)) in zip(axes_app, v3_metrics.items()):
            for proto in ['tcp', 'udp']:
                pts = sorted([x for x in v3_agg if x['transport'] == proto and x['metric'] == m and x['mean'] != ''], key=lambda x: x['stations'])
                if pts:
                    ax.plot([x['stations'] for x in pts], [x['mean'] for x in pts],
                            marker='o' if proto == 'tcp' else 's',
                            linestyle='-' if proto == 'tcp' else '--',
                            color=colors[proto], label=proto.upper())
                    for x in pts:
                        if x['ci95_half'] != '':
                            ax.errorbar(x['stations'], x['mean'], yerr=x['ci95_half'], fmt='none', capsize=4, color=colors[proto])
            ax.set_title(title, fontsize=10)
            ax.set_xlabel('Number of sending stations')
            ax.set_ylabel(unit)
            ax.set_xticks(sorted({int(r['stations']) for r in rows}))
            ax.grid(alpha=0.2)
            ax.legend()
        fig_app.suptitle(f'Custom 24B-Framed Socket Application Metrics — {mode}', fontsize=12)
        fig_app.savefig(folder/'app_layer_comparison.png', dpi=180)
        fig_app.savefig(folder/'app_layer_comparison.pdf')
        plt.close(fig_app)

    notes = [
        'KẾT QUẢ TỔNG HỢP (tự động; không phải kết luận TCP/UDP hơn kém)',
        f"Chế độ: {config['mode']}; số lượt hợp lệ: {len(rows)}/{expected}.",
        'Mỗi lượt chạy là một mẫu thống kê, không coi từng gói tin là mẫu độc lập.',
        'aggregate.csv: trung bình, độ lệch chuẩn mẫu, CI 95% Student-t giữa các lượt.',
        'CI này xấp xỉ; ít lượt, dữ liệu lệch hoặc PDR sát 0/100% có thể làm CI kém tin cậy.',
        'Không cắt CI về 0..100; đầu mút vượt miền là giới hạn của cách xấp xỉ, không phải PDR hợp lệ.',
        'n_valid < n_runs ở delay: bỏ các lượt không nhận gói khỏi phép tính độ trễ, ghi rõ n_missing.',
        'Lượt có gửi nhưng nhận 0 vẫn được tính trong PDR, loss và goodput.',
        'paired_differences.csv: hiệu TCP trừ UDP theo cùng seed/run; không kết luận chỉ bằng việc các CI riêng chồng nhau.',
        'TCP PDR/delay tại IP tính các lần truyền lại và gói điều khiển cùng chiều; không phải độ tin cậy/độ trễ ứng dụng.',
        'goodput_active_kbps: byte ứng dụng nhận trong cửa sổ gửi / thời gian cửa sổ; Kbps = 1000 bit/s.',
        'throughput_ip_kbps: byte gồm IP header nhận đến cutoff / (duration + cleanup).',
        'Chuyển động là điều kiện mô phỏng; ma trận chính không tách riêng ảnh hưởng chuyển động.',
        'Tải yêu cầu mỗi trạm cố định: số trạm tăng làm tăng cả tổng tải yêu cầu và mức tranh chấp.',
    ]
    for (proto, n), records in sorted(groups.items()):
        zero = sum(int(r['rx_ip_packets']) == 0 for r in records)
        notes.append(f'{proto.upper()}, {n} trạm: {len(records)} lượt, {zero} lượt không nhận gói IP.')
        dead = sum(int(r.get('zero_app_rx_flows', 0)) for r in records)
        if dead:
            notes.append(f'  CẢNH BÁO: {dead} trường hợp trạm/run nhận 0 byte ứng dụng; phải xem result-flows.csv.')
        unready = sum(int(r.get('associated_pairs_at_start', n)) < n for r in records)
        if unready:
            notes.append(f'  CẢNH BÁO: {unready} lượt chưa đủ liên kết STA/AP lúc bắt đầu gửi.')
    notes.append('--- CÔNG BẰNG JAIN (JFI) VÀ TƯƠNG QUAN KHOẢNG CÁCH ---')
    for fa in fairness_agg:
        notes.append(f"{fa['transport'].upper()}, {fa['stations']} trạm: JFI mean={float(fa['mean']):.4f}"
                     + (f" (±{float(fa['ci95_half']):.4f})" if fa['ci95_half'] != '' else '')
                     + f", corr(dist, goodput)={fa['pearson_dist_goodput']}.")
    if incomplete:
        notes.append('CẢNH BÁO: thiếu lượt/lượt lỗi. Không dùng như ma trận hoàn chỉnh.')
    (folder/'analysis_notes.txt').write_text('\n'.join(notes)+'\n', encoding='utf-8')
    print('Đã tạo aggregate.csv, paired_differences.csv, fairness_summary.csv, comparison.png/.pdf, deep_analysis.png/.pdf, analysis_notes.txt')
    print(folder)

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print('Lỗi:', exc, file=sys.stderr)
        sys.exit(1)
