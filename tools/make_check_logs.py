#!/usr/bin/env python3
"""Write test logs in the C-Com WP format, to check that C-Com WP opens files
written by parse_ecu_log.header_bytes() and record_bytes().

Each file reuses the records of a real log, but differs from anything C-Com WP
has written in the way the collector's own files will.

usage: make_check_logs.py SOURCE.log OUT_DIR
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parse_ecu_log as p                                   # noqa: E402

SUBSET = ['RPM', 'TPS (%)', 'MAP (PSIA)', 'Coolant(*F)', 'Spark(*BTDC)']


def write(path, channels, recs, count):
    with open(path, 'wb') as f:
        f.write(p.header_bytes(channels, count))
        for r in recs:
            f.write(p.record_bytes(r[0], r[1:]))
    print(f'{os.path.basename(path)}: {len(channels)} channels, {len(recs)} records, '
          f'header count {count}')


def main():
    source, out = sys.argv[1], sys.argv[2]
    buf = p.read_bytes(source)
    hdr = p.parse_header(buf)
    recs = p.records(buf, hdr)
    names = [c.name for c in hdr.channels]
    os.makedirs(out, exist_ok=True)
    base = os.path.splitext(os.path.basename(source))[0]

    # A: a channel list C-Com WP never saved: 5 of the channels, in another order
    pick = [names.index(n) for n in SUBSET]
    write(os.path.join(out, f'check_A_{base}_5_channels.log'),
          [hdr.channels[k] for k in pick],
          [(r[0],) + tuple(r[1 + k] for k in pick) for r in recs], len(recs))
    # B: power cut after the records were written but before the count was updated
    write(os.path.join(out, f'check_B_{base}_count_behind.log'),
          hdr.channels, recs, len(recs) - 10)
    # C: power cut before the first count update
    write(os.path.join(out, f'check_C_{base}_count_zero.log'), hdr.channels, recs, 0)


if __name__ == '__main__':
    main()
