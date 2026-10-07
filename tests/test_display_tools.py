"""Display tools (all screen-only): display curve, P16 live view and its readout, rotation, zebra,
focus assist, crop rectangle, measurements, persistence and raw snapshots.

Anchors outside the code under test: hand-computed curve values (worked in the comments), numpy's
own ``np.rot90`` / flips for what a rotated screen shows, the simulator's ``synthetic_frame`` for
what the camera sent, a 3-4-5 triangle, and time stamps written into a test cine on purpose
NON-uniformly so a speed from image number / frame rate would come out different.
"""
import json
import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
if os.name == 'nt':
    os.environ.setdefault('QT_QPA_FONTDIR', os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'))

import numpy as np
import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from phantastic.cine import TIME64_SCALE, CineReader, CineWriter
from phantastic.gui import image_tools, settings
from phantastic.gui.imageview import display_curve, focus_overlay, zebra_overlay
from phantastic.gui.main_window import MainWindow
from phantastic.gui.panels import PreviewPanel, format_scaled
from phantastic.measure import Measurement, distance_angle, px_per_unit, to_csv


def wait_until(cond, timeout=15.0, what='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError(f'timed out waiting for {what}')


# ----------------------------------------------------------------------------- pure: display curve

def test_display_curve_hand_values():
    lo, hi = 100, 200
    v = np.array([99, 100, 125, 150, 175, 200, 201], np.uint16)
    # identity: x = (v - 100) / 100, out = rint(255 x): 0, 0, 63.75->64, 127.5->128 (half to even), 191.25->191, 255
    assert display_curve(v, lo, hi).tolist() == [0, 0, 64, 128, 191, 255, 255]
    # gamma 2 at mid-window: x = 0.5, y = 0.5 ** (1/2) = 0.70711, 255 y = 180.31 -> 180
    assert int(display_curve(np.array([150], np.uint16), lo, hi, gamma=2.0)[0]) == 180
    # discriminating: the other convention (y = x ** gamma) would give 255 * 0.25 = 63.75 -> 64
    assert int(display_curve(np.array([150], np.uint16), lo, hi, gamma=2.0)[0]) != 64
    # gamma 0.5 at x = 0.25: 0.25 ** 2 = 0.0625, 255 * 0.0625 = 15.94 -> 16
    assert int(display_curve(np.array([125], np.uint16), lo, hi, gamma=0.5)[0]) == 16
    # gain 2 at x = 0.2: 0.4 -> 102; brightness 0.1 at x = 0.3: 0.4 -> 102
    assert int(display_curve(np.array([120], np.uint16), lo, hi, gain=2.0)[0]) == 102
    assert int(display_curve(np.array([130], np.uint16), lo, hi, brightness=0.1)[0]) == 102
    # gain, brightness, then gamma: 2 * 0.3 - 0.1 = 0.5, sqrt -> 180
    assert int(display_curve(np.array([130], np.uint16), lo, hi, gamma=2.0, gain=2.0, brightness=-0.1)[0]) == 180
    # toe 0.5 at x = 0.25: p = 0.5 ** 0.75 = 0.59460, 0.25 ** p = 0.43854, 255 * 0.43854 = 111.83 -> 112;
    # toe off gives 64, and at x = 1 the toe changes nothing (255)
    assert display_curve(np.array([125, 200], np.uint16), lo, hi, toe=0.5).tolist() == [112, 255]
    # clipping by gain: x = 0.75 * 2 = 1.5 -> 255; below black stays 0 whatever the gamma
    assert display_curve(np.array([175, 50], np.uint16), lo, hi, gamma=3.0, gain=2.0).tolist() == [255, 0]


def test_display_curve_never_modifies_raw_and_paths_agree():
    rng = np.random.default_rng(7)
    raw = rng.integers(0, 4096, (32, 48), dtype=np.uint16)
    before = raw.copy()
    out = display_curve(raw, 300, 3000, gamma=2.222, gain=1.5, brightness=-0.05, toe=0.7)
    assert out.dtype == np.uint8 and out.shape == raw.shape and out is not raw
    assert np.array_equal(raw, before)
    # look-up-table path (uint16) == direct float path
    assert np.array_equal(out, display_curve(raw.astype(np.float64), 300, 3000, 2.222, 1.5, -0.05, 0.7))
    assert np.array_equal(display_curve(raw.astype(np.uint8), 0, 255), raw.astype(np.uint8))   # identity 0..255


def test_zebra_and_focus_overlays_are_display_only():
    raw = np.zeros((16, 16), np.uint16)
    raw[:, 8:] = 4095                                   # right half saturated (12-bit)
    keep = raw.copy()
    a8 = display_curve(raw, 0, 4095)
    z = zebra_overlay(a8, raw, 4095)
    assert np.array_equal(raw, keep) and z.shape == (16, 16, 3)
    assert (z[:, :8] == 0).all()                        # unsaturated pixels untouched
    striped = (z[:, 8:] == (255, 0, 0)).all(axis=2)
    assert 0.3 < striped.mean() < 0.7                   # stripes, not a solid fill
    assert np.array_equal(zebra_overlay(a8, raw, 4096)[:, :, 0], a8)   # level above the data: nothing marked
    f = focus_overlay(a8, raw)
    assert np.array_equal(raw, keep)
    edge = (f[:, 6:10, 1].astype(int) - f[:, 6:10, 0].astype(int)).max()
    flat = np.abs(f[:, :4].astype(int) - a8[:, :4, None].astype(int)).max()
    assert edge > 100 and flat == 0                    # green on the edge, flat region unchanged


def test_format_scaled():
    assert format_scaled(19753, 16) == '1234.6'        # 1234.5625, one decimal
    assert format_scaled(19744, 16) == '1234'          # exactly 1234 x 16
    assert format_scaled(255, 1) == '255'


# ----------------------------------------------------------------------------- pure: measurement

def test_measure_345_and_speed_from_stamps():
    d, a = distance_angle((0, 4), (3, 0))               # 3 right, 4 up (rows grow downwards)
    assert d == 5.0 and a == pytest.approx(53.13010235, abs=1e-8)
    assert distance_angle((0, 0), (3, 4))[1] == pytest.approx(-53.13010235, abs=1e-8)
    assert distance_angle((5, 5), (2, 5))[1] == pytest.approx(180.0)
    assert px_per_unit((2, 3), (32, 43), 10.0) == 5.0  # 50 px over 10 mm
    with pytest.raises(ValueError):
        px_per_unit((1, 1), (1, 1), 10.0)
    m = Measurement((0, 4), (3, 0), image1=1, image2=2, t1=0.25, t2=1.0)
    assert m.dt == 0.75 and m.speed() == pytest.approx(5 / 0.75) and m.speed(5.0) == pytest.approx(1 / 0.75)
    assert Measurement((0, 4), (3, 0), 1, 1, 0.25, 0.25).speed() is None    # same image: no speed
    back = Measurement((0, 4), (3, 0), image1=2, image2=1, t1=1.0, t2=0.25)  # point 2 on an EARLIER image
    assert back.dt == -0.75 and back.speed() == pytest.approx(5 / 0.75)      # a speed is never negative
    csv = to_csv([m], 5.0, 'mm').splitlines()
    assert csv[0].startswith('n,image1,image2,x1,y1,x2,y2,distance_px,distance,unit,angle_deg')
    assert csv[1].split(',')[7:11] == ['5', '1', 'mm', '53.1301']


# ----------------------------------------------------------------------------- GUI fixtures

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(app):
    w = MainWindow()
    w.resize(1456, 868)
    w.show()
    yield w
    w.close()


def pattern(w=8, h=4):
    """All values distinct and < 256, asymmetric in x and y."""
    return (np.arange(w)[None, :] * 20 + np.arange(h)[:, None] * 3 + 10).astype(np.uint16)


def write_cine(path, frames, times=None, bpp=8, first=0, rate=100):
    h, w = frames[0].shape
    with CineWriter(path, w, h, len(frames), 'mono16', first_image_no=first,
                    setup_fields={'FrameRate': rate, 'RealBPP': bpp}) as cw:
        for k, f in enumerate(frames):
            t = (k, 0) if times is None else times[k]
            cw.append(f, time=t, exposure=1)
    return path


def open_max(win, path):
    assert win.open_file(path)
    panel = win.play_tab.panel
    win.mdi.activeSubWindow().showMaximized()
    QApplication.processEvents()
    return panel


def mouse(view, kind, pos, button=Qt.MouseButton.LeftButton):
    types = {'press': QEvent.Type.MouseButtonPress, 'move': QEvent.Type.MouseMove,
             'release': QEvent.Type.MouseButtonRelease}
    buttons = Qt.MouseButton.NoButton if kind == 'release' else button
    ev = QMouseEvent(types[kind], QPointF(pos), view.mapToGlobal(QPointF(pos)),
                     button if kind != 'move' else Qt.MouseButton.NoButton, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view, ev)


def screen_pos(view, c, r):
    """Widget position of the centre of DISPLAYED pixel (column c, row r)."""
    rect = view.target_rect()
    s = rect.width() / view.display_size[0]
    return rect.topLeft() + QPointF((c + 0.5) * s, (r + 0.5) * s)


# ----------------------------------------------------------------------------- GUI

def test_view_mapping_matches_numpy_for_every_rotation_and_flip(app):
    """For all 16 (rotation, flip H, flip V): each displayed pixel maps back to the stored pixel numpy
    puts there, and the stored pixel centre maps forward to that display pixel centre."""
    import itertools

    from phantastic.gui.imageview import ImageView
    a = np.arange(35, dtype=np.uint8).reshape(5, 7)
    v = ImageView()
    v.set_array(a)
    for rot, fh, fv in itertools.product(range(4), (False, True), (False, True)):
        v.rot, v.flip_h, v.flip_v = rot, fh, fv
        d = np.rot90(a, -rot)                    # clockwise, then flips on screen
        d = d[:, ::-1] if fh else d
        d = d[::-1] if fv else d
        assert np.array_equal(v.display_array(), d) and v.display_size == d.shape[::-1]
        for r, c in itertools.product(range(d.shape[0]), range(d.shape[1])):
            su, sv = v.to_stored(c + 0.5, r + 0.5)
            x, y = int(su), int(sv)
            assert a[y, x] == d[r, c] and v.to_display(x + 0.5, y + 0.5) == (c + 0.5, r + 0.5)


def test_cursor_on_pixel_edges_reads_the_painted_pixel(app):
    """Regression (review 2026-10-07): with a flip or a quarter turn, a cursor exactly on a displayed
    pixel's left/top edge read the NEIGHBOUR. Probe every whole widget position with the image on an
    integer origin (widget minus image even), at zoom 1 and 2, and compare with what numpy displays."""
    import itertools

    from phantastic.gui.imageview import ImageView
    a = np.arange(8 * 6, dtype=np.uint8).reshape(6, 8)
    v = ImageView()
    v.resize(40, 30)
    v.set_array(a)
    for zoom, (rot, fh, fv) in itertools.product((1.0, 2.0), itertools.product(range(4), (False, True),
                                                                                (False, True))):
        v.set_zoom(zoom)
        v.rot, v.flip_h, v.flip_v = rot, fh, fv
        r = v.target_rect()
        assert r.x() == int(r.x()) and r.y() == int(r.y())          # integer origin: edges are probed
        shown = v.display_array()
        for px, py in itertools.product(range(int(r.left()), int(r.right())), range(int(r.top()), int(r.bottom()))):
            x, y = v.image_coords(QPointF(px, py))
            c, row = int((px - r.x()) // zoom), int((py - r.y()) // zoom)
            assert a[y, x] == shown[row, c], (zoom, rot, fh, fv, px, py)


def test_settings_are_isolated(isolated_settings):
    path = os.path.normcase(settings.settings().fileName())
    assert path.startswith(os.path.normcase(str(isolated_settings)))


def test_rotation_composes_with_flips_and_readout_stays_stored(win, tmp_path):
    a = pattern()
    panel = open_max(win, write_cine(tmp_path / 'p.cine', [a]))
    panel.set_display_range(0, 255)                     # display value = raw value
    tools = win.show_image_tools()
    view = panel.view
    c, r = 0, 5                                          # a displayed pixel that exists when rotated (4 x 8)
    cases = (                                            # (rotate, flip_h, flip_v) -> what numpy says is shown there
        ('cw', False, False, np.rot90(a, -1)), ('ccw', False, False, np.rot90(a, 1)),
        ('cw', True, False, np.rot90(a, -1)[:, ::-1]), ('ccw', False, True, np.rot90(a, 1)[::-1]),
        ('cw', True, True, np.rot90(a, -1)[::-1, ::-1]))
    for rot, fh, fv, shown in cases:
        (tools.rot_cw if rot == 'cw' else tools.rot_ccw).setChecked(True)
        tools.flip_h.setChecked(fh)
        tools.flip_v.setChecked(fv)
        QApplication.processEvents()
        assert view.display_size == (4, 8)
        pos = screen_pos(view, c, r)
        mouse(view, 'move', pos)
        x, y, v = panel.last_readout
        assert v == int(shown[r, c]) == int(a[y, x])     # the stored pixel numpy puts on that screen spot
        assert win.xy_label.text() == f'X: {x + 1} Y: {y + 1}' and win.value_label.text() == f'Value: {v}'
        painted = view.grab().toImage().pixelColor(pos.toPoint())
        assert painted.red() == painted.green() == painted.blue() == v, (rot, fh, fv)
    # hand-worked anchor for CW, no flip: screen (col 0, row 5) shows stored (x 5, y 3) (top-left -> top-right)
    tools.flip_h.setChecked(False)
    tools.flip_v.setChecked(False)
    tools.rot_cw.setChecked(True)
    mouse(view, 'move', screen_pos(view, 0, 5))
    assert panel.last_readout == (5, 3, int(a[3, 5]))
    assert np.array_equal(panel.frame, a)                # raw untouched
    tools.rot_cw.setChecked(False)
    assert view.rot == 0 and view.display_size == (8, 4)


def test_curve_tools_disable_default_and_phadj(win, tmp_path):
    a = (np.arange(64, dtype=np.uint16).reshape(8, 8) * 60)          # 0 .. 3780, 12-bit
    panel = open_max(win, write_cine(tmp_path / 'c.cine', [a], bpp=12))
    tools = win.show_image_tools()
    tools.min_spin.setValue(0)
    tools.max_spin.setValue(3720)
    tools.gamma_spin.setValue(2.0)
    assert panel.gamma == 2.0
    # pixel [3, 7] = 31 * 60 = 1860 is mid-window: sqrt(0.5) * 255 = 180.3 -> 180 (hand value, see the pure test)
    assert int(a[3, 7]) == 1860 and int(panel.view._a8[3, 7]) == 180
    tools.disable_btn.click()                                        # linear over the full 12-bit range
    assert panel.curve_disabled and int(panel.view._a8[3, 7]) == round(1860 * 255 / 4095)   # 115.8 -> 116
    tools.disable_btn.click()
    assert int(panel.view._a8[3, 7]) == 180 and panel.gamma == 2.0  # settings kept through Disable
    path = tmp_path / 'look.phadj'
    image_tools.save_adjustments(path, panel.adjustments())
    d = json.loads(path.read_text(encoding='utf-8'))
    assert d['format'] == image_tools.PHADJ_FORMAT and d['gamma'] == 2.0 and (d['lo'], d['hi']) == (0, 3720)
    tools.default_btn.click()
    assert (panel.gamma, panel.gain, panel.brightness, panel.toe) == (1.0, 1.0, 0.0, 1.0)
    assert tools.gamma_spin.value() == 1.0
    tools.load_file(path)
    assert panel.gamma == 2.0 and (panel.lo, panel.hi) == (0, 3720) and tools.gamma_spin.value() == 2.0
    bad = tmp_path / 'bad.phadj'
    bad.write_text('{"gamma": 3}', encoding='utf-8')
    with pytest.raises(ValueError):
        image_tools.load_adjustments(bad)
    with pytest.raises(ValueError):
        panel.set_curve(gamma=20.0)                                  # PCC range 0.1 - 10
    assert np.array_equal(panel.frame, a)


def test_zebra_toolbar_and_focus_only_on_live(win, tmp_path):
    a = np.zeros((8, 16), np.uint16)
    a[:, 8:] = 255                                     # saturated for an 8-bit file
    panel = open_max(win, write_cine(tmp_path / 'z.cine', [a]))
    panel.set_display_range(0, 255)
    win.zebra_action.trigger()
    assert panel.zebra and panel.view._a8.ndim == 3
    assert (panel.view._a8[:, 8:] == (255, 0, 0)).all(axis=2).any() and (panel.view._a8[:, :8] == 0).all()
    tools = win.show_image_tools()
    assert tools.zebra_check.isChecked()
    tools.zebra_spin.setValue(300)                     # level above the data: nothing striped
    assert not (panel.view._a8 == (255, 0, 0)).all(axis=2).any()
    tools.zebra_spin.setValue(0)                       # back to saturation
    win.focus_action.trigger()                         # focus assist: live panels only (PCC p.17)
    assert panel.focus_assist and not panel.focus_allowed
    assert not (panel.view._a8[:, :, 1] > panel.view._a8[:, :, 0]).any()
    assert np.array_equal(panel.frame, a)
    assert settings.get('image_tools/zebra', False, bool) and settings.get('image_tools/focus', False, bool)
    # a full-16-bit P16 file of a 12-bit sensor: saturated = 4095 * 16 = 65520 (+ a 0-15 fraction), never 65535
    b = np.full((8, 16), 1000, np.uint16)
    b[:, 8:] = 65520 + np.arange(8)[None, :]
    p16 = open_max(win, write_cine(tmp_path / 'z16.cine', [b], bpp=16))
    assert p16.sat_level == 65520 and (p16.view._a8[:, 8:] == (255, 0, 0)).all(axis=2).any()
    assert not (p16.view._a8[:, :8] == (255, 0, 0)).all(axis=2).any()


def test_p16_live_view_readout_and_focus(win):
    win.manager_tab.add_sim_btn.click()
    wait_until(lambda: win.preview is not None and win.preview.frames_received >= 3, what='live P16 frames')
    pv = win.preview
    assert pv.fmt == 'P16' and pv.raw_max == 65535 and pv.frame.dtype == np.uint16
    assert (pv.frame % 16 == 0).all()                 # simulator P16: 12-bit x 16, no fraction
    pv.timer.stop()                                    # freeze the frame under the cursor
    QApplication.processEvents()
    f = pv.frame
    x, y = 17, 9
    mouse(pv.view, 'move', pv.view.widget_point(x, y))
    assert pv.last_readout == (x, y, int(f[y, x]))     # raw P16 as sent
    assert win.value_label.text() == f'Value: {int(f[y, x]) // 16} (12-bit)'
    tools = win.show_image_tools()
    assert tools.levels_label.text() == '0 ... 65536 levels ... 65535'
    tools.auto_btn.click()
    assert pv.lo == pytest.approx(np.percentile(f, 0.175), abs=1) and pv.hi == pytest.approx(np.percentile(f, 99.825), abs=1)
    assert pv.sat_level == 4095 * 16
    win.focus_action.trigger()
    assert (pv.view._a8[:, :, 1].astype(int) - pv.view._a8[:, :, 0]).max() > 0    # edges coloured on live
    assert np.array_equal(pv.frame, f)


def test_preview_falls_back_to_8bit_without_p16(app):
    class FakeSession:
        formats = ['8', '8R']
        info = {}
    p = PreviewPanel(tasks=None, session=FakeSession())
    try:
        assert p.fmt == '8' and p.raw_max == 255 and p.readout_div == 1
        p.last_readout = (0, 0, 200)
        assert p.readout_text() == 'Value: 200'
    finally:
        p.stop()


def test_crop_rectangle_drawn_on_rotated_view_is_stored_coordinates(win, tmp_path):
    a = np.zeros((40, 60), np.uint16)
    panel = open_max(win, write_cine(tmp_path / 'crop.cine', [a]))
    got = []
    panel.crop_changed.connect(got.append)
    tools = win.show_image_tools()
    tools.rot_cw.setChecked(True)                       # display is 40 wide x 60 tall
    win.crop_action.trigger()
    assert panel.view.mode == 'crop'
    view = panel.view
    # drag on screen from displayed (col 5, row 10) to (col 14, row 30). CW: displayed (c, r) shows stored
    # (x = r, y = H - 1 - c), so the corners are stored (10, 34) and (30, 25): x 10..30, y 25..34
    mouse(view, 'press', screen_pos(view, 5, 10))
    mouse(view, 'move', screen_pos(view, 14, 30))
    mouse(view, 'release', screen_pos(view, 14, 30))
    assert panel.crop_rect() == (10, 25, 21, 10) and got == [(10, 25, 21, 10)]
    assert tools.crop_check.isChecked() and [tools.crop_spins[k].value() for k in 'xywh'] == [10, 25, 21, 10]
    tools.crop_spins['w'].setValue(100)                 # typed values are clamped to the image
    assert panel.crop_rect() == (10, 25, 50, 10)
    panel.set_crop_rect((-5, -5, 10, 10))
    assert panel.crop_rect() == (0, 0, 5, 5)
    tools.crop_check.setChecked(False)
    assert panel.crop_rect() is None and got[-1] is None
    assert np.array_equal(panel.frame, a)


def test_calibrate_measure_and_speed_from_time_stamps(win, tmp_path, monkeypatch):
    h, w = 48, 64
    frames = [np.full((h, w), 10 * k, np.uint16) for k in range(3)]
    q = int(TIME64_SCALE)
    # images 0, 1, 2 at 0, 0.25, 1.0 s: NOT 1/100 s apart as the frame rate (100 fps) would say
    times = [(0, 0), (0, q // 4), (1, 0)]
    path = write_cine(tmp_path / 'm.cine', frames, times=times, first=0, rate=100)
    with CineReader(path) as r:
        assert r.relative_times().tolist() == [0.0, 0.25, 1.0]
    panel = open_max(win, path)
    tools = win.show_image_tools()
    monkeypatch.setattr(image_tools, 'ask_scale', lambda parent, d_px: (10.0, 'mm'))
    tools.calibrate_btn.click()
    assert win.measure_action.isChecked() and panel.view.mode == 'measure'
    view = panel.view
    for x, y in ((2, 3), (32, 43)):                    # 30 x 40 -> 50 px = 10 mm
        mouse(view, 'press', view.widget_point(x, y))
        mouse(view, 'release', view.widget_point(x, y))
    assert panel.scale == 5.0 and panel.unit == 'mm' and tools.scale_label.text() == 'Scale: 5 px/mm'
    panel.goto(1)
    for x, y in ((2, 43),):                            # point 1 on image 1 (t = 0.25 s)
        mouse(view, 'press', view.widget_point(x, y))
    panel.goto(2)                                      # point 2 on image 2 (t = 1.0 s): 3 right, 4 up
    mouse(view, 'press', view.widget_point(5, 39))
    assert len(panel.measurements) == 1
    m = panel.measurements[0]
    assert (m.p1, m.p2, m.image1, m.image2) == ((2, 43), (5, 39), 1, 2)
    assert m.distance_px == 5.0 and m.angle_deg == pytest.approx(53.130102, abs=1e-6)
    assert m.speed() == pytest.approx(5 / 0.75) and m.speed(panel.scale) == pytest.approx(1 / 0.75)
    assert m.speed() != pytest.approx(5 / 0.01)        # what image number / frame rate would claim
    assert tools.table.rowCount() == 1 and tools.table.item(0, 8).text() == '1'          # 1 mm
    assert tools.table.item(0, 13).text() == f'{1 / 0.75:.6g}'                          # mm/s
    out = tmp_path / 'meas.csv'
    tools.save_csv(out)
    lines = out.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2 and lines[1].split(',')[11] == '0.75'
    tools.copy_table()
    assert QApplication.clipboard().text().split('\n')[0].split('\t')[0] == 'n'
    assert np.array_equal(panel.frame, frames[2])


def test_snapshot_raw_tiff_is_the_stored_array(win, tmp_path):
    import tifffile
    a = pattern(16, 8) * 16                             # 12-bit-looking values
    panel = open_max(win, write_cine(tmp_path / 's.cine', [a], bpp=12))
    tools = win.show_image_tools()
    tools.rot_ccw.setChecked(True)
    tools.flip_h.setChecked(True)
    tools.gamma_spin.setValue(3.0)
    win.zebra_action.trigger()
    win.snapshot_dir = tmp_path / 'snaps'
    raw = win.snapshot_raw()
    got = tifffile.imread(raw)
    assert got.dtype == np.uint16 and np.array_equal(got, a)             # untransformed stored values
    meta = json.loads(tifffile.TiffFile(raw).pages[0].description)
    assert meta['raw'] is True and meta['image'] == panel.shown
    png = win.snapshot()                                                  # the display image: rotated
    from PySide6.QtGui import QImage
    assert QImage(str(png)).size().toTuple() == (8, 16)


def test_persistence_folders_geometry_ip(app, tmp_path, monkeypatch):
    w = MainWindow()
    w.show()
    w.resize(900, 600)
    QApplication.processEvents()
    monkeypatch.setattr('phantastic.gui.main_window.QFileDialog.getExistingDirectory',
                        lambda *a, **k: str(tmp_path / 'snapdir'))
    w.choose_snapshot_folder()
    w.close()
    assert settings.get('snapshot_dir') == str(tmp_path / 'snapdir')
    assert settings.get('main/geometry') is not None
    settings.put('last_ip', '100.100.7.7')
    settings.put('last_port', 7115)
    seen = {}

    class FakeDialog:
        def __init__(self, parent, ip='100.100.100.1', port=7115):
            seen['ip'], seen['port'] = ip, port

        def exec(self):
            return 0
    monkeypatch.setattr('phantastic.gui.main_window.ConnectDialog', FakeDialog)
    w2 = MainWindow()
    try:
        assert w2.snapshot_dir == tmp_path / 'snapdir' and w2.geometry_restored
        w2.connect_by_ip()
        assert seen == {'ip': '100.100.7.7', 'port': 7115}
    finally:
        w2.close()
