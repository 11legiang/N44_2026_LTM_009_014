#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
N44 Binary Application Protocol (24-byte Big-Endian Header + UTF-8 Payload).
Matches the exact wire format implemented in src/wifi-transport-study.cc:

  Offset | Size | Field      | Type    | Description
  -------+------+------------+---------+--------------------------------------
    0    |  2B  | magic      | char[2] | b"N4"
    2    |  1B  | version    | uint8   | Protocol version (1)
    3    |  1B  | msg_type   | uint8   | 1 = DATA telemetry, 2 = ALERT
    4    |  2B  | station_id | uint16  | Source STA index (0..200)
    6    |  2B  | payload_len| uint16  | Payload byte count
    8    |  4B  | seq_num    | uint32  | Per-station monotonic sequence
   12    |  8B  | tx_time_ns | uint64  | Sender timestamp (ns)
   20    |  4B  | crc32      | uint32  | IEEE 802.3 CRC32 over [0..20)+payload
"""

import json
import struct
import time
import zlib
from typing import Dict, Any, Optional, Tuple

HEADER_FORMAT = "!2sBBHHIQI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 24 bytes
MAGIC = b"N4"
VERSION = 1
MSG_DATA = 1
MSG_ALERT = 2


def compute_crc32(header_prefix_20b: bytes, payload: bytes) -> int:
    crc = zlib.crc32(header_prefix_20b)
    return zlib.crc32(payload, crc) & 0xFFFFFFFF


def pack_frame(
    station_id: int,
    seq_num: int,
    payload_obj: Optional[Dict[str, Any]] = None,
    raw_payload: Optional[bytes] = None,
    msg_type: int = MSG_DATA,
    tx_time_ns: Optional[int] = None,
    pad_to_bytes: int = 0,
) -> bytes:
    if tx_time_ns is None:
        tx_time_ns = time.time_ns()
    if raw_payload is None:
        payload = json.dumps(payload_obj or {}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    else:
        payload = raw_payload
    if pad_to_bytes > HEADER_SIZE + len(payload):
        payload = payload + b" " * (pad_to_bytes - HEADER_SIZE - len(payload))
    prefix20 = struct.pack(
        "!2sBBHHIQ",
        MAGIC,
        VERSION,
        msg_type,
        station_id & 0xFFFF,
        len(payload) & 0xFFFF,
        seq_num & 0xFFFFFFFF,
        tx_time_ns & 0xFFFFFFFFFFFFFFFF,
    )
    crc = compute_crc32(prefix20, payload)
    return prefix20 + struct.pack("!I", crc) + payload


def try_unpack_stream(buf: bytearray) -> Tuple[Optional[Dict[str, Any]], int]:
    """
    Attempt to parse one complete N44 frame from the front of `buf`.
    Returns (frame_dict_or_None, bytes_consumed).
    Handles partial TCP reads and magic resynchronization.
    """
    n = len(buf)
    idx = 0
    while idx + HEADER_SIZE <= n:
        if buf[idx : idx + 2] != MAGIC or buf[idx + 2] != VERSION:
            idx += 1
            continue
        hdr = bytes(buf[idx : idx + HEADER_SIZE])
        magic, ver, msg_type, sta_id, plen, seq, tx_ns, rx_crc = struct.unpack(HEADER_FORMAT, hdr)
        total = HEADER_SIZE + plen
        if idx + total > n:
            return None, idx  # Wait for remaining bytes on TCP stream
        payload = bytes(buf[idx + HEADER_SIZE : idx + total])
        expected_crc = compute_crc32(hdr[:20], payload)
        crc_ok = (rx_crc == expected_crc)
        decoded_json = None
        raw_text = payload.decode("utf-8", errors="replace").strip()
        if raw_text.startswith("{"):
            try:
                decoded_json = json.loads(raw_text)
            except ValueError:
                decoded_json = None
        return {
            "magic": magic.decode("ascii", errors="replace"),
            "version": ver,
            "msg_type": msg_type,
            "station_id": sta_id,
            "payload_len": plen,
            "seq_num": seq,
            "tx_time_ns": tx_ns,
            "rx_crc32": f"0x{rx_crc:08X}",
            "crc_ok": crc_ok,
            "header_hex": hdr.hex(" ").upper(),
            "payload": decoded_json if decoded_json is not None else raw_text[:80],
            "frame_bytes": total,
        }, idx + total
    return None, max(0, idx)
