#!/usr/bin/env python3
"""Parser for the log file format written by the F.A.S.T. PC software (spec.md 3.C).

The collector uses only the header of such a file, as its channel template.
The mock ECU also uses the records, to replay them.

usage: ecu_log.py FILE      print a summary of the file
"""
import re
import struct
import sys
from collections import namedtuple

CHANNEL_TABLE_OFFSET = 516
CHANNEL_SIZE = 58
DATA_HEADER_SIZE = 18

Channel = namedtuple('Channel', 'name unit id conv scale offset raw_min raw_max '
                                'disp_max disp_min fmt index')
Header = namedtuple('Header', 'channels record_size record_count data_start')


class FormatError(ValueError):
    pass


def _text(raw):
    return raw.split(b'\0', 1)[0].decode('ascii', 'replace').strip()


def parse_header(buf, need_data_header=True):
    """Return a Header. Raises FormatError if buf is not a usable log header."""
    if len(buf) < 4:
        raise FormatError('file too short')
    n = struct.unpack_from('<H', buf, 2)[0]
    if not 1 <= n <= 64:
        raise FormatError(f'channel count {n} is not between 1 and 64')
    table_end = CHANNEL_TABLE_OFFSET + CHANNEL_SIZE * n
    if len(buf) < table_end:
        raise FormatError('file ends inside the channel table')
    index = struct.unpack_from(f'<{n}H', buf, 4)
    channels = []
    for i in range(n):
        f = struct.unpack_from('<16s10sHHffHHff8s', buf,
                               CHANNEL_TABLE_OFFSET + CHANNEL_SIZE * i)
        channels.append(Channel(_text(f[0]), _text(f[1]), *f[2:10], _text(f[10]), index[i]))
    record_size, record_count = 4 + 2 * n, 0
    if len(buf) >= table_end + DATA_HEADER_SIZE:
        record_size, _, record_count = struct.unpack_from('<HHI', buf, table_end)
        if record_size != 4 + 2 * n:
            raise FormatError(f'record size {record_size} does not fit {n} channels')
    elif need_data_header:
        raise FormatError('file ends before the data block header')
    return Header(channels, record_size, record_count, table_end + DATA_HEADER_SIZE)


def version_tag(buf):
    return buf[:2]


def records(buf, hdr):
    """Return the complete records as tuples (ms, raw0, raw1, ...)."""
    n = len(hdr.channels)
    available = max(0, len(buf) - hdr.data_start) // hdr.record_size
    count = min(hdr.record_count, available)
    return [struct.unpack_from(f'<I{n}H', buf, hdr.data_start + k * hdr.record_size)
            for k in range(count)]


def decimals(fmt, default=2):
    """Decimal places of a printf format such as '%5.2f'."""
    m = re.search(r'%[-+ 0#]*\d*\.(\d+)[fF]', fmt)
    return int(m.group(1)) if m else default


def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def poll_order(channels):
    """Positions of the channels in poll-table order (ascending channel index)."""
    return sorted(range(len(channels)), key=lambda k: channels[k].index)


def main():
    buf = read_bytes(sys.argv[1])
    hdr = parse_header(buf)
    recs = records(buf, hdr)
    if version_tag(buf) != b'22':
        print(f'warning: version tag is {version_tag(buf)!r}, expected b"22"')
    print(f'{len(hdr.channels)} channels, record size {hdr.record_size}, '
          f'{hdr.record_count} records in header, {len(recs)} complete')
    if recs:
        print(f'first timestamp {recs[0][0]} ms, last timestamp {recs[-1][0]} ms')
    for i, c in enumerate(hdr.channels):
        first = f'  first raw {recs[0][1 + i]}' if recs else ''
        print(f'{i:2d} {c.name:<16} idx {c.index:2d} id 0x{c.id:02X} conv {c.conv:2d} '
              f'scale {c.scale:<9.6g} offset {c.offset:<6g} {c.fmt}{first}')
    print('poll table:', ' '.join(f'{hdr.channels[k].id:02X}' for k in poll_order(hdr.channels)), '00')


if __name__ == '__main__':
    main()
