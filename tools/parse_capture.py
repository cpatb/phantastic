"""Turn a packet capture of PCC (or any client) talking to a Phantom camera into a readable record.

    python tools/parse_capture.py session.pcapng outdir/

Writes to ``outdir``:
  transcript.txt  every control command and the camera's answer, with time, in order
  discovery.txt   UDP 7380 requests and replies
  summary.json    connections, image requests, byte counts, gaps
  img_<k>.npz     the image data of the k-th ``img`` request, decoded (raw wire values) when
                  the format is known, plus the raw bytes either way

Works on pcap and pcapng (Wireshark, or Windows ``pktmon etl2pcap``). pktmon records each
packet at several stack components, so duplicates are expected and removed by TCP sequence
number. Needs ``dpkt`` (``pip install dpkt``).
"""
from __future__ import annotations

import json
import socket
import sys
from collections import defaultdict
from pathlib import Path

import dpkt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phantastic import protocol as P  # noqa: E402

CONTROL_PORT, ATTACH_PORT, DISCOVERY_PORT = P.CONTROL_PORT, P.ATTACH_PORT, P.DISCOVERY_PORT


def packets(path):
    """Yield (time, ethernet-or-ip bytes, linktype) from pcap or pcapng."""
    with open(path, 'rb') as f:
        magic = f.read(4)
        f.seek(0)
        reader = dpkt.pcapng.Reader(f) if magic == b'\x0a\x0d\x0d\x0a' else dpkt.pcap.Reader(f)
        link = reader.datalink()
        for ts, buf in reader:
            yield ts, buf, link


def ip_of(buf, link):
    if link == dpkt.pcap.DLT_EN10MB:
        eth = dpkt.ethernet.Ethernet(buf)
        return eth.data if isinstance(eth.data, dpkt.ip.IP) else None
    if link in (dpkt.pcap.DLT_RAW, 101, 228):
        return dpkt.ip.IP(buf)
    if link == dpkt.pcap.DLT_NULL:
        return dpkt.loopback.Loopback(buf).data
    return None


class Stream:
    """One direction of a TCP connection, reassembled by sequence number."""

    def __init__(self):
        self.segments = {}       # seq -> (time, payload); duplicates collapse
        self.isn = None

    def add(self, t, seq, payload, syn):
        if syn:
            self.isn = (seq + 1) & 0xFFFFFFFF
        if payload:
            old = self.segments.get(seq)
            if old is None or len(payload) > len(old[1]):
                self.segments[seq] = (t, payload)

    def assemble(self):
        """Return (bytes, [(offset, time)], gaps) in sequence order from the ISN (or lowest seq)."""
        if not self.segments:
            return b'', [], []
        base = self.isn if self.isn is not None else min(self.segments)
        items = sorted(((s - base) & 0xFFFFFFFF, t, p) for s, (t, p) in self.segments.items())
        out, marks, gaps, pos = bytearray(), [], [], 0
        for off, t, p in items:
            if off > pos:
                gaps.append((pos, off))
                out.extend(b'\0' * (off - pos))       # keep offsets aligned; gap is reported
                pos = off
            if off + len(p) <= pos:
                continue                               # fully retransmitted data
            p = p[pos - off:]
            marks.append((pos, t))
            out.extend(p)
            pos += len(p)
        return bytes(out), marks, gaps


def time_at(marks, offset):
    t = None
    for off, tt in marks:
        if off > offset:
            break
        t = tt
    return t


def main(path, outdir):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    flows = defaultdict(Stream)
    disc = []
    n_pkts = 0
    for ts, buf, link in packets(path):
        ip = ip_of(buf, link)
        if ip is None:
            continue
        n_pkts += 1
        src, dst = socket.inet_ntoa(ip.src), socket.inet_ntoa(ip.dst)
        seg = ip.data
        if isinstance(seg, dpkt.udp.UDP) and DISCOVERY_PORT in (seg.sport, seg.dport):
            disc.append((ts, f'{src}:{seg.sport}', f'{dst}:{seg.dport}', bytes(seg.data)))
        elif isinstance(seg, dpkt.tcp.TCP):
            key = (src, seg.sport, dst, seg.dport)
            flows[key].add(ts, seg.seq, bytes(seg.data), bool(seg.flags & dpkt.tcp.TH_SYN))

    # de-duplicate discovery (pktmon) and write it
    seen, lines = set(), []
    for ts, s, d, data in sorted(disc):
        k = (s, d, data)
        if k in seen:
            continue
        seen.add(k)
        lines.append(f'{ts:.6f} {s} -> {d}: {data!r}')
    (out / 'discovery.txt').write_text('\n'.join(lines) + '\n')

    # control connections: camera side on port 7115
    controls = sorted({(k[2], k[3], k[0], k[1]) for k in flows if k[3] == CONTROL_PORT})
    transcript, img_requests, summary = [], [], {'packets': n_pkts, 'controls': [], 'data': []}
    for cam_ip, cam_port, host_ip, host_port in controls:
        c2s, cm, cg = flows[(host_ip, host_port, cam_ip, cam_port)].assemble()
        s2c, sm, sg = flows[(cam_ip, cam_port, host_ip, host_port)].assemble()
        cmds, pos = [], 0
        for raw in c2s.split(b'\n'):
            line = raw.rstrip(b'\r')
            if line.strip():
                cmds.append((time_at(cm, pos), line.decode('latin-1')))
            pos += len(raw) + 1
        resps, rest, pos = [], s2c, 0
        while True:
            line, rest2 = P.split_response(rest)
            if line is None:
                break
            resps.append((time_at(sm, pos), P.clean_line(line)))
            pos += len(rest) - len(rest2)
            rest = rest2
        summary['controls'].append(dict(camera=f'{cam_ip}:{cam_port}', host=f'{host_ip}:{host_port}',
                                        commands=len(cmds), responses=len(resps), gaps=cg + sg))
        transcript.append(f'### control {host_ip}:{host_port} -> {cam_ip}:{cam_port}')
        for i, (t, cmd) in enumerate(cmds):
            r = resps[i][1] if i < len(resps) else '<no response captured>'
            transcript.append(f'{t if t is not None else 0:.6f} >>> {cmd}')
            transcript.append(f'          <<< {r.strip()[:2000]}')
            head = cmd.split(' ', 1)[0]
            if head in ('img', 'time') and i < len(resps):
                try:
                    hdr = P.parse_value(P.check_ok(resps[i][1]))
                    req = P.parse_value(cmd.split(' ', 1)[1])
                except P.ProtocolError:
                    continue
                img_requests.append(dict(kind=head, cam=cam_ip, request=req, reply=hdr, t=t))

    # data streams: any TCP flow from a camera that is not a control stream, in time order
    cams = {c[0] for c in controls}
    data_flows = sorted((k for k in flows if k[0] in cams and k[1] != CONTROL_PORT and flows[k].segments),
                        key=lambda k: min(t for t, _ in flows[k].segments.values()))
    # A camera has one data stream at a time and a new startdata/attach replaces the old one, so a
    # camera's data connections are concatenated in time order and consumed in request order.
    blob_of, cursor = {}, defaultdict(int)
    for k in data_flows:
        b, _, gaps = flows[k].assemble()
        blob_of[k[0]] = blob_of.get(k[0], b'') + b
        summary['data'].append(dict(flow=f'{k[0]}:{k[1]} -> {k[2]}:{k[3]}', bytes=len(b), gaps=gaps))
    for n, rq in enumerate(img_requests):
        k = rq['cam']
        if k not in blob_of:
            rq['note'] = 'no data stream captured'
            continue
        reply = rq['reply']
        if rq['kind'] == 'img':
            res = reply['res']
            fmt = P.format_number(reply.get('fmt', rq['request'].get('fmt', '8')))
            cnt = int(rq['request'].get('cnt', 1))
            try:
                nbytes = P.frame_bytes(fmt, res.width, res.height) * cnt
            except (KeyError, ValueError):
                rq['note'] = f'unknown format {fmt}: data stream position lost after this request'
                break
            chunk = blob_of[k][cursor[k]:cursor[k] + nbytes]
            cursor[k] += nbytes
            arrays = {'raw': np.frombuffer(chunk, np.uint8)}
            if len(chunk) == nbytes:
                arrays['frames'] = P.decode_frames(chunk, fmt, res.width, res.height, cnt)
            np.savez(out / f'img_{n:04d}.npz', **arrays, request=json.dumps(rq['request'], default=str),
                     reply=json.dumps(reply, default=str))
            rq['bytes'] = len(chunk)
        else:
            nbytes = int(reply['size']) * int(reply['cnt'])
            chunk = blob_of[k][cursor[k]:cursor[k] + nbytes]
            cursor[k] += nbytes
            stamps = P.decode_time_stamps(chunk, int(reply['size']), int(reply['cnt'])) if len(chunk) == nbytes else []
            rq['stamps'] = [vars(s) for s in stamps]
    summary['requests'] = img_requests
    (out / 'transcript.txt').write_text('\n'.join(transcript) + '\n', encoding='latin-1')
    (out / 'summary.json').write_text(json.dumps(summary, indent=1, default=str))
    print(f'{n_pkts} IP packets; {len(controls)} control connection(s); {len(img_requests)} img/time requests; '
          f'{len(data_flows)} data stream(s) -> {out}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
