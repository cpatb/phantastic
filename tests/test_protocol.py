"""Protocol grammar tests.

Inline fixtures are short excerpts of real Phantom Miro M310 telnet transcripts published in
DiamondLightSource/miroCamera (Apache-2.0, documentation/logs). The full logs are parsed too when
PHANTASTIC_MIRO_LOGS points at that directory.
"""
import glob
import os

import numpy as np
import pytest

from phantastic.cine import pack10, pack12L
from phantastic.protocol import (MAX_RESPONSE, Flags, ProtocolError, Resolution, check_ok, clean_line, decode_frames,
                                 decode_time_stamps, format_value, parse_discovery_reply, parse_get,
                                 parse_value, split_response, strip_events)

DEFC = (b'defc : {\t\t\\\r\n\tres : 1280 x 504,\t\\\r\n\trate : 1080,\t\\\r\n\texp : 925482,\t\\\r\n'
        b'\tptframes : 820,\t\\\r\n\tramp : "",\t\\\r\n\taexpcomp : -0.1605,\t\\\r\n'
        b'\tmeta : {\t\t\\\r\n\t\tox : -160,\t\\\r\n\t\tresize : 0 \t\\\r\n\t} \t\\\r\n}\r\n')
CSTATS = b'c0 : { RDY DEF PRE }\\\r\nc1 : { WTR DEF ABL ACT }\\\r\nc2 : { RDY DEF ABL }\r\n'


def one(raw):
    line, rest = split_response(raw)
    assert rest == b''
    return clean_line(line)


def test_structure_get():
    v = parse_get(one(DEFC))
    assert v['res'] == Resolution(1280, 504)          # printed with spaces
    assert v['rate'] == 1080 and v['exp'] == 925482
    assert v['ramp'] == '' and v['aexpcomp'] == pytest.approx(-0.1605)
    assert v['meta'] == {'ox': -160, 'resize': 0}


def test_cstats_flags():
    v = parse_get(one(CSTATS))
    assert v['c1'] == Flags(('WTR', 'DEF', 'ABL', 'ACT'))
    assert 'WTR' in v['c1'] and 'STR' not in v['c1']
    assert 'PRE' in v['c0']


def test_leaf_and_errors():
    assert parse_get('snstemp : 29') == 29
    assert parse_get('model : "Phantom Miro M310"') == 'Phantom Miro M310'
    assert parse_get('features : "bref blk4 burst edr attach"') == 'bref blk4 burst edr attach'
    with pytest.raises(ProtocolError, match='name junk'):
        check_ok('ERR: name junk  is unknown ')
    assert check_ok('Ok!') == '' and check_ok('OK! {cine:1}') == '{cine:1}'


def test_img_response():
    v = parse_value(check_ok('Ok! { cine: 1, res: 4096 x 2304, fmt: 266 }'))
    assert v == {'cine': 1, 'res': Resolution(4096, 2304), 'fmt': 266}
    assert parse_value('{cine:1, res:256x256, fmt:272}')['res'] == Resolution(256, 256)


def test_incomplete_and_events():
    line, rest = split_response(b'snstemp : 2')
    assert line is None and rest == b'snstemp : 2'
    text, ev = strip_events('@trig@\\\r\nOk!')
    assert ev == ['trig'] and text.strip() == 'Ok!'


def test_format_value():
    assert format_value({'res': (512, 384), 'rate': 1000}) == '{res:512x384, rate:1000}'
    assert format_value('my camera') == '"my camera"'
    assert format_value(Resolution(1280, 800)) == '1280x800'


def test_discovery():
    assert parse_discovery_reply(b'PH16 7115 8001 17277') == dict(protocol='PH16', port=7115, hwver=8001,
                                                                 serial=17277, name=None)
    r = parse_discovery_reply(b'PH16 7115 4001 16001 "FAKE CAMERA"\0')
    assert r['name'] == 'FAKE CAMERA' and r['serial'] == 16001
    assert parse_discovery_reply(b'PH7 7115')['protocol'] == 'PH7'
    assert parse_discovery_reply(b'hello') is None


def test_decode_frames_formats():
    rng = np.random.default_rng(0)
    img16 = rng.integers(0, 4096, (2, 4, 8), dtype=np.uint16)
    assert np.array_equal(decode_frames(img16.astype('<u2').tobytes(), 'P16', 8, 4, 2), img16)
    codes = rng.integers(0, 1024, (1, 4, 8), dtype=np.uint16)
    assert np.array_equal(decode_frames(b''.join(pack10(r) for r in codes[0]), 'P10', 8, 4, 1), codes)
    v12 = rng.integers(0, 4096, (1, 4, 8), dtype=np.uint16)
    assert np.array_equal(decode_frames(b''.join(pack12L(r) for r in v12[0]), 'P12L', 8, 4, 1), v12)
    with pytest.raises(ValueError):
        decode_frames(b'\0' * 10, 'P16', 8, 4, 1)


def test_p10_spec_bit_diagram():
    # Cine File Format 5.1.2: byte0 = P0[9:2], byte1 = P0[1:0] P1[9:4], ... (hand-built bytes)
    p = [0b1110000101, 0b0011101111, 0b0000001111, 0b0010111001]   # 901, 239, 15, 185
    bits = ''.join(f'{x:010b}' for x in p)
    raw = bytes(int(bits[i:i + 8], 2) for i in range(0, 40, 8))
    assert decode_frames(raw, 'P10', 4, 1, 1).ravel().tolist() == p


def test_time_stamps():
    rec = (100 * 3600).to_bytes(4, 'big') + (500).to_bytes(2, 'big') + ((1234 << 2) | 0b10).to_bytes(2, 'big')
    ts = decode_time_stamps(rec, 8, 1)[0]
    assert ts.exptime_us == 500 and ts.seconds_of_year == pytest.approx(3600.001234)
    assert ts.locked and not ts.event


@pytest.mark.skipif(not os.environ.get('PHANTASTIC_MIRO_LOGS'), reason='real transcript directory not given')
def test_real_transcripts_parse():
    bad, truncated = [], 0
    for p in glob.glob(os.path.join(os.environ['PHANTASTIC_MIRO_LOGS'], '*.log')):
        buf = open(p, 'rb').read()
        while True:
            line, buf = split_response(buf)
            if line is None:
                break
            if len(line) >= MAX_RESPONSE:      # camera truncates at 64 KB (spec 5.1): not parsable
                truncated += 1
                continue
            s = clean_line(line).strip()
            if ' : ' in s and not s.startswith(('ERR', '[')):   # '[' = camera console log echo
                try:
                    parse_get(s)
                except ProtocolError as e:
                    bad.append((os.path.basename(p), s[:60], str(e)))
    assert not bad, bad[:3]
    assert truncated == 2   # the two 'get *' answers in miroTelnet2.log
