#!/usr/bin/env python3
"""Install and run the experiment in an existing ns-3.35 tree; Python stdlib only."""
import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import sys
from fix_ns335 import repair

ROOT = Path(__file__).resolve().parents[1]
NAME = 'wifi-transport-study'
RELEASE = '2026-11-15-final'


def validate(prefix, stations):
    with Path(str(prefix) + '-summary.csv').open() as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 1:
        raise ValueError('Summary must contain exactly one record')
    r = rows[0]
    if int(r['stations']) != stations:
        raise ValueError('Wrong station count')
    tx, rx = int(r['tx_ip_packets']), int(r['rx_ip_packets'])
    if tx <= 0 or not 0 <= rx <= tx:
        raise ValueError('Invalid IP counts, or no transmitted packets')
    if int(r['zero_tx_flows']):
        raise ValueError('Some configured sources produced no IP traffic')
    if int(r['unreceived_ip_packets']) != tx - rx:
        raise ValueError('Incorrect unreceived count')
    for key, expected in [('pdr_ip_pct', 100 * rx / tx), ('loss_ip_pct', 100 * (tx-rx)/tx)]:
        if not math.isclose(float(r[key]), expected, rel_tol=1e-8, abs_tol=1e-8):
            raise ValueError('Invalid ' + key)
    if (rx == 0) != (r['delay_ip_ms'] == ''):
        raise ValueError('Delay must be blank iff no IP packets were received')
    for k in ['delay_ip_ms', 'goodput_active_kbps', 'goodput_full_kbps', 'throughput_ip_kbps']:
        if r[k] and (not math.isfinite(float(r[k])) or float(r[k]) < 0):
            raise ValueError('Invalid metric ' + k)
    if not (0 <= int(r['app_rx_active_bytes']) <= int(r['app_rx_bytes']) <= int(r['app_tx_accepted_bytes'])):
        raise ValueError('Invalid application byte counts')
    with Path(str(prefix) + '-flows.csv').open() as f:
        flows = list(csv.DictReader(f))
    if len(flows) != stations or {int(x['station']) for x in flows} != set(range(stations)):
        raise ValueError('Missing or duplicate station records')
    for k in ['tx_ip_packets', 'rx_ip_packets', 'unreceived_ip_packets', 'rx_ip_bytes',
              'app_tx_accepted_bytes', 'app_rx_bytes', 'app_rx_active_bytes']:
        if sum(int(x[k]) for x in flows) != int(r[k]):
            raise ValueError('Flow/summary mismatch: ' + k)
    for item in [r] + flows:
        sent, received = int(item['tx_ip_packets']), int(item['rx_ip_packets'])
        if not 0 <= received <= sent or int(item['unreceived_ip_packets']) != sent-received:
            raise ValueError('Invalid per-station IP counts')
        if not 0 <= int(item['app_rx_active_bytes']) <= int(item['app_rx_bytes']) <= int(item['app_tx_accepted_bytes']):
            raise ValueError('Invalid per-station application counts')
        for key, expected in [('pdr_ip_pct', 100*received/sent if sent else None),
                              ('loss_ip_pct', 100*(sent-received)/sent if sent else None)]:
            if expected is None:
                if item[key] != '':
                    raise ValueError('Metric must be blank without transmissions: '+key)
            elif not math.isclose(float(item[key]), expected, rel_tol=1e-8, abs_tol=1e-8):
                raise ValueError('Invalid per-station '+key)
        if (received == 0) != (item['delay_ip_ms'] == ''):
            raise ValueError('Invalid per-station delay availability')
        if received and (not math.isfinite(float(item['delay_ip_ms'])) or float(item['delay_ip_ms']) < 0):
            raise ValueError('Invalid delay')
        active = float(r['duration_s'])
        full = active + float(r['cleanup_s'])
        for key, byte_key, seconds in [('goodput_active_kbps','app_rx_active_bytes',active),
                                        ('goodput_full_kbps','app_rx_bytes',full),
                                        ('throughput_ip_kbps','rx_ip_bytes',full)]:
            if not math.isclose(float(item[key]), int(item[byte_key])*8/(seconds*1000), rel_tol=1e-8, abs_tol=1e-8):
                raise ValueError('Invalid rate formula: '+key)
    for key, field in [('zero_tx_flows','tx_ip_packets'), ('zero_rx_flows','rx_ip_packets'),
                       ('zero_app_rx_flows','app_rx_bytes')]:
        if key in r and int(r[key]) != sum(int(f[field]) == 0 for f in flows):
            raise ValueError('Invalid counter: '+key)
    if 'associated_pairs_at_start' in r:
        flags = [(int(f['sta_associated_at_start']), int(f['ap_associated_at_start'])) for f in flows]
        if any(a not in (0,1) or b not in (0,1) for a,b in flags):
            raise ValueError('Invalid association flags')
        if int(r['associated_pairs_at_start']) != sum(a and b for a,b in flags):
            raise ValueError('Invalid associated-pair counter')
        if int(r['association_mismatches_at_start']) != sum(a != b for a,b in flags):
            raise ValueError('Invalid association-mismatch counter')
    if rx and not math.isclose(float(r['delay_ip_ms']),
            sum(float(f['delay_ip_ms'] or 0)*int(f['rx_ip_packets']) for f in flows)/rx,
            rel_tol=1e-8, abs_tol=1e-8):
        raise ValueError('Incorrect weighted mean delay')
    if r.get('schema_version') == '3':
        if 'jains_fairness_active' in r and r['jains_fairness_active'] != '':
            jfi = float(r['jains_fairness_active'])
            if not (0.0 <= jfi <= 1.0 + 1e-8):
                raise ValueError('Invalid Jain fairness index')
        for item in [r] + flows:
            ftx, frx = int(item['app_frames_tx']), int(item['app_frames_rx'])
            fcrc, fooo = int(item['app_frames_crc_ok']), int(item['app_frames_out_of_order'])
            if not (0 <= frx <= ftx and 0 <= fcrc <= frx and 0 <= fooo <= frx):
                raise ValueError('Invalid application frame counters')
            if (frx == 0) != (item['app_delay_ms'] == ''):
                raise ValueError('Invalid app_delay_ms availability')
    return r


def save_progress(out, statuses, rows):
    """Atomic checkpoints after every case, including interruption/failure."""
    temp = out/'status.json.tmp'
    temp.write_text(json.dumps(statuses, indent=2, ensure_ascii=False)+'\n')
    temp.replace(out/'status.json')
    if rows:
        temp = out/'runs.csv.tmp'
        with temp.open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        temp.replace(out/'runs.csv')


def warnings(row):
    result = []
    if int(row.get('zero_app_rx_flows', 0)):
        result.append(f"{row['zero_app_rx_flows']} trạm không nhận byte ứng dụng")
    if 'associated_pairs_at_start' in row and int(row['associated_pairs_at_start']) < int(row['stations']):
        result.append('chưa đủ liên kết STA/AP ở thời điểm bắt đầu')
    return result


def annotate_csvs(prefix, row):
    """Keep each per-station CSV interpretable even when copied out of its folder."""
    metadata = {k: row[k] for k in ['transport', 'stations', 'seed', 'run']}
    for suffix in ['flows', 'positions']:
        path = Path(str(prefix) + '-' + suffix + '.csv')
        with path.open() as f:
            reader = csv.DictReader(f)
            fields = list(reader.fieldnames)
            records = list(reader)
        fields = list(metadata) + [k for k in fields if k not in metadata]
        with path.open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(dict(r, **metadata) for r in records)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['smoke', 'pilot', 'matrix', 'single'])
    p.add_argument('--ns3', default=os.environ.get('NS3_ROOT', str(Path.home()/'ns-allinone-3.35/ns-3.35')))
    p.add_argument('--out', help='New output directory; existing directories are never overwritten')
    p.add_argument('--resume', action='store_true', help='Continue this exact configuration; keep verified successful cases')
    p.add_argument('--runs', type=int, help='Repetitions per configuration (pilot default 5; matrix requires this)')
    p.add_argument('--stations', type=int, nargs='+', default=[5, 10, 20])
    p.add_argument('--transport', choices=['tcp', 'udp'], nargs='+', default=['udp', 'tcp'])
    p.add_argument('--seed', type=int, default=12345)
    p.add_argument('--run-start', type=int, default=1)
    p.add_argument('--duration', type=float, default=None)
    p.add_argument('--cleanup', type=float, default=10)
    p.add_argument('--start', type=float, default=3)
    p.add_argument('--rate-mbps', type=float, default=1)
    p.add_argument('--packet-size', type=int, default=1024)
    p.add_argument('--half-side', type=float, default=25)
    p.add_argument('--speed', type=float, default=1.5)
    p.add_argument('--mobility', choices=['moving', 'static'], default='moving')
    p.add_argument('--app-mode', choices=['onoff', 'socket'], default='onoff',
                   help='onoff (baseline schema v2) or socket (24B binary header, schema v3)')
    p.add_argument('--tcp-variant', choices=['NewReno', 'Cubic', 'Bbr'], default='NewReno')
    p.add_argument('--rts-cts', action='store_true', help='Enable Wi-Fi RTS/CTS (threshold=500B)')
    p.add_argument('--live-udp-host', default='127.0.0.1')
    p.add_argument('--live-udp-port', type=int, default=0, help='Optional live UDP telemetry sink port')
    p.add_argument('--tx-power', type=float, default=16)
    p.add_argument('--jobs', type=int, default=2, help='Compiler jobs; simulations run sequentially')
    p.add_argument('--timeout', type=int, default=900, help='Wall-clock timeout per simulation, seconds')
    p.add_argument('--xml', action='store_true')
    p.add_argument('--pcap', action='store_true')
    p.add_argument('--visual', action='store_true', help='Record NetAnim XML with colored/labeled AP and STAs')
    a = p.parse_args()
    ns = Path(a.ns3).expanduser().resolve()
    if not (ns/'waf').is_file() or not (ns/'VERSION').is_file():
        p.error('Cannot find ns-3 root with waf and VERSION: ' + str(ns))
    if (ns/'VERSION').read_text().strip() != '3.35':
        p.error('This package targets exactly ns-3.35')
    if a.mode == 'matrix' and a.runs is None:
        p.error('Choose --runs explicitly after inspecting the pilot')
    runs = a.runs if a.runs is not None else (5 if a.mode == 'pilot' else 1)
    if runs < 1 or a.run_start < 1 or a.seed < 1 or a.jobs < 1 or a.timeout < 1:
        p.error('runs, run-start, seed, jobs and timeout must be positive')
    if len(set(a.stations)) != len(a.stations) or any(n < 1 or n > 200 for n in a.stations):
        p.error('stations must be distinct values in 1..200')
    duration = a.duration if a.duration is not None else (5 if a.mode == 'smoke' else 30)
    for label, value in [('duration', duration), ('rate-mbps', a.rate_mbps), ('half-side', a.half_side), ('speed', a.speed)]:
        if not math.isfinite(value) or value <= 0:
            p.error(label + ' must be finite and positive')
    if not math.isfinite(a.cleanup) or a.cleanup < 0 or not math.isfinite(a.start) or a.start < 1:
        p.error('cleanup must be >=0 and start must be >=1')
    if not math.isfinite(a.tx_power) or not 64 <= a.packet_size <= 1400 or a.rate_mbps > 100:
        p.error('Invalid tx-power, packet-size or rate-mbps')
    stations = [5] if a.mode == 'smoke' else a.stations
    protocols = list(dict.fromkeys(a.transport))
    if a.mode == 'single' and (len(stations) != 1 or len(protocols) != 1 or runs != 1):
        p.error('single requires --stations N --transport tcp|udp and one run')
    if a.mode == 'smoke':
        runs = 1
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    out = Path(a.out).expanduser().resolve() if a.out else ROOT/'results'/(a.mode+'-'+stamp)
    if a.resume and not a.out:
        p.error('--resume requires --out')
    existing = out.exists()
    if existing and not a.resume:
        p.error('Output exists; use a new directory or --resume with the identical configuration')
    # Read-only validation first, then apply both known fixes (with backups).
    core = repair(ns)
    out.mkdir(parents=True, exist_ok=a.resume)
    source = ROOT/'src'/(NAME+'.cc')
    dest = ns/'scratch'/(NAME+'.cc')
    config = vars(a).copy()
    config.update(ns3=str(ns), ns3_version='3.35', out=str(out), runs=runs,
                  duration=duration, stations=stations, transport=protocols,
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  release=RELEASE, core_patch=core,
                  created_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  argv=sys.argv, platform=sys.platform, python=sys.version)
    cache = ns/'build/c4che/_cache.py'
    config['build_config_sha256'] = hashlib.sha256(cache.read_bytes()).hexdigest() if cache.exists() else None
    if existing:
        previous = json.loads((out/'experiment.json').read_text())
        ignored = {'out','resume','created_utc','argv','jobs','timeout','python','platform','core_patch','live_udp_host','live_udp_port'}
        different = [k for k in config if k not in ignored and config[k] != previous.get(k)]
        if core['files'] != previous.get('core_patch', {}).get('files'):
            different.append('ns-3 core source hashes')
        if different:
            p.error('Cannot mix different code/settings in a resumed run: '+', '.join(different))
        config = previous
    else:
        (out/'experiment.json').write_text(json.dumps(config, indent=2, ensure_ascii=False)+'\n')
    # Preserve any pre-existing user edits in scratch before updating our target.
    if dest.exists() and dest.read_bytes() != source.read_bytes():
        backup = out/(NAME+'.previous-'+stamp+'.cc')
        shutil.copy2(dest, backup)
    shutil.copy2(source, dest)
    shutil.copy2(source, out/(NAME+'.cc'))
    print('Kết quả:', out, flush=True)
    print('Đang biên dịch; log: build.log', flush=True)
    with (out/'build.log').open('w') as log:
        rc = subprocess.run([sys.executable, str(ns/'waf'), 'build', '--targets='+NAME,
                             '-j', str(a.jobs)], cwd=ns, stdout=log, stderr=subprocess.STDOUT).returncode
    if rc:
        print('Biên dịch lỗi. Gửi build.log; chưa chạy mô phỏng.', file=sys.stderr)
        return 1
    cases = [(n, run, proto) for n in stations for run in range(a.run_start, a.run_start+runs) for proto in protocols]
    old_status = {s['case']:s for s in json.loads((out/'status.json').read_text())} if (out/'status.json').exists() else {}
    saved_rows = {}
    expected_cases = {f'{proto}-n{n}-r{run}' for n,run,proto in cases}
    if set(old_status) - expected_cases:
        raise ValueError('Unexpected saved case IDs')
    for n, run, proto in cases:
        name = f'{proto}-n{n}-r{run}'
        if old_status.get(name, {}).get('status') == 'ok':
            row = validate(out/name/'result', n)
            if (row['transport'], int(row['seed']), int(row['run'])) != (proto, a.seed, run):
                raise ValueError('Saved case identity mismatch: '+name)
            saved_rows[name] = dict(row, case=name)
    statuses, valid = [], []
    def checkpoint():
        seen = {s['case'] for s in statuses}
        later = [s for name,s in old_status.items() if name not in seen]
        later_rows = [row for name,row in saved_rows.items() if name not in seen]
        save_progress(out, statuses+later, valid+later_rows)

    for index, (n, run, proto) in enumerate(cases, 1):
        case = out/f'{proto}-n{n}-r{run}'
        prefix = case/'result'
        if old_status.get(case.name, {}).get('status') == 'ok':
            row = saved_rows[case.name]
            valid.append(row)
            statuses.append(old_status[case.name])
            print(f'[{index}/{len(cases)}] Giữ lượt đã xác minh: {case.name}', flush=True)
            continue
        if case.exists():
            archive = out/'failed-attempts'
            archive.mkdir(exist_ok=True)
            case.rename(archive/(case.name+'-'+stamp))
        case.mkdir()
        options = dict(stations=n, transport=proto, appMode=a.app_mode,
                       tcpVariant=a.tcp_variant, rtsCts=int(a.rts_cts),
                       seed=a.seed, run=run, duration=duration,
                       cleanup=a.cleanup, start=a.start, rateMbps=a.rate_mbps,
                       packetSize=a.packet_size, halfSide=a.half_side, speed=a.speed,
                       mobility=a.mobility, txPower=a.tx_power, output=str(prefix),
                       xml=int(a.xml), pcap=int(a.pcap), visual=int(a.visual))
        if a.live_udp_port > 0:
            options.update(liveUdpHost=a.live_udp_host, liveUdpPort=a.live_udp_port)
        target = shlex.join([NAME] + [f'--{k}={v}' for k, v in options.items()])
        command = [sys.executable, str(ns/'waf'), '--run-no-build', target]
        (case/'config.json').write_text(json.dumps({'options':options, 'command':command,
            'source_sha256':config['source_sha256'], 'core_patch':core}, indent=2)+'\n')
        print(f'[{index}/{len(cases)}] {proto.upper()} / {n} trạm / run {run}', flush=True)
        status = dict(case=case.name, status='running', message='')
        statuses.append(status)
        checkpoint()
        try:
            with (case/'run.log').open('w') as log:
                process = subprocess.Popen(command, cwd=ns, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    rc = process.wait(timeout=a.timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise
                result = subprocess.CompletedProcess(command, rc)
            if result.returncode:
                raise RuntimeError('Simulation exit code ' + str(result.returncode))
            row = validate(prefix, n)
            if (row['transport'], int(row['seed']), int(row['run'])) != (proto, a.seed, run):
                raise ValueError('Simulation identity mismatch')
            annotate_csvs(prefix, row)
            row['case'] = case.name
            valid.append(row)
            status.update(status='ok', message='', warnings=warnings(row))
            print(f"  PDR(IP)={float(row['pdr_ip_pct']):.2f}% | goodput={float(row['goodput_active_kbps']):.2f} Kbps", flush=True)
            for note in status['warnings']:
                print('  LƯU Ý:', note, flush=True)
        except KeyboardInterrupt:
            status.update(status='interrupted', message='Stopped by user; resume is available')
            checkpoint()
            print('\nĐã lưu tiến độ. Chạy lại cùng lệnh và thêm --resume.', flush=True)
            return 130
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
            status.update(status='error', message=str(exc))
            print('  LỖI:', exc, flush=True)
        checkpoint()
    save_progress(out, statuses, valid)
    failed = sum(s['status'] != 'ok' for s in statuses)
    print(f'Hoàn tất: {len(valid)} thành công, {failed} lỗi. Thư mục: {out}', flush=True)
    print('Vẽ biểu đồ: python3 scripts/analyze.py ' + shlex.quote(str(out)), flush=True)
    return 1 if failed else 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as e:
        print('Lỗi:', e, file=sys.stderr)
        sys.exit(1)
