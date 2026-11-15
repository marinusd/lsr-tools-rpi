#!/usr/bin/env python3
"""Parser for the log file format written by the F.A.S.T. PC software (spec.md 3.C).

The collector uses only the header of such a file, as its channel template.
The mock ECU also uses the records, to replay them.

A channel template can also be a text file (see TEMPLATE_COLUMNS below), which
is easier to read and to edit than the header of a binary log.

usage: ecu_log.py FILE                    print a summary of a log file
       ecu_log.py FILE --template OUT     write its channel table as a text template
"""
import csv
import io
from decimal import Decimal
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


# ---- text channel template ---------------------------------------------------
TEMPLATE_COLUMNS = ['name', 'unit', 'index', 'id', 'conv', 'scale', 'offset',
                    'display_min', 'display_max', 'format']


def _f32_text(value):
    """Shortest decimal text that reads back as the same 32-bit float."""
    for digits in range(1, 10):
        text = f'{value:.{digits}g}'
        if struct.pack('<f', float(text)) == struct.pack('<f', value):
            return format(Decimal(text), 'f')
    return repr(value)


def _f32(text):
    """Read a number the way the binary log stores it: as a 32-bit float."""
    return struct.unpack('<f', struct.pack('<f', float(text)))[0]


def template_text(channels, source=''):
    """The channels as a text template. Line order is CSV column order."""
    out = io.StringIO()
    out.write('# ECU channel template for collector.py (ECU_CHANNEL_TEMPLATE).\n')
    if source:
        out.write(f'# Extracted from {source}.\n')
    out.write('#\n'
              '# One line per channel. The order of the lines is the order of the ECU\n'
              '# columns in the CSV files. To leave a channel out, delete its line or put\n'
              '# a # in front of it. The ECU accepts 11 channels at most until a longer\n'
              '# poll table has been confirmed (spec.md 3.D.2).\n'
              '#\n'
              '#   name, unit    as shown by the PC software\n'
              '#   index         the PC software\'s channel number; sets the poll-table order\n'
              '#   id            ECU RAM address of the value (hex)\n'
              '#   conv          conversion code (spec.md 3.C.6)\n'
              '#   scale, offset value = raw x scale + offset, for conv 0, 4 and 5\n'
              '#   display_min, display_max, format   range and printf format for display\n'
              '#\n'
              '# Do not change index, id, conv, scale or offset: they describe the ECU.\n')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(TEMPLATE_COLUMNS)
    for c in channels:
        writer.writerow([c.name, c.unit, c.index, f'0x{c.id:02X}', c.conv, _f32_text(c.scale),
                         _f32_text(c.offset), _f32_text(c.disp_min), _f32_text(c.disp_max),
                         c.fmt])
    return out.getvalue()


def parse_template_text(text):
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith('#')]
    rows = list(csv.reader(lines))
    if not rows or [c.strip() for c in rows[0]] != TEMPLATE_COLUMNS:
        raise FormatError('text template must start with the line: ' + ','.join(TEMPLATE_COLUMNS))
    channels = []
    for number, row in enumerate(rows[1:], 1):
        if len(row) != len(TEMPLATE_COLUMNS):
            raise FormatError(f'channel line {number}: expected {len(TEMPLATE_COLUMNS)} '
                              f'fields, found {len(row)}')
        r = [c.strip() for c in row]
        try:
            channels.append(Channel(r[0], r[1], int(r[3], 0), int(r[4]), _f32(r[5]),
                                    _f32(r[6]), 0, 255, _f32(r[8]), _f32(r[7]), r[9],
                                    int(r[2])))
        except ValueError as e:
            raise FormatError(f'channel line {number} ({r[0]}): {e}')
    if not 1 <= len(channels) <= 64:
        raise FormatError(f'{len(channels)} channels; need between 1 and 64')
    return channels


def load_template(path):
    """Read a channel template, text or binary log. Returns (channels, warning or None)."""
    buf = read_bytes(path)
    try:
        text = buf.decode('utf-8')
        is_text = '\0' not in text
    except UnicodeDecodeError:
        is_text = False
    if is_text:
        return parse_template_text(text), None
    hdr = parse_header(buf, need_data_header=False)
    note = None if version_tag(buf) == b'22' else \
        f'version tag is {version_tag(buf)!r}, expected "22"'
    return hdr.channels, note


def poll_order(channels):
    """Positions of the channels in poll-table order (ascending channel index)."""
    return sorted(range(len(channels)), key=lambda k: channels[k].index)


def main():
    buf = read_bytes(sys.argv[1])
    hdr = parse_header(buf)
    if sys.argv[2:3] == ['--template']:
        with open(sys.argv[3], 'w') as f:
            f.write(template_text(hdr.channels, sys.argv[1]))
        print(f'wrote {len(hdr.channels)} channels to {sys.argv[3]}')
        return
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
