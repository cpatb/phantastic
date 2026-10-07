"""parse_capture.py on a synthetic pcap built from real simulator exchanges.

The camera side's bytes come from Simulator.execute (the same code that serves the socket
tests). Packets are written with pktmon's quirks: every packet twice, some out of order, and
segment boundaries that split lines and frames.
"""
import importlib.util
import socket
import struct
from pathlib import Path

import numpy as np
import pytest

dpkt = pytest.importorskip('dpkt')
from phantastic.simulator import CameraModel, Simulator, synthetic_frame  # noqa: E402

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
spec = importlib.util.spec_from_file_location('parse_capture', TOOLS / 'parse_capture.py')
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

HOST, CAM = '100.100.100.1', '100.100.1.7'


def tcp_packets(src, sport, dst, dport, data, t0, seg=700, isn=1000):
    pkts, seq = [], isn
    syn = dpkt.tcp.TCP(sport=sport, dport=dport, seq=isn - 1, flags=dpkt.tcp.TH_SYN)
    pkts.append((t0, src, dst, syn))
    for k, off in enumerate(range(0, len(data), seg)):
        part = data[off:off + seg]
        pkts.append((t0 + 1e-4 * (k + 1), src, dst, dpkt.tcp.TCP(sport=sport, dport=dport, seq=seq + off,
                                                                 flags=dpkt.tcp.TH_ACK, data=part)))
    return pkts


def write_pcap(path, pkts):
    with open(path, 'wb') as f:
        w = dpkt.pcap.Writer(f, linktype=dpkt.pcap.DLT_EN10MB)
        for t, src, dst, l4 in pkts:
            ip = dpkt.ip.IP(src=socket.inet_aton(src), dst=socket.inet_aton(dst), p=6 if isinstance(l4, dpkt.tcp.TCP) else 17,
                            data=l4)
            ip.len = len(bytes(ip))
            eth = dpkt.ethernet.Ethernet(src=b'\x02' * 6, dst=b'\x04' * 6, type=dpkt.ethernet.ETH_TYPE_IP, data=ip)
            for _dup in range(2):              # pktmon records each packet at several components
                w.writepkt(bytes(eth), ts=t)


def test_capture_round_trip(tmp_path):
    sim = Simulator('127.0.0.1', 0, discovery=False, attach_port=0, model=CameraModel(width=64, height=32))
    try:
        state = {'data': None}
        cmds = ['get info.serial', 'set defc {frcount:120, ptframes:20}', 'rec {cine: 1}', 'trig', 'cstats',
                'img {cine:1, start:-50, cnt:3, fmt:P16 }', 'img {cine:1, start:0, cnt:2, fmt:P10}',
                'time {cine:1, start:-50, cnt:3}', 'get nonsense.x']
        ctrl_out, ctrl_in, data = b'', b'', b''
        for c in cmds:
            try:
                r = sim.execute(c, '127.0.0.1', state)
            except Exception as e:  # mirror the socket handler: errors become ERR lines
                r = f'ERR: {e}'
            text, payload = (r, b'') if isinstance(r, str) else r
            ctrl_out += c.encode() + b'\r\n'
            ctrl_in += text.encode('latin-1') + b'\r\n'
            data += payload
    finally:
        sim.stop()
    pkts = []
    pkts += tcp_packets(HOST, 50001, CAM, 7115, ctrl_out, 1.0, seg=37)
    pkts += tcp_packets(CAM, 7115, HOST, 50001, ctrl_in, 1.0005, seg=53)
    pkts += tcp_packets(CAM, 7116, HOST, 50002, data, 1.2, seg=1460, isn=77)
    disc = dpkt.udp.UDP(sport=40000, dport=7380, data=b'phantom?')
    disc.ulen = len(bytes(disc))
    pkts.append((0.5, HOST, '100.100.255.255', disc))
    pkts[5], pkts[6] = pkts[6], pkts[5]              # out of order
    cap = tmp_path / 'session.pcap'
    write_pcap(cap, pkts)

    out = tmp_path / 'out'
    pc.main(str(cap), str(out))
    tr = (out / 'transcript.txt').read_text(encoding='latin-1')
    for c in cmds:
        assert f'>>> {c}' in tr
    assert 'serial : 99001' in tr and 'ERR:' in tr and 'STR' in tr
    a = np.load(out / 'img_0000.npz')
    want = np.stack([synthetic_frame(n, 64, 32, seed=1) << 4 for n in (-50, -49, -48)])
    assert np.array_equal(a['frames'], want)             # P16 decoded from the reassembled data stream
    b = np.load(out / 'img_0001.npz')
    assert b['frames'].shape == (2, 32, 64) and b['raw'].size == 64 * 32 * 2 * 10 // 8
    import json
    summ = json.loads((out / 'summary.json').read_text())
    stamps = [r for r in summ['requests'] if r['kind'] == 'time'][0]['stamps']
    assert len(stamps) == 3 and all(s['exptime_us'] == 90 for s in stamps)
    assert 'phantom?' in (out / 'discovery.txt').read_text()
    assert struct.calcsize('<H') == 2
