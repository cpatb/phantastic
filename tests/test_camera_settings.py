"""Camera settings beyond res/rate/exposure, against the simulated camera with the 'v2512' profile.

Anchors outside the code under test: the simulator's own model state (what the camera was actually
told), the variable names and types of the v2512 read-out (encoded in data/v2512_tree.txt), hand-written
expected command lines, and the vendor SDK's captured clock commands (docs/captures).
"""
import json
import time

import pytest

from phantastic import camsettings as CS
from phantastic import protocol as P
from phantastic.camera import Camera, acquisition_update, bref_finished
from phantastic.cine import CineReader
from phantastic.simulator import CameraModel, Simulator

V2512_POWER_ON_SEC = 946684911     # irig.sec a v2512 read after power-on (2026-10-07): 2000-01-01, never set


@pytest.fixture()
def sim():
    s = Simulator('127.0.0.1', 0, discovery_port=0, attach_port=0,
                  model=CameraModel(width=64, height=32, profile='v2512')).start()
    yield s
    s.stop()


@pytest.fixture()
def cam(sim):
    with Camera('127.0.0.1', sim.port, timeout=5) as c:
        yield c


def sent(sim, prefix):
    return [c for c in sim.model.received if c.startswith(prefix)]


def test_set_line_forms():
    assert P.set_line('cam.timezone', 18000) == 'set cam.timezone:18000'     # the vendor SDK's form (captured)
    assert P.set_line('defc', {'bcount': 2}, ' ') == 'set defc {bcount:2}'
    assert P.set_line('meta.comment', 'two words') == 'set meta.comment:"two words"'
    assert P.set_line('defc.aexpcomp', -0.0385) == 'set defc.aexpcomp:-0.0385'
    with pytest.raises(ValueError):
        P.set_line('x', 1, '=')


def test_read_settings_reports_only_what_the_camera_has(sim, cam):
    cur, structs = CS.read_settings(cam)
    assert set(structs) == {'defc', 'cam', 'auto', 'meta', 'irig'}
    # every table entry exists in the v2512 profile; types are the v2512's
    assert set(cur) == set(CS.BY_KEY)
    for k, v in cur.items():
        assert CS.type_ok(CS.BY_KEY[k], v), (k, v)
    assert isinstance(cur['meta.comment'], str) and isinstance(cur['auto.trigger.x'], int)
    # reading sends no set, setrtc, bref, rec, del or partition
    assert all(c.startswith('get ') for c in sim.model.received)


def test_default_camera_hides_unknown_structures():
    with Simulator('127.0.0.1', 0, discovery_port=0, attach_port=0) as s, \
            Camera('127.0.0.1', s.port, timeout=5) as c:
        cur, structs = CS.read_settings(c)
        assert 'auto' not in structs and 'meta' not in structs           # the default model has neither
        assert not any(k.startswith(('auto.', 'meta.')) for k in cur)
        assert 'cam.trigpol' in cur and 'cam.trigdelay' not in cur      # only what the camera reported
        with pytest.raises(ValueError, match='not reported'):
            CS.changes(cur, {'cam.trigdelay': 5})


def test_changes_typing_and_readonly(cam):
    cur, _ = CS.read_settings(cam)
    assert CS.changes(cur, {'cam.trigpol': cur['cam.trigpol']}) == {}       # unchanged -> nothing to send
    assert CS.changes(cur, {'cam.trigpol': 1}) == {'cam.trigpol': 1}
    with pytest.raises(ValueError, match='not an integer'):
        CS.changes(cur, {'cam.trigpol': 1.5})
    with pytest.raises(ValueError, match='not an integer'):
        CS.changes(cur, {'cam.trigpol': '1'})
    with pytest.raises(ValueError, match='read only'):
        CS.changes(cur, {'cam.aux1mode': 3})
    assert CS.changes(cur, {'defc.aexpcomp': 1}) == {'defc.aexpcomp': 1.0}  # int accepted for a float, sent as float
    with pytest.raises(ValueError, match='not a string'):
        CS.changes(cur, {'meta.name': 5})
    assert CS.parse(CS.BY_KEY['cam.trigdelay'], ' 12 ') == 12
    with pytest.raises(ValueError):
        CS.parse(CS.BY_KEY['cam.trigdelay'], '1.5')
    with pytest.raises(ValueError):
        CS.parse(CS.BY_KEY['defc.aexpcomp'], 'nan')
    with pytest.raises(ValueError, match='latin-1'):
        CS.parse(CS.BY_KEY['meta.name'], 'arrow →')


def test_apply_sends_exactly_the_previewed_lines(sim, cam):
    cur, _ = CS.read_settings(cam)
    chg = CS.changes(cur, {'cam.trigpol': 1, 'cam.trigfilt': 32, 'auto.trigger.x': -10, 'meta.comment': 'drop 7, 2 m/s',
                           'defc.bcount': 3, 'defc.aexpcomp': -0.5, 'cam.quiet': cur['cam.quiet']})
    lines = CS.set_lines(chg)
    assert lines == ['set defc {bcount:3, aexpcomp:-0.5}', 'set cam.trigpol:1', 'set cam.trigfilt:32',
                     'set auto.trigger.x:-10', 'set meta.comment:"drop 7, 2 m/s"']
    n0 = len(sim.model.received)
    back = CS.apply_changes(cam, chg)
    assert [c for c in sim.model.received[n0:] if not c.startswith('get ')] == lines   # nothing else written
    assert back == {'defc.bcount': 3, 'defc.aexpcomp': -0.5, 'cam.trigpol': 1, 'cam.trigfilt': 32,
                    'auto.trigger.x': -10, 'meta.comment': 'drop 7, 2 m/s'}
    m = sim.model                                                            # what the camera was told
    assert (m.cam['trigpol'], m.cam['trigfilt'], m.auto['trigger']['x']) == (1, 32, -10)
    assert m.meta['comment'] == 'drop 7, 2 m/s' and m.defc['bcount'] == 3 and m.defc['aexpcomp'] == -0.5
    assert 'quiet' not in ' '.join(lines)                                    # untouched field never sent


def test_set_returns_readback_and_logs(sim, tmp_path):
    log = tmp_path / 's.log'
    with Camera('127.0.0.1', sim.port, timeout=5, log_file=log) as c:
        assert c.set('cam.trigpol', 1) == 1
        with pytest.raises(P.ProtocolError, match='expects int'):        # the simulator refuses a wrong type
            c.set('cam.trigpol', 'rising')
    text = log.read_text(encoding='utf-8')
    assert '>> set cam.trigpol:1' in text and 'camera reads back 1' in text


def test_configure_new_keywords(sim, cam):
    assert acquisition_update(burst_count=2, shutter_offset=0) == {'bcount': 2, 'shoff': 0}
    assert acquisition_update(burst_period=None) == {}                     # untouched -> not sent
    with pytest.raises(TypeError):
        acquisition_update(burst_count=2.5)
    with pytest.raises(TypeError):
        acquisition_update(burst_counts=2)
    d = cam.configure(burst_count=2, burst_period=500)
    assert d['bcount'] == 2 and d['bperiod'] == 500
    assert sent(sim, 'set ')[-1] == 'set defc {bcount:2, bperiod:500}'      # only the given fields


def test_set_clock_and_camera_clock(sim, cam):
    t0 = cam.get('irig.sec')
    assert abs(t0 - V2512_POWER_ON_SEC) < 60                               # never set: 2000-01-01
    now = int(time.time())
    back = cam.set_clock(now)
    assert sent(sim, 'setrtc') == [f'setrtc {now}']                       # the vendor SDK's form (captured)
    assert abs(back - now) <= 2
    assert cam.set('cam.timezone', 18000) == 18000
    assert sent(sim, 'set cam.timezone') == ['set cam.timezone:18000']


def test_bref_finished_rule():
    assert bref_finished([], 0) is None
    assert bref_finished([0], 0.1) is None                                   # not started yet: keep polling
    assert bref_finished([0, 30, 70], 0.5) is None
    assert bref_finished([0, 30, 100], 0.5)[0] is True
    assert bref_finished([0, 30, 0], 0.5)[0] is True
    ok, why = bref_finished([0, 0, 0], 5.0)
    assert ok is False and 'NOT confirmed' in why                            # silence is not success


def test_black_reference_polls_progress(sim, cam):
    seen = []
    r = cam.black_reference(progress=lambda d, t: seen.append(d), poll=0.05, timeout=5)
    assert r['confirmed'] and max(r['readings']) > 0 and r['readings'][-1] == 0
    assert seen[-1] == 100 and sent(sim, 'bref') == ['bref']


def test_download_carries_cine_name_and_description(sim, cam, tmp_path):
    cam.set('meta.name', 'impact')
    cam.set('meta.comment', 'silica 40 %, 2.1 m/s')
    cam.command('set defc {frcount:20, ptframes:5}')
    cam.record(1)
    cam.trigger()
    cam.wait_stored(1, timeout=2)
    cam.set('meta.comment', 'changed after the recording')                  # must not reach cine 1's file
    out = tmp_path / 'named.cine'
    cam.download(1, out)
    with CineReader(out) as r:
        assert r.setup['CineName'] == 'impact'
        d = r.setup['Description']
        assert 'silica 40 %, 2.1 m/s' in d and 'c1.meta.comment' in d and 'changed after' not in d
        assert 'Phantastic download' in d.split('Camera description')[0]   # own notes first, never truncated away


def test_download_without_camera_meta(tmp_path):
    with Simulator('127.0.0.1', 0, discovery_port=0, attach_port=0,
                   model=CameraModel(width=32, height=16)) as s, Camera('127.0.0.1', s.port, timeout=5) as c:
        assert c.cine_meta(1)['source'] is None
        c.command('set defc {frcount:10, ptframes:2}')
        c.record(1)
        c.trigger()
        c.wait_stored(1, timeout=2)
        c.download(1, tmp_path / 'x.cine')
        with CineReader(tmp_path / 'x.cine') as r:
            assert r.setup['CineName'] == '' and 'Camera description' not in r.setup['Description']


def test_cine_meta_falls_back_to_current_meta(sim, cam):
    cam.set('meta.name', 'now')
    m = cam.cine_meta(1)          # cine 1 holds no recording, so c1.meta has no name: falls back, and says so
    assert m == {'name': 'now', 'comment': '', 'source': 'meta'}


def test_roi_convention_round_trip_and_anchor():
    # Miro M310 tree: y = -52, h = 400 on 1280 x 504 -> flush with the top edge under the stated convention
    assert CS.roi_from_camera(0, -52, 1280, 400, 1280, 504) == (0, 0, 1280, 400)
    for rect in ((0, 0, 64, 16), (13, 5, 37, 21), (200, 100, 56, 156)):
        cx = CS.roi_to_camera(*rect, 256, 256)
        assert CS.roi_from_camera(*cx, 256, 256) == rect
    assert CS.roi_to_camera(96, 120, 64, 16, 256, 256) == (0, 0, 64, 16)    # centred region -> (0, 0)


def test_backup_round_trip_and_diff(cam, tmp_path):
    cur, _ = CS.read_settings(cam)
    path = tmp_path / 'setup.json'
    CS.save_backup(path, cur, {'model': 'sim', 'serial': 1, 'swver': 1})
    d = json.loads(path.read_text(encoding='utf-8'))
    assert 'cam.aux1mode' not in d['settings'] and d['settings']['cam.trigpol'] == cur['cam.trigpol']
    saved = CS.load_backup(path)['settings']
    assert CS.backup_diff(cur, saved) == ({}, [])                            # same camera state: nothing to apply
    saved.update({'cam.trigpol': 1, 'cam.trigdelay': 'x', 'video.system': 4, 'cam.aux2mode': 1})
    wanted, notes = CS.backup_diff(cur, saved)
    assert wanted == {'cam.trigpol': 1}
    assert len(notes) == 3 and any('video.system' in n for n in notes)
    bad = tmp_path / 'bad.json'
    bad.write_text('{"settings": {}}', encoding='utf-8')
    with pytest.raises(ValueError, match='not a Phantastic'):
        CS.load_backup(bad)
