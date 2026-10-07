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


def cmd_download(a):
    _resolve_count(a)
    cam = _cam(a)
    t0 = time.time()

    def progress(done, total):
        print(f'\r{done}/{total}', end='', file=sys.stderr)
    try:
        info = cam.download(a.cine, a.out, first=a.first, last=a.last, step=a.step, fmt=a.format,
                            align=a.align, progress=progress, as_12bit=a.as_12bit)
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
    print(json.dumps(decimate_cine(a.src, a.dst, a.step, a.align, a.first, a.last), indent=1))
    return 0


def cmd_tiff(a):
    _resolve_count(a, _first_in_file(a.src) if a.count is not None else None)
    from .decimate import export_tiff
    print(json.dumps(export_tiff(a.src, a.dst, a.first, a.last, a.step, a.align, pcc_table=a.pcc_table), indent=1))
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
    p.add_argument('--cine', type=int, required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--first', type=int)
    p.add_argument('--last', type=int)
    p.add_argument('--count', type=int, help='save this many images starting at --first (instead of --last)')
    p.add_argument('--step', type=int, default=1)
    p.add_argument('--align', default='trigger', choices=['trigger', 'first'])
    p.add_argument('--format', default='P16', choices=list(P.IMAGE_FORMATS))
    p.add_argument('--as-12bit', action='store_true',
                   help="store value >> 4 as 12-bit in PCC's layout (PCC shows 16-bit files white); "
                        'drops any sub-count correction fraction')
    p.set_defaults(fn=cmd_download)
    p = sub.add_parser('decimate', help='lossless decimated copy of a cine file')
    p.add_argument('src')
    p.add_argument('dst')
    p.add_argument('--step', type=int, required=True)
    p.add_argument('--align', default='trigger', choices=['trigger', 'first'])
    p.add_argument('--first', type=int)
    p.add_argument('--last', type=int)
    p.add_argument('--count', type=int, help='keep this many images starting at --first (instead of --last)')
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
    p.set_defaults(fn=cmd_tiff)
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
    except (ConnectionError, OSError) as e:
        print(f'connection error: {e}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
