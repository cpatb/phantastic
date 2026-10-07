"""Command line: ``phantastic <command> ...`` (also ``python -m phantastic``)."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from . import protocol as P


def _cam(a):
    from .camera import Camera
    return Camera(a.ip, a.port, timeout=a.timeout).connect()


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (P.Resolution, P.Flags)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def cmd_discover(a):
    from .camera import discover
    cams = discover(timeout=a.timeout)
    for c in cams:
        print(c)
    if not cams:
        print('no cameras answered (check subnet 100.100.x.x / firewall for UDP 7380)')
    return 0 if cams else 1


def cmd_info(a):
    cam = _cam(a)
    try:
        out = {'info': {k: cam._safe_get(f'info.{k}') for k in
                        ('model', 'serial', 'name', 'hwver', 'swver', 'fver', 'features', 'imgformats', 'maxcines')},
               'defc': cam.acquisition(), 'cstats': cam.cine_states()}
        print(json.dumps(_jsonable(out), indent=1))
    finally:
        if a.transcript:
            with open(a.transcript, 'w', encoding='latin-1', newline='') as f:
                for c, r in cam.transcript:
                    f.write(f'>>> {c}\r\n{r}\r\n')
        cam.close()
    return 0


def cmd_set(a):
    cam = _cam(a)
    res = tuple(int(x) for x in a.res.lower().split('x')) if a.res else None
    d = cam.configure(resolution=res, rate=a.rate, exposure_ns=a.exposure_ns, post_trigger=a.post_trigger)
    print(json.dumps(_jsonable(d), indent=1))
    cam.close()
    return 0


def cmd_record(a):
    cam = _cam(a)
    cam.record(a.cine)
    print('recording into cine', a.cine if a.cine is not None else '(next ready)')
    cam.close()
    return 0


def cmd_trigger(a):
    cam = _cam(a)
    cam.trigger()
    cam.close()
    return 0


def cmd_live(a):
    import numpy as np
    import tifffile
    cam = _cam(a)
    try:
        img, hdr = cam.live_image(a.format)
    finally:
        cam.close()
    tifffile.imwrite(a.out, img)
    print(f'{a.out}: {img.shape} {img.dtype} min {img.min()} max {img.max()} '
          f'(all multiples of 16: {bool(np.all(img % 16 == 0))}) header {hdr}')
    return 0


def _resolve_count(a, first_default=None):
    """--count n: end the range at the n-th kept image after --first."""
    if getattr(a, 'count', None) is None:
        return
    if a.last is not None:
        raise SystemExit('give either --last or --count, not both')
    first = a.first if a.first is not None else first_default
    if first is None:
        raise SystemExit('--count needs --first')
    from .decimate import last_for_count
    a.last = last_for_count(first, a.count, a.step, a.align)


def _crop(a):
    """--crop x,y,w,h -> tuple (validated against the image by the exporter)."""
    if getattr(a, 'crop', None) is None:
        return None
    from .crop import parse_crop
    try:
        return parse_crop(a.crop)
    except ValueError as e:
        raise SystemExit(str(e))


def cmd_download(a):
    _resolve_count(a)
    if a.all:
        if a.cine is not None or a.out is not None:
            raise SystemExit('--all saves every stored cine: give --out-dir (and --name), not --cine/--out')
        if a.out_dir is None:
            raise SystemExit('--all needs --out-dir')
    elif a.cine is None or a.out is None:
        raise SystemExit('give --cine and --out (or --all --out-dir DIR)')
    cam = _cam(a)
    t0 = time.time()

    def progress(done, total):
        print(f'\r{done}/{total}', end='', file=sys.stderr)
    kw = dict(first=a.first, last=a.last, step=a.step, fmt=a.format, align=a.align, as_12bit=a.as_12bit,
              crop=_crop(a), fill_flags=not a.keep_flagged)
    try:
        if a.all:
            info = {'saved': cam.download_all(a.out_dir, a.name, progress=progress, **kw)}
        else:
            out = a.out
            if '{' in out:          # file-name tokens (phantastic.naming); never overwrite a file
                from .naming import expand_name, unique_path
                out = str(unique_path(expand_name(out, **cam.name_fields(a.cine))))
            part = out + '.part'           # renamed only when complete (never a truncated .cine)
            try:
                info = cam.download(a.cine, part, progress=progress, **kw)
                import os
                os.replace(part, out)
            except BaseException:
                import pathlib
                pathlib.Path(part).unlink(missing_ok=True)
                raise
            info['path'] = out
    finally:
        cam.close()
    print(file=sys.stderr)
    info['seconds'] = round(time.time() - t0, 2)
    print(json.dumps(_jsonable(info), indent=1))
    return 0


def _first_in_file(path):
    from .cine import CineReader
    with CineReader(path) as r:
        return r.first


def cmd_decimate(a):
    _resolve_count(a, _first_in_file(a.src) if a.count is not None else None)
    from .decimate import decimate_cine
    print(json.dumps(decimate_cine(a.src, a.dst, a.step, a.align, a.first, a.last, crop=_crop(a)), indent=1))
    return 0


def cmd_tiff(a):
    _resolve_count(a, _first_in_file(a.src) if a.count is not None else None)
    if a.sequence:
        from .export import export_tiff_sequence
        res = export_tiff_sequence(a.src, a.dst, a.first, a.last, a.step, a.align, pattern=a.pattern,
                                   crop=_crop(a), pcc_table=a.pcc_table, overwrite=a.overwrite)
        res['files'] = f'{len(res["files"])} files: {res["files"][0]} .. {res["files"][-1]}'
    else:
        from .decimate import export_tiff
        res = export_tiff(a.src, a.dst, a.first, a.last, a.step, a.align, pcc_table=a.pcc_table, crop=_crop(a))
    print(json.dumps(_jsonable(res), indent=1))
    return 0


def cmd_mp4(a):
    _resolve_count(a, _first_in_file(a.src) if a.count is not None else None)
    from .export import export_mp4
    res = export_mp4(a.src, a.dst, a.first, a.last, a.step, a.align, fps=a.fps, black=a.black, white=a.white,
                     gamma=a.gamma, crop=_crop(a), rotate=a.rotate, flip_h=a.flip_h, flip_v=a.flip_v,
                     border=a.border, crf=a.crf)
    print(json.dumps(_jsonable(res), indent=1))
    print('note: an 8-bit display render for viewing, NOT for measurement', file=sys.stderr)
    return 0


def cmd_selftest(a):
    from .selftest import run
    res = run(a.ip, a.port, outdir=a.out, record_cine=a.record_cine)
    return 0 if all(r.status != 'FAIL' for r in res) else 1


def cmd_simulate(a):
    from .simulator import CameraModel, Simulator
    sim = Simulator(a.host, a.port, model=CameraModel(profile=a.profile)).start()
    print(f'simulated camera on {a.host}:{sim.port}; Ctrl+C to stop')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


CROP_HELP = 'x,y,w,h: keep only this rectangle (0-based, rows counted top-down); recorded in the output'


def main(argv=None):
    ap = argparse.ArgumentParser(prog='phantastic', description='Open-source Phantom camera control and cine tools')
    ap.add_argument('--version', action='version', version=__version__)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def camera_args(p):
        p.add_argument('--ip', required=True)
        p.add_argument('--port', type=int, default=P.CONTROL_PORT)
        p.add_argument('--timeout', type=float, default=10.0)

    p = sub.add_parser('discover', help='find cameras on the network (UDP 7380)')
    p.add_argument('--timeout', type=float, default=1.5)
    p.set_defaults(fn=cmd_discover)
    p = sub.add_parser('info', help='print identity, acquisition settings and cine states')
    camera_args(p)
    p.add_argument('--transcript', help='write every command and raw response to this file')
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser('set', help='set acquisition parameters (read back what the camera accepted)')
    camera_args(p)
    p.add_argument('--res', help='WxH')
    p.add_argument('--rate', type=float)
    p.add_argument('--exposure-ns', type=int)
    p.add_argument('--post-trigger', type=int)
    p.set_defaults(fn=cmd_set)
    p = sub.add_parser('record', help='start recording (deletes what that cine held)')
    camera_args(p)
    p.add_argument('--cine', type=int)
    p.set_defaults(fn=cmd_record)
    p = sub.add_parser('trigger', help='software trigger')
    camera_args(p)
    p.set_defaults(fn=cmd_trigger)
    p = sub.add_parser('live', help='save one live image as TIFF')
    camera_args(p)
    p.add_argument('--format', default='P16', choices=list(P.IMAGE_FORMATS))
    p.add_argument('--out', required=True)
    p.set_defaults(fn=cmd_live)
    p = sub.add_parser('download', help='download a stored cine (or a decimated range) to a .cine file')
    camera_args(p)
    p.add_argument('--cine', type=int)
    p.add_argument('--out', help='output .cine; may hold tokens such as cine{cinenr}_{serial} (then a '
                   'name that exists gets _1, _2, ...)')
    p.add_argument('--all', action='store_true', help='Save All RAM Cines: every stored cine into --out-dir')
    p.add_argument('--out-dir', help='folder for --all')
    p.add_argument('--name', help='file-name template for --all (default cine{cinenr}_{serial}; tokens: '
                   '{cinenr} {serial} {camname} {date} {time} {count}, digit = min width, e.g. {cinenr3})')
    p.add_argument('--first', type=int)
    p.add_argument('--last', type=int)
    p.add_argument('--count', type=int, help='save this many images starting at --first (instead of --last)')
    p.add_argument('--step', type=int, default=1)
    p.add_argument('--align', default='trigger', choices=['trigger', 'first'])
    p.add_argument('--crop', help=CROP_HELP)
    p.add_argument('--format', default='P16', choices=list(P.IMAGE_FORMATS))
    p.add_argument('--as-12bit', action='store_true',
                   help="store value >> 4 as 12-bit in PCC's layout (PCC shows 16-bit files white); "
                        'drops any sub-count correction fraction')
    p.add_argument('--keep-flagged', action='store_true',
                   help="keep the pixels the camera flags as defective (0xFF00 in P16) instead of PCC's fill-in "
                        '(mean of the 8 neighbours)')
    p.set_defaults(fn=cmd_download)
    p = sub.add_parser('decimate', help='lossless decimated copy of a cine file')
    p.add_argument('src')
    p.add_argument('dst')
    p.add_argument('--step', type=int, required=True)
    p.add_argument('--align', default='trigger', choices=['trigger', 'first'])
    p.add_argument('--first', type=int)
    p.add_argument('--last', type=int)
    p.add_argument('--count', type=int, help='keep this many images starting at --first (instead of --last)')
    p.add_argument('--crop', help=CROP_HELP)
    p.set_defaults(fn=cmd_decimate)
    p = sub.add_parser('tiff', help='export raw values to a 16-bit multi-page TIFF (or PCC-identical with --pcc-table)')
    p.add_argument('src')
    p.add_argument('dst')
    p.add_argument('--step', type=int, default=1)
    p.add_argument('--align', default='trigger', choices=['trigger', 'first'])
    p.add_argument('--first', type=int)
    p.add_argument('--last', type=int)
    p.add_argument('--count', type=int, help='export this many images starting at --first (instead of --last)')
    p.add_argument('--pcc-table', help="write what PCC's 8-bit export would write: 'auto' picks the shipped "
                   "table matching the file's display settings, or give a table .csv")
    p.add_argument('--crop', help=CROP_HELP)
    p.add_argument('--sequence', action='store_true', help='one TIFF per image; dst is a folder')
    p.add_argument('--pattern', default='{source}_{image6}',
                   help='--sequence file names: {source} {image} {+image}, digit = min width; a negative '
                        "image number is written m + digits (-123 -> m000123). Default '%(default)s'")
    p.add_argument('--overwrite', action='store_true', help='--sequence: replace files that exist')
    p.set_defaults(fn=cmd_tiff)
    p = sub.add_parser('mp4', help='H.264 MP4 for presentations (8-bit display render, NOT for measurement)')
    p.add_argument('src')
    p.add_argument('dst')
    p.add_argument('--step', type=int, default=1)
    p.add_argument('--align', default='trigger', choices=['trigger', 'first'])
    p.add_argument('--first', type=int)
    p.add_argument('--last', type=int)
    p.add_argument('--count', type=int, help='export this many images starting at --first (instead of --last)')
    p.add_argument('--fps', type=float, default=30.0, help='movie playback rate (default 30)')
    p.add_argument('--black', type=float, help='raw value shown black (default 0)')
    p.add_argument('--white', type=float, help='raw value shown white (default full scale of RealBPP)')
    p.add_argument('--gamma', type=float, default=1.0, help='display gamma; >1 brightens mid-tones')
    p.add_argument('--rotate', type=int, default=0, choices=[0, 90, 180, 270], help='degrees clockwise')
    p.add_argument('--flip-h', action='store_true')
    p.add_argument('--flip-v', action='store_true')
    p.add_argument('--border', action='store_true',
                   help='burn in image number, time from trigger, rate and exposure on a strip below the image')
    p.add_argument('--crf', type=int, default=18, help='x264 quality (lower = better; default 18)')
    p.add_argument('--crop', help=CROP_HELP)
    p.set_defaults(fn=cmd_mp4)
    p = sub.add_parser('selftest', help='run the camera test (identity, live, formats, time stamps) and write a report')
    camera_args(p)
    p.add_argument('--out', help='report folder (default: ./phantastic_selftest_<time>)')
    p.add_argument('--record-cine', type=int, help='record a test clip into this cine (ERASES what it holds)')
    p.set_defaults(fn=cmd_selftest)
    p = sub.add_parser('simulate', help='run a simulated camera')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=P.CONTROL_PORT)
    p.add_argument('--profile', help="e.g. 'miro-m310'")
    p.set_defaults(fn=cmd_simulate)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except P.ProtocolError as e:
        print(f'camera error: {e}', file=sys.stderr)
        return 2
    except (FileExistsError, ValueError, RuntimeError) as e:     # refused input; nothing was written
        print(f'error: {e}', file=sys.stderr)
        return 4
    except (ConnectionError, OSError) as e:
        print(f'connection error: {e}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
