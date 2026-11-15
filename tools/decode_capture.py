#!/usr/bin/env python3
"""Decode a USB capture (pcapng, LINKTYPE_USB_LINUX_MMAPPED) of a PL2303
serial session between C-Com WP and a F.A.S.T. ECU.

Prints one line per serial transaction (bytes sent, reply with the echo
removed) and, if a C-Com .log file is given, checks that its records are
the replies to the FC poll command.

usage: decode_capture.py capture.pcap [session.log]
"""
import struct
import sys

EP_OUT, EP_IN = 0x02, 0x83          # PL2303 bulk endpoints


def serial_events(path):
    """Yield (time_s, 'O'|'I'|'L', bytes) for serial data and DTR/RTS changes."""
    f = open(path, 'rb').read()
    pos, t0, last_line = 0, None, None
    while pos < len(f):
        btype, blen = struct.unpack_from('<II', f, pos)
        if btype == 6:                                   # enhanced packet block
            caplen = struct.unpack_from('<I', f, pos + 20)[0]
            d = f[pos + 28:pos + 28 + caplen]
            _, typ, xfer, ep, _dev, _bus, _fs, _fd, sec, usec, _st, _ulen, dlen = \
                struct.unpack_from('<QBBBBHBBqiiII', d, 0)
            t = sec + usec / 1e6
            t0 = t if t0 is None else t0
            data = d[64:64 + dlen]
            if xfer == 3 and ep == EP_OUT and typ == ord('S'):
                yield t - t0, 'O', data
            elif xfer == 3 and ep == EP_IN and typ == ord('C') and dlen:
                yield t - t0, 'I', data
            elif xfer == 2 and typ == ord('S') and d[40:42] == b'\x21\x22':
                if d[42] != last_line:                   # SET_CONTROL_LINE_STATE
                    last_line = d[42]
                    yield t - t0, 'L', bytes([d[42]])
        pos += blen


def transactions(events):
    """Group into (t, sent, reply). The echo of each sent byte is removed."""
    out, cur, pending = [], None, b''
    for t, kind, b in events:
        if kind == 'L':
            out.append((t, None, b))
        elif kind == 'O':
            if cur is None or cur[2]:
                if cur:
                    out.append(tuple(cur))
                cur = [t, b'', b'']
            cur[1] += b
            pending += b
        elif cur:
            n = 0
            while pending and n < len(b) and b[n] == pending[0]:
                pending, n = pending[1:], n + 1
            cur[2] += b[n:]
    if cur:
        out.append(tuple(cur))
    return out


def short(b, n=24):
    return b.hex(' ') if len(b) <= n else f"{b[:12].hex(' ')} ... {b[-4:].hex(' ')}"


def main():
    tx = transactions(serial_events(sys.argv[1]))
    polls = []
    for t, sent, reply in tx:
        if sent is None:
            print(f"{t:8.3f}  line state: DTR={reply[0] & 1} RTS={reply[0] >> 1 & 1}")
            continue
        ok = '' if not reply or sum(reply[:-1]) & 255 == reply[-1] else '  (no checksum)'
        if sent[-1:] == b'\xfc' and sent[:1] != b'\xfd':
            polls.append((t, reply))
            if len(polls) > 3 and len(sent) == 1:
                continue                                  # don't print every poll
        print(f"{t:8.3f}  sent {short(sent, 60)}  ->  [{len(reply)}] {short(reply)}{ok}")
    if polls:
        iv = [(b[0] - a[0]) * 1000 for a, b in zip(polls, polls[1:])]
        print(f"\n{len(polls)} FC polls from {polls[0][0]:.3f}s to {polls[-1][0]:.3f}s, "
              f"mean interval {sum(iv) / len(iv):.1f} ms")

    if len(sys.argv) > 2 and polls:
        f = open(sys.argv[2], 'rb').read()
        n = struct.unpack_from('<H', f, 2)[0]
        idx = struct.unpack_from(f'<{n}H', f, 4)
        ids = [struct.unpack_from('<H', f, 516 + 58 * i + 26)[0] for i in range(n)]
        order = sorted(range(n), key=lambda k: idx[k])     # poll-table order
        print("poll table expected from log header:", ' '.join(f"{ids[k]:02x}" for k in order))
        ds = 516 + 58 * n
        rs, _, cnt = struct.unpack_from('<HHI', f, ds)
        recs = [struct.unpack_from(f'<I{n}H', f, ds + 18 + k * rs) for k in range(cnt)]
        vals = []
        for _, r in polls:
            v = [0] * n
            for p, k in enumerate(order):
                v[k] = r[p]
            vals.append(tuple(v))
        for off in range(len(vals) - cnt + 1):
            if all(a == b[1:] for a, b in zip(vals[off:off + cnt], recs)):
                d = [(polls[off + k][0] - polls[off][0]) * 1000 - recs[k][0] for k in range(cnt)]
                print(f"all {cnt} log records equal polls {off}..{off + cnt - 1}; "
                      f"log ms vs capture time differs by {min(d):.0f}..{max(d):.0f} ms")
                break
        else:
            print("log records do NOT line up with the polls")


if __name__ == '__main__':
    main()
