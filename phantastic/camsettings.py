"""Camera settings beyond resolution / rate / exposure: the variables, how they are read, compared,
written and backed up.

Rules this module enforces (the GUI and the backup file both go through it):

* Read before write. A setting exists for Phantastic only if the connected camera reported it in
  ``get <structure>``; :func:`read_settings` returns exactly those, and anything not in
  :data:`SETTINGS` is ignored (never guessed).
* Only changed values are sent, typed as the camera reported them (int / float / string), and the
  exact command lines are available before anything is sent (:func:`set_lines`).
* Every write goes through ``Camera.set`` (``configure`` for ``defc`` fields), which logs it and reads
  it back; :func:`apply_changes` returns those read-backs.

Variable names and value types: as a Phantom v2512 (s/n 21598, firmware 23070) answered ``get defc``,
``get cam``, ``get auto``, ``get meta`` and ``get irig`` on 2026-10-07 (read only). Behaviour: PCC 3.11
user manual pages cited per entry. What the manual does NOT give is the numeric meaning of most
values (which number is "rising edge", which unit a delay is in); those are shown raw and labelled.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass

from . import protocol as P
from .camera import DEFC_KEYWORDS

STRUCTURES = ('defc', 'cam', 'auto', 'meta', 'irig')   # read with 'get'; never 'get *' (64 KB cap)
NOT_VERIFIED = 'value meaning not verified'
BACKUP_FORMAT = 'phantastic-camera-settings'
BACKUP_VERSION = 1


@dataclass(frozen=True)
class Setting:
    key: str              # dotted camera variable, e.g. 'cam.trigpol'
    typ: type             # int, float or str (as the v2512 answered)
    label: str
    section: str
    note: str             # what is and is not known, with the PCC manual page
    editable: bool = True

    @property
    def via_configure(self) -> bool:
        """defc fields go through ``Camera.configure`` (one ``set defc {...}``), others ``Camera.set``."""
        return self.key.startswith('defc.')


SIGNALS, ADVANCED, CINE_ADV, AUTO_EXP, IBAT, CINE_META = (
    'Camera Signals', 'Advanced Settings', 'Cine Settings advanced', 'Auto Exposure',
    'Image-Based Auto-Trigger', 'Cine Name / Description')

SETTINGS: tuple[Setting, ...] = (
    # -- Camera Signals, PCC p.136-139. PCC names the choices; the numbers behind them are not documented.
    Setting('cam.trigpol', int, 'Trigger edge', SIGNALS,
            f'PCC p.136 offers Rising / Falling Edge; which number is which: {NOT_VERIFIED}.'),
    Setting('cam.trigfilt', int, 'Trigger filter', SIGNALS,
            "PCC p.136 'Filter Time' (µs in PCC); the unit of this value is not verified."),
    Setting('cam.trigdelay', int, 'Trigger delay', SIGNALS,
            "PCC p.139 'Delay Time'; the unit of this value is not verified."),
    Setting('cam.memgateen', int, 'Memory gate', SIGNALS,
            f"PCC p.136 'Pretrigger pin is: Memory Gate / Pre-trigger'; {NOT_VERIFIED}."),
    Setting('cam.readygate', int, 'Ready signal ends at', SIGNALS,
            f"PCC p.136 'Trigger / Rec end'; {NOT_VERIFIED}."),
    *(Setting(f'cam.aux{k}mode', int, f'Aux {k} pin', SIGNALS,
              'Raw, meaning not verified: PCC p.137 names the assignable signals but not their numbers.',
              editable=False) for k in (1, 2, 3, 4)),
    # -- Advanced Settings, PCC p.47-49
    Setting('cam.syncimg', int, 'External sync', ADVANCED,
            f'PCC p.48 (Sync Imaging, see Camera Synchronization); {NOT_VERIFIED}.'),
    Setting('cam.master', int, 'Master camera', ADVANCED, f'PCC p.47 External Sync; {NOT_VERIFIED}.'),
    Setting('cam.frdelay', int, 'Frame delay', ADVANCED,
            'PCC p.47 External Sync (µs in PCC); the unit of this value is not verified.'),
    Setting('cam.startonacq', int, 'Starts in', ADVANCED,
            f'PCC p.48 Starts in Idle / Capture (at power-up); which number is which: {NOT_VERIFIED}.'),
    Setting('cam.quiet', int, 'Quiet fans', ADVANCED,
            f'PCC p.48: fans off or at minimum until the temperature threshold; {NOT_VERIFIED} (0/1 assumed).'),
    Setting('cam.tcmode', int, 'Time-code mode', ADVANCED,
            f"PCC p.48 'Time info out pin format' IRIG / SMPTE is the likely match; {NOT_VERIFIED}."),
    Setting('auto.bref', int, 'Auto CSR', ADVANCED,
            f'PCC p.47: a CSR whenever the camera enters Capture (cameras with an internal shutter); '
            f'{NOT_VERIFIED} (0/1 assumed).'),
    Setting('auto.filesave', int, 'Auto file save', ADVANCED,
            'PCC p.47: save to flash when the recording completes. NEEDS FLASH STORAGE (CineMag, CineFlash or '
            'built-in flash); PCC lists two auto-save targets and which one this variable is, and whether this '
            f'camera has flash, is not verified. {NOT_VERIFIED}.'),
    Setting('auto.acqrestart', int, 'Restart recording', ADVANCED,
            f'PCC p.48: back to Capture after the end-of-recording actions; only with an auto save. {NOT_VERIFIED}.'),
    # -- Cine Settings, advanced: burst (PCC p.47, p.112-114) and shutter offset; sent with configure()
    Setting('defc.bcount', int, 'Burst count', CINE_ADV, 'PCC p.47: images per burst; 0 disables burst mode.'),
    Setting('defc.bperiod', int, 'Burst period', CINE_ADV,
            'PCC p.47 (µs in PCC); the unit of this value is not verified.'),
    Setting('defc.shoff', int, 'Shutter offset', CINE_ADV,
            "Possibly PCC's 'Exposure in PIV Mode' (p.47); unit and behaviour not verified."),
    # -- Auto Exposure, PCC p.41-42
    Setting('defc.aexpmode', int, 'Auto exposure mode', AUTO_EXP,
            f'PCC p.41: on/off and Average / Spot / Center Weighted; {NOT_VERIFIED}.'),
    Setting('defc.aexpcomp', float, 'Compensation', AUTO_EXP,
            'PCC p.42: target grey level, 0 = 50 % grey, about +/-2 f-stops; the scale of this value is not verified.'),
    # -- Image-Based Auto-Trigger, PCC p.115-117
    Setting('auto.trigger.mode', int, 'Mode', IBAT, f'Enables the auto trigger (PCC p.116); {NOT_VERIFIED}.'),
    Setting('auto.trigger.threshold', int, 'Threshold', IBAT,
            f"PCC p.115-116 'Gray Values' / threshold is the likely match; scale {NOT_VERIFIED}."),
    Setting('auto.trigger.area', int, 'Area', IBAT, f"PCC p.116 'Area %' is the likely match; {NOT_VERIFIED}."),
    Setting('auto.trigger.speed', int, 'Speed', IBAT,
            f"PCC p.116 'Check Interval (frames)' is the likely match; {NOT_VERIFIED}."),
    Setting('auto.trigger.x', int, 'x', IBAT, 'Region; see ROI_CONVENTION (unverified on hardware).'),
    Setting('auto.trigger.y', int, 'y', IBAT, 'Region; see ROI_CONVENTION (unverified on hardware).'),
    Setting('auto.trigger.w', int, 'w', IBAT, 'Region width in pixels (assumed).'),
    Setting('auto.trigger.h', int, 'h', IBAT, 'Region height in pixels (assumed).'),
    # -- Cine name / description, PCC p.33
    Setting('meta.name', str, 'Name', CINE_META,
            'PCC p.33: name of the next recorded cine (the camera may append a counter).'),
    Setting('meta.comment', str, 'Description', CINE_META,
            'PCC p.33: up to 4096 characters; cannot be edited after the cine is captured.'),
)
BY_KEY = {s.key: s for s in SETTINGS}
_KIND = {int: 'an integer', float: 'a number', str: 'a string'}
_DEFC_KEYWORD = {name: kw for kw, (name, _) in DEFC_KEYWORDS.items()}

# Image-Based Auto-Trigger region: how auto.trigger {x, y, w, h} maps to image pixels is NOT documented
# where Phantastic can read it. Assumption (UNVERIFIED ON HARDWARE): (x, y) is the offset of the region's
# CENTRE from the image centre, in pixels, y growing downwards like image rows. Reason: a Miro M310 held
# y = -52 with h = 400 on a 1280 x 504 image (data/miro_m310_tree.txt), which a top-left origin cannot
# express, and which this convention places flush with the top edge.
ROI_CONVENTION = 'centre offset from the image centre, y down (unverified on hardware)'


def roi_from_camera(x: int, y: int, w: int, h: int, image_w: int, image_h: int) -> tuple[int, int, int, int]:
    """auto.trigger {x, y, w, h} -> stored-image rectangle (left, top, w, h). See ROI_CONVENTION."""
    return int(x) - int(w) // 2 + int(image_w) // 2, int(y) - int(h) // 2 + int(image_h) // 2, int(w), int(h)


def roi_to_camera(left: int, top: int, w: int, h: int, image_w: int, image_h: int) -> tuple[int, int, int, int]:
    """Inverse of :func:`roi_from_camera` (exact for integers)."""
    return int(left) + int(w) // 2 - int(image_w) // 2, int(top) + int(h) // 2 - int(image_h) // 2, int(w), int(h)


def flatten(prefix: str, value, out: dict) -> dict:
    """{'trigger': {'x': 0}} under 'auto' -> {'auto.trigger.x': 0}."""
    if isinstance(value, dict):
        for k, v in value.items():
            flatten(f'{prefix}.{k}', v, out)
    else:
        out[prefix] = value
    return out


def read_structures(cam, names=STRUCTURES) -> dict[str, dict]:
    """``get <name>`` for each structure the camera knows (read only); unknown ones are left out."""
    out = {}
    for name in names:
        try:
            v = cam.get(name)
        except P.ProtocolError:
            continue
        if isinstance(v, dict):
            out[name] = v
    return out


def read_settings(cam) -> tuple[dict, dict]:
    """(settings, structures): every :data:`SETTINGS` key the camera reported, with its value, and the
    raw structures (``irig.sec``, ``cam.timezone`` ... are read from those)."""
    structs = read_structures(cam)
    flat: dict = {}
    for name, v in structs.items():
        flatten(name, v, flat)
    return {k: flat[k] for k in BY_KEY if k in flat}, structs


def type_ok(setting: Setting, value) -> bool:
    """Does the camera's value have the type the v2512 showed? A float may print as an integer (0)."""
    if setting.typ is str:
        return isinstance(value, str)
    if isinstance(value, bool):
        return False
    if setting.typ is float:
        return isinstance(value, (int, float))
    return isinstance(value, int)


def display(value) -> str:
    """Text for a field: as the protocol prints it, strings unquoted."""
    if isinstance(value, float):
        return f'{value:.10g}'
    return str(value)


def parse(setting: Setting, text: str):
    """Field text -> typed value. Raises ValueError with the variable's name."""
    if setting.typ is str:
        try:
            text.encode('latin-1')
        except UnicodeEncodeError:
            raise ValueError(f'{setting.key}: the camera protocol carries latin-1 text only') from None
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
            raise ValueError(f'{setting.key}: line breaks, tabs and other control characters cannot be sent '
                             '(the command is one line; the camera\'s escape for them is not known)')
        return text
    t = text.strip()
    try:
        if setting.typ is int:
            return int(t)
        v = float(t)
    except ValueError:
        raise ValueError(f'{setting.key}: {text!r} is not {"an integer" if setting.typ is int else "a number"}') \
            from None
    if not math.isfinite(v):
        raise ValueError(f'{setting.key}: {text!r} is not a finite number')
    return v


def same(setting: Setting, a, b) -> bool:
    if setting.typ is float:
        return math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-12)
    return a == b


def changes(current: dict, wanted: dict) -> dict:
    """The subset of ``wanted`` to send: editable keys the camera reported, whose value differs.

    Raises ValueError for a key the camera did not report, a read-only key, or a value of the wrong type.
    """
    out = {}
    for key, v in wanted.items():
        s = BY_KEY.get(key)
        if s is None or key not in current:
            raise ValueError(f'{key}: not reported by this camera; it is never written')
        if not s.editable:
            raise ValueError(f'{key}: read only in Phantastic')
        if not type_ok(s, current[key]):
            raise ValueError(f'{key}: the camera reported {current[key]!r}, not {_KIND[s.typ]}; not written')
        if not type_ok(s, v):
            raise ValueError(f'{key}: {v!r} is not {_KIND[s.typ]}')
        if s.typ is float:
            v = float(v)
        if not same(s, current[key], v):
            out[key] = v
    return out


def configure_kwargs(chg: dict) -> dict:
    """The defc part of ``chg`` as ``Camera.configure`` keywords."""
    return {_DEFC_KEYWORD[k.split('.', 1)[1]]: v for k, v in chg.items() if BY_KEY[k].via_configure}


def set_lines(chg: dict) -> list[str]:
    """The exact command lines :func:`apply_changes` will send for ``chg``, in order."""
    from .camera import acquisition_update
    lines = []
    kw = configure_kwargs(chg)
    if kw:
        lines.append(P.set_line('defc', acquisition_update(**kw), ' '))
    lines += [P.set_line(k, v) for k, v in chg.items() if not BY_KEY[k].via_configure]
    return lines


def apply_changes(cam, chg: dict) -> dict:
    """Send ``chg`` (from :func:`changes`) and return {key: value read back by the camera}.

    defc fields go in one ``configure`` call (only those fields), the others one ``Camera.set`` each.
    """
    back = {}
    kw = configure_kwargs(chg)
    if kw:
        d = cam.configure(**kw)
        for k in chg:
            if BY_KEY[k].via_configure:
                back[k] = flatten('defc', d, {}).get(k)
    for k, v in chg.items():
        if not BY_KEY[k].via_configure:
            back[k] = cam.set(k, v)
    return back


# ----------------------------------------------------------------------------- host-side backup (PCC p.31-32)

def backup_dict(current: dict, info: dict) -> dict:
    """A JSON-ready backup of the editable settings the camera reported (PCC 'Backup & restore', p.31-32,
    kept on the host: nothing is written to the camera's memory slots)."""
    return dict(format=BACKUP_FORMAT, version=BACKUP_VERSION, saved=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                camera=dict(model=info.get('model'), serial=info.get('serial'), swver=info.get('swver')),
                settings={k: v for k, v in current.items() if k in BY_KEY and BY_KEY[k].editable})


def save_backup(path, current: dict, info: dict):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(backup_dict(current, info), f, indent=1, ensure_ascii=False)


def load_backup(path) -> dict:
    """The backup's settings dict. Raises ValueError for a file that is not a Phantastic settings backup."""
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    if not isinstance(d, dict) or d.get('format') != BACKUP_FORMAT or not isinstance(d.get('settings'), dict):
        raise ValueError(f'{path} is not a Phantastic camera-settings backup')
    if d.get('version') != BACKUP_VERSION:
        raise ValueError(f'{path}: backup version {d.get("version")!r}, this Phantastic reads {BACKUP_VERSION}')
    return d


def backup_diff(current: dict, saved: dict) -> tuple[dict, list[str]]:
    """(wanted, notes): the saved values that differ from the camera, and why any saved value is not used.

    Nothing is sent: the caller shows ``wanted`` and the user applies it.
    """
    wanted, notes = {}, []
    for key, v in saved.items():
        s = BY_KEY.get(key)
        if s is None:
            notes.append(f'{key}: not a setting this Phantastic knows; skipped')
        elif key not in current:
            notes.append(f'{key}: this camera does not report it; skipped')
        elif not s.editable:
            notes.append(f'{key}: read only; skipped')
        elif not type_ok(s, v):
            notes.append(f'{key}: saved value {v!r} is not {_KIND[s.typ]}; skipped')
        elif not same(s, current[key], v):
            wanted[key] = float(v) if s.typ is float else v
    return wanted, notes
