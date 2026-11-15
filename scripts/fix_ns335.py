#!/usr/bin/env python3
"""Two narrow ns-3.36 Wi-Fi fixes backported to ns-3.35, with backups.

References (official ns-3 source):
https://github.com/nsnam/ns-3-dev-git/blob/ns-3.36/src/wifi/model/frame-exchange-manager.cc
https://github.com/nsnam/ns-3-dev-git/blob/ns-3.36/src/wifi/model/ap-wifi-mac.cc

No assertion is disabled, no association is forced, and no RNG/ARP parameter
is changed. Validate all three files before writing any of them.
"""
import argparse
from datetime import datetime
import difflib
import hashlib
from pathlib import Path
import re
import shutil

PATCH_ID = 'ns335-wifi-two-fixes-v1'
FILES = ['frame-exchange-manager.cc', 'ap-wifi-mac.cc', 'ap-wifi-mac.h']


def transform(files):
    result = files.copy()
    replacements = [
        ('ap-wifi-mac.h',
         'void TxFailed (uint8_t timeoutReason, Ptr<const WifiMacQueueItem> mpdu, const WifiTxVector& txVector);',
         'void TxFailed (WifiMacDropReason timeoutReason, Ptr<const WifiMacQueueItem> mpdu);'),
        ('ap-wifi-mac.cc',
         'ApWifiMac::TxFailed (uint8_t timeoutReason, Ptr<const WifiMacQueueItem> mpdu, const WifiTxVector& txVector)\n{\n  NS_LOG_FUNCTION (this << +timeoutReason << *mpdu << txVector);',
         'ApWifiMac::TxFailed (WifiMacDropReason timeoutReason, Ptr<const WifiMacQueueItem> mpdu)\n{\n  NS_LOG_FUNCTION (this << +timeoutReason << *mpdu);'),
        ('ap-wifi-mac.cc',
         'TraceConnectWithoutContext ("MpduResponseTimeout", MakeCallback (&ApWifiMac::TxFailed, this))',
         'TraceConnectWithoutContext ("DroppedMpdu", MakeCallback (&ApWifiMac::TxFailed, this))'),
    ]
    states = []
    for name, old, new in replacements:
        text = files[name]
        if text.count(old) == 1 and new not in text:
            states.append('old')
        elif text.count(new) == 1 and old not in text:
            states.append('new')
        else:
            raise ValueError('Mã AP khác mẫu dự kiến; chưa sửa file: ' + name)
    if len(set(states)) != 1:
        raise ValueError('Bản sửa AP chưa đầy đủ; dừng để tránh ghép mã không nhất quán.')
    if states[0] == 'old':
        for name, old, new in replacements:
            result[name] = result[name].replace(old, new)

    name = 'frame-exchange-manager.cc'
    text = files[name]
    begin = text.index('FrameExchangeManager::NotifyInternalCollision (Ptr<Txop> txop)')
    end = text.find('\nvoid\n', begin)
    if end == -1:
        raise ValueError('Không tìm thấy cuối hàm NotifyInternalCollision')
    block = text[begin:end]
    report = 'm_mac->GetWifiRemoteStationManager ()->ReportDataFailed (mpdu);'
    guarded = ('if (mpdu->GetHeader ().HasData ()\n'
               '          && !mpdu->GetHeader ().GetAddr1 ().IsGroup ())\n'
               '        {\n          ' + report + '\n        }')
    # Accept the same earlier manual fix despite differences in whitespace.
    compact = re.sub(r'\s+', '', block)
    protected = re.sub(r'\s+', '', guarded)
    if compact.count(protected) == 1 and block.count(report) == 1:
        pass
    elif block.count(report) == 1 and 'HasData' not in block:
        anchor = 'if (mpdu != nullptr)\n    {\n      ' + report
        if block.count(anchor) != 1:
            raise ValueError('Hàm va chạm khác ns-3.35 dự kiến; chưa sửa file.')
        block = block.replace(anchor, 'if (mpdu != nullptr)\n    {\n      ' + guarded)
        result[name] = text[:begin] + block + text[end:]
    else:
        raise ValueError('Không xác minh được bản sửa va chạm; chưa sửa file.')
    return result


def repair(root, check=False):
    root = Path(root).expanduser().resolve()
    if (root/'VERSION').read_text().strip() != '3.35':
        raise ValueError('Chỉ hỗ trợ đúng ns-3.35')
    model = root/'src/wifi/model'
    original = {name: (model/name).read_text() for name in FILES}
    patched = transform(original)
    changed = [name for name in FILES if patched[name] != original[name]]
    if check and changed:
        raise ValueError('Chưa đủ hai bản sửa Wi-Fi. Chạy scripts/fix_ns335.py trước.')
    backup = None
    if changed:
        backup = root/('backup-wifi-study-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir()
        for name in FILES:
            shutil.copy2(model/name, backup/name)
        diff = ''.join(''.join(difflib.unified_diff(original[name].splitlines(True),
                   patched[name].splitlines(True), fromfile='a/src/wifi/model/'+name,
                   tofile='b/src/wifi/model/'+name)) for name in changed)
        (backup/'fixes.patch').write_text(diff)
        try:
            for name in changed:
                (model/name).write_text(patched[name])
        except BaseException:
            for name in changed:
                shutil.copy2(backup/name, model/name)
            raise
        print('Đã áp dụng bản sửa Wi-Fi; bản sao lưu:', backup, flush=True)
    return dict(patch_id=PATCH_ID, backup=str(backup) if backup else None,
                files={name: hashlib.sha256((model/name).read_bytes()).hexdigest() for name in FILES})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ns3', nargs='?', default=str(Path.home()/'ns-allinone-3.35/ns-3.35'))
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        info = repair(args.ns3, check=args.check)
        print('Đã xác minh đủ hai bản sửa:', info['patch_id'])
    except (OSError, ValueError) as exc:
        raise SystemExit('Lỗi: ' + str(exc))
