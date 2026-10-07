"""Camera self-test: the hardware checklist, run automatically, with a written report.

    phantastic selftest --ip 100.100.1.7 [--record-cine 2]

Steps (each reports PASS / WARN / FAIL / SKIP and keeps going where it can):
  1 network     adapters and whether one is on a Phantom subnet
  2 discover    does the camera answer UDP discovery
  3 identity    model, serial, firmware, features, image formats (read only)
  4 live        one live P16 frame: size, value range, MSB alignment (multiples of 16)
  5 record      ONLY with --record-cine N: record into cine N (erases what it held) and trigger
  6 formats     the same 20 stored frames downloaded in every offered format, each cross-checked
                against P16 (P16/16 = P12L = linearised P10 within its step = 8-bit within 1;
                P16R differs from P16 only by the camera's correction). Settles the P12L bit
                order on real hardware: the report says which order matches.
  7 times       time stamps: count, monotonic, spacing vs 1/frame rate
The report (text) and the downloaded arrays (.npz) go to the output folder; the camera session log
records every command.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import protocol as P
from .camera import Camera, discover, network_report
from .cine import load_linlut10

N_FRAMES = 20


@dataclass
class Result:
    step: str
    status: str          # PASS / WARN / FAIL / SKIP
    detail: str
    data: dict = field(default_factory=dict)

    def line(self):
        return f'[{self.status:4s}] {self.step}: {self.detail}'


def _p12l_alternatives(raw: bytes, w: int, h: int, n: int) -> dict[str, np.ndarray]:
    """P12L decoded under the two plausible bit orders (MSB-first is Phantastic's)."""
    b = np.frombuffer(raw, np.uint8).astype(np.int64).reshape(-1, 3)
    msb = np.stack([(b[:, 0] << 4) | (b[:, 1] >> 4), ((b[:, 1] & 15) << 8) | b[:, 2]], 1).ravel()
    lsb = np.stack([b[:, 0] | ((b[:, 1] & 15) << 8), (b[:, 1] >> 4) | (b[:, 2] << 4)], 1).ravel()
    return {'MSB-first': msb.reshape(n, h, w), 'LSB-first': lsb.reshape(n, h, w)}


def run(ip: str, port: int = P.CONTROL_PORT, outdir=None, record_cine: int | None = None,
        attach_port: int = P.ATTACH_PORT, log_file=None, say: Callable[[str], None] = print,
        cancelled: Callable[[], bool] = lambda: False, cam: Camera | None = None) -> list[Result]:
    """Run the checks. Pass ``cam`` to reuse an open connection (the app does: a camera has one
    data stream, so a second connection would cut the app's live view); it is then left open."""
    own = cam is None
    out = Path(outdir) if outdir else Path.cwd() / f'phantastic_selftest_{time.strftime("%Y%m%d_%H%M%S")}'
    out.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []

    def add(r: Result):
        results.append(r)
        say(r.line())

    # 1 network
    rep = network_report()
    on_subnet = [r for r in rep if 'Phantom' in r]
    add(Result('network', 'PASS' if on_subnet else 'WARN',
               ('adapter on a Phantom subnet: ' + on_subnet[0]) if on_subnet else
               'no adapter on 100.100.x.x or 172.16.x.x; give the camera adapter 100.100.100.1 / 255.255.0.0',
               {'adapters': rep}))
    # 2 discover
    found = discover(timeout=1.5)
    mine = [c for c in found if c.ip == ip]
    add(Result('discover', 'PASS' if mine else 'WARN',
               (f'{mine[0]}' if mine else f'{ip} did not answer discovery (found: {[str(c) for c in found] or "none"}); '
                'connecting directly'), {'found': [str(c) for c in found]}))
    # 3 identity
    try:
        if cam is None:
            cam = Camera(ip, port, timeout=10, attach_port=attach_port, log_file=log_file).connect()
    except OSError as e:
        add(Result('identity', 'FAIL', f'cannot connect to {ip}:{port}: {e}'))
        return _finish(results, out, say)
    cam.note('---- self-test start ----')
    try:
        ident = {k: cam._safe_get(f'info.{k}') for k in ('model', 'serial', 'name', 'hwver', 'swver', 'fver', 'sver',
                                                          'pver', 'features', 'imgformats', 'maxcines')}
        formats = str(ident.get('imgformats') or 'P16').split()
        add(Result('identity', 'PASS' if ident.get('serial') is not None else 'WARN',
                   f'{ident.get("model")} serial {ident.get("serial")}, firmware {ident.get("fver")}/{ident.get("swver")}, '
                   f'formats {formats}, features {ident.get("features")}', {'info': {k: str(v) for k, v in ident.items()}}))
        acq = cam.acquisition()
        add(Result('settings', 'PASS', f'res {acq.get("res")}, rate {acq.get("rate")}, exposure {acq.get("exp")} ns, '
                   f'post-trigger {acq.get("ptframes")}', {'defc': {k: str(v) for k, v in acq.items()}}))
        if cancelled():
            return _finish(results, out, say, cam, own)
        # 4 live
        try:
            fr, hdr = cam.read_images(-1, 0, 1, 'P16' if 'P16' in formats else formats[0])
            f = fr[0]
            aligned = bool(np.all(f % 16 == 0)) if f.dtype == np.uint16 else None
            add(Result('live', 'PASS', f'{hdr["fmt"]} {f.shape[1]}x{f.shape[0]}, values {int(f.min())}..{int(f.max())}, '
                       f'low 4 bits all zero (12-bit x 16 exactly): {aligned}', {'aligned': aligned}))
            np.savez_compressed(out / 'live.npz', frame=f)
        except (P.ProtocolError, OSError, ValueError) as e:
            add(Result('live', 'FAIL', f'{type(e).__name__}: {e}'))
        # 5 record (opt-in)
        cine = None
        if record_cine is not None:
            try:
                cam.record(record_cine)
                time.sleep(0.5)
                cam.trigger()
                st = cam.wait_stored(record_cine, timeout=60)
                cine = record_cine
                add(Result('record', 'PASS', f'cine {record_cine} recorded and triggered: {st}'))
            except (P.ProtocolError, OSError, TimeoutError) as e:
                add(Result('record', 'FAIL', f'{type(e).__name__}: {e}'))
        else:
            states = cam.cine_states()
            stored = [int(k[1:]) for k, v in states.items() if k[1:].isdigit() and isinstance(v, P.Flags) and 'STR' in v]
            cine = stored[0] if stored else None
            add(Result('record', 'SKIP', f'not asked to record; using stored cine {cine}' if cine is not None
                       else 'not asked to record and no stored cine; format tests skipped'))
        if cine is None or cancelled():
            return _finish(results, out, say, cam, own)
        # 6 formats
        ci = cam.cine_info(cine)
        start = max(int(ci['firstfr']), min(0, int(ci['lastfr']) - N_FRAMES + 1))
        n = min(N_FRAMES, int(ci['lastfr']) - start + 1)
        ref = None
        arrays = {}
        for fmt in [f for f in ('P16', 'P16R', 'P12L', 'P10', '8', '8R') if f in formats]:
            try:
                raw, hdr = cam.read_images_raw(cine, start, n, fmt)
                res = hdr['res']
                frames = P.decode_frames(raw, hdr['fmt'], res.width, res.height, n)
                arrays[fmt] = frames
                arrays[fmt + '_raw'] = np.frombuffer(raw, np.uint8)
            except (P.ProtocolError, OSError, ValueError) as e:
                add(Result(f'format {fmt}', 'FAIL', f'{type(e).__name__}: {e}'))
                continue
            if fmt == 'P16':
                ref = frames.astype(np.int64)
                al = bool(np.all(ref % 16 == 0))
                add(Result('format P16', 'PASS', f'{n} frames from image {start}; low 4 bits zero: {al}; '
                           f'values {int(ref.min())}..{int(ref.max())}', {'aligned': al}))
                continue
            if ref is None:
                add(Result(f'format {fmt}', 'WARN', 'downloaded, no P16 reference to compare'))
                continue
            # P16 is full scale (12-bit value x 16). The simulator leaves the low 4 bits zero; a real v2512
            # (serial 21598, 2026-10-07) fills them, so compare on the 12-bit scale P16/16, never on raw P16.
            v12 = ref / 16.0
            if fmt == 'P12L':
                alts = _p12l_alternatives(raw, res.width, res.height, n)
                match = {k: float(np.mean(np.abs(a - v12) < 1)) for k, a in alts.items()}
                best = max(match, key=match.get)
                ok = match['MSB-first'] == 1.0
                add(Result('format P12L', 'PASS' if ok else 'FAIL',
                           'within 1 of P16/16: ' + ', '.join(f'{k} {v:.4f}' for k, v in match.items())
                           + ('' if ok else f'  -> camera uses {best}; Phantastic must be changed'), {'match': match}))
            elif fmt == 'P10':
                lin = load_linlut10()[frames].astype(np.int64)
                # companding step of the LUT at each value bounds the honest error
                d = np.abs(lin - v12)
                add(Result('format P10', 'PASS' if np.percentile(d, 99) <= 40 else 'FAIL',
                           f'|linearised P10 - P16/16|: median {np.median(d):.1f}, 99th pct {np.percentile(d, 99):.1f}, '
                           f'max {d.max():.1f} (companding steps reach ~32 at the top)', {}))
            elif fmt in ('8', '8R'):
                d = np.abs(frames.astype(np.float64) - ref / 256.0)
                add(Result(f'format {fmt}', 'PASS' if d.max() <= 1 or fmt == '8R' else 'WARN',
                           f'|8-bit - P16/256| max {d.max():.2f} (8R is uncorrected, may differ)', {}))
            elif fmt == 'P16R':
                d = frames.astype(np.int64) - ref
                add(Result('format P16R', 'PASS', f'P16R - P16 (the camera\'s FPN/PRNU correction): mean {d.mean():.2f}, '
                           f'sd {d.std():.2f}, max |d| {np.abs(d).max()}', {}))
        np.savez_compressed(out / 'formats.npz', **arrays)
        # 7 time stamps
        try:
            ts = cam.read_time_stamps(cine, start, n)
            t = np.array([s.seconds_of_year for s in ts])
            dt = np.diff(t)
            rate = float(ci['rate'] or 0)
            exp_dt = 1 / rate if rate else float('nan')
            ok = len(ts) == n and np.all(dt > 0) and (not rate or np.allclose(dt, exp_dt, rtol=0.05, atol=2e-6))
            add(Result('times', 'PASS' if ok else 'WARN',
                       f'{len(ts)} stamps, spacing {np.median(dt) * 1e6:.3f} us (1/rate = {exp_dt * 1e6:.3f} us), '
                       f'exposure {ts[0].exptime_us} us, locked {ts[0].locked}', {}))
        except (P.ProtocolError, OSError, ValueError) as e:
            add(Result('times', 'FAIL', f'{type(e).__name__}: {e}'))
    except Exception as e:   # report anything unexpected rather than losing the partial results
        add(Result('error', 'FAIL', f'{type(e).__name__}: {e}'))
    return _finish(results, out, say, cam, own)


def _finish(results, out, say, cam=None, own=True):
    log = cam.log_file if cam is not None else None
    if cam is not None:
        cam.note('---- self-test end ----')
        if own:
            cam.close()
    lines = [r.line() for r in results]
    if log:
        lines.append(f'camera session log: {log}')
    (out / 'report.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    (out / 'report.json').write_text(json.dumps([vars(r) for r in results], indent=1, default=str))
    say(f'report: {out / "report.txt"}')
    return results
