#!/usr/bin/env python3
"""Parser for the log file format written by the F.A.S.T. PC software (spec.md 3.C).

The collector uses only the header of such a file, as its channel template.
The mock ECU also uses the records, to replay them. header_bytes() and
record_bytes() write the format (see "writing" below).

A channel template can also be a text file (see TEMPLATE_COLUMNS below), which
is easier to read and to edit than the header of a binary log.

usage: parse_ecu_log.py FILE                    print a summary of a log file
       parse_ecu_log.py FILE --template OUT     write its channel table as a text template
       parse_ecu_log.py FILE --repair           set the record count to the records in
                                                the file (after a power cut)
"""
import csv
import io
import os
from decimal import Decimal
import re
import struct
import sys
from collections import namedtuple

CHANNEL_TABLE_OFFSET = 516
CHANNEL_SIZE = 58
DATA_HEADER_SIZE = 18

# flag: a u16 at offset 24 of each channel entry. Its meaning is unknown; in every
# sample it is 1, except for Coolant(*F) where it is 0. It is kept so that a log
# written by header_bytes() matches what the PC software writes.
Channel = namedtuple('Channel', 'name unit id conv scale offset raw_min raw_max '
                                'disp_max disp_min fmt index flag')
CHANNEL_STRUCT = '<16s8sHHHffHHff8s'     # name unit flag id conv scale offset ...
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
        f = struct.unpack_from(CHANNEL_STRUCT, buf, CHANNEL_TABLE_OFFSET + CHANNEL_SIZE * i)
        channels.append(Channel(_text(f[0]), _text(f[1]), *f[3:11], _text(f[11]), index[i],
                                f[2]))
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


# ---- writing ------------------------------------------------------------------
LAYOUT_OFFSET = 132
COUNT_OFFSET_IN_DATA_HEADER = 4


def _layout_block():
    """Bytes 132-515: display settings of the PC software. Identical in every sample."""
    block = bytearray(CHANNEL_TABLE_OFFSET - LAYOUT_OFFSET)
    struct.pack_into('<8H', block, 0, 15, 170, 325, 480, 15, 170, 325, 480)
    struct.pack_into('<8f', block, 260 - LAYOUT_OFFSET, *([420.0] * 4 + [440.0] * 4))
    return bytes(block)


LAYOUT_BLOCK = _layout_block()


def _field(text, size, what, channel):
    raw = text.encode('ascii')
    if len(raw) > size:
        raise FormatError(f'{channel}: {what} "{text}" is longer than {size} characters')
    return raw


def header_bytes(channels, record_count=0):
    """Everything up to the first record, as the PC software writes it."""
    n = len(channels)
    if not 1 <= n <= 64:
        raise FormatError(f'{n} channels; need between 1 and 64')
    out = bytearray(b'22' + struct.pack(f'<H{n}H', n, *(c.index for c in channels)))
    out += bytes(LAYOUT_OFFSET - len(out))
    out += LAYOUT_BLOCK
    for c in channels:
        out += struct.pack(CHANNEL_STRUCT, _field(c.name, 16, 'name', c.name),
                           _field(c.unit, 8, 'unit', c.name), c.flag, c.id, c.conv, c.scale,
                           c.offset, c.raw_min, c.raw_max, c.disp_max, c.disp_min,
                           _field(c.fmt, 8, 'format', c.name))
    out += struct.pack('<HHIHHHHH', 4 + 2 * n, 0, record_count, 8, 0, 650, 600, 1)
    return bytes(out)


def count_offset(channel_count):
    """File offset of the u32 record count, for updating it as records are added."""
    return (CHANNEL_TABLE_OFFSET + CHANNEL_SIZE * channel_count
            + COUNT_OFFSET_IN_DATA_HEADER)


def record_bytes(ms, raws):
    """One record: elapsed ms, then one raw value per channel in channel-table order."""
    return struct.pack(f'<I{len(raws)}H', ms, *raws)


class LogWriter:
    """Writes a log in the PC software's format, one record at a time.

    The PC software trusts the record count in the header and shows no records
    past it (tested 2026-11-17: a count of 0 shows no data). sync() therefore
    writes the records to disk first and then updates the count, so after a
    power cut the count is never ahead of the records, and at most the records
    since the last sync() are hidden. repair() makes those visible again.
    """

    def __init__(self, path, channels):
        self.path = path
        self.count = 0
        self._counted = 0
        self._count_offset = count_offset(len(channels))
        header = header_bytes(channels, 0)                 # checks the fields first
        self._file = open(path, 'xb')
        self._file.write(header)

    def add(self, ms, raws):
        self._file.write(record_bytes(ms, raws))
        self.count += 1

    def sync(self):
        self._file.flush()
        os.fsync(self._file.fileno())
        if self.count != self._counted:
            os.pwrite(self._file.fileno(), struct.pack('<I', self.count), self._count_offset)
            os.fsync(self._file.fileno())
            self._counted = self.count

    def close(self):
        try:
            self.sync()
        finally:
            self._file.close()


def repair(path):
    """Set the record count to the number of complete records. Returns (old, new)."""
    buf = read_bytes(path)
    hdr = parse_header(buf)
    complete = max(0, len(buf) - hdr.data_start) // hdr.record_size
    if complete != hdr.record_count:
        with open(path, 'r+b') as f:
            f.seek(count_offset(len(hdr.channels)))
            f.write(struct.pack('<I', complete))
    return hdr.record_count, complete


# ---- text channel template ---------------------------------------------------
TEMPLATE_COLUMNS = ['name', 'unit', 'index', 'id', 'conv', 'scale', 'offset',
                    'display_min', 'display_max', 'format', 'flag']


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
              '# For a dynamic list, set ECU_CHANNEL_TEMPLATE to a log file saved by C-Com WP\n'
              '# instead of this file: the channels and their order then come from that\n'
              '# log\'s header.\n'
              '#\n'
              '#   name, unit    as shown by the PC software\n'
              '#   index         the PC software\'s channel number; sets the poll-table order\n'
              '#   id            ECU RAM address of the value (hex)\n'
              '#   conv          conversion code (spec.md 3.C.6)\n'
              '#   scale, offset value = raw x scale + offset, for conv 0, 4 and 5\n'
              '#   display_min, display_max, format   range and printf format for display\n'
              '#   flag          copied from the PC software\'s log; meaning unknown\n'
              '#\n'
              '# Do not change index, id, conv, scale, offset or flag.\n')
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(TEMPLATE_COLUMNS)
    for c in channels:
        writer.writerow([c.name, c.unit, c.index, f'0x{c.id:02X}', c.conv, _f32_text(c.scale),
                         _f32_text(c.offset), _f32_text(c.disp_min), _f32_text(c.disp_max),
                         c.fmt, c.flag])
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
                                    int(r[2]), int(r[10])))
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
    if sys.argv[2:3] == ['--repair']:
        old, new = repair(sys.argv[1])
        print(f'{sys.argv[1]}: record count {old} -> {new}' if old != new else
              f'{sys.argv[1]}: record count {old} is already right')
        return
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
