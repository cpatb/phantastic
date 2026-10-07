"""Extract a real camera's parameter tree into a simulator profile.

Source: DiamondLightSource/miroCamera (Apache-2.0), documentation/logs/miroTelnet2.log, the answer
of a Phantom Miro M310 (serial 17277) to ``get *``. The camera truncates that answer at 64 KB, so
only the complete top-level structures are kept.

    python tools/make_m310_profile.py <miroTelnet2.log> phantastic/data/miro_m310_tree.txt
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phantastic.protocol import Flags, Resolution, _Parser, _tokens, clean_line, split_response  # noqa: E402


def compact(v) -> str:
    if isinstance(v, dict):
        return '{' + ', '.join(f'{k} : {compact(x)}' for k, x in v.items()) + '}'
    if isinstance(v, Resolution):
        return f'{v.width} x {v.height}'
    if isinstance(v, Flags):
        return '{ ' + ' '.join(v.names) + ' }'
    if isinstance(v, list):
        return '{ ' + ' '.join(compact(x) for x in v) + ' }'
    if isinstance(v, str):
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(v, float):
        return f'{v:.10g}'
    return str(v)


def main(log_path, out_path):
    buf = Path(log_path).read_bytes()
    while True:
        line, buf = split_response(buf)
        if line is None:
            raise SystemExit('no "get *" answer found')
        if line.startswith(b'* :'):
            break
    s = clean_line(line).strip()[3:].strip()
    p = _Parser(_tokens(s))
    p.take()  # opening brace
    tree = {}
    while True:
        try:
            _, name = p.take()
            p.take()
            tree[name] = p.value()
            if p.peek()[0] == 3:
                p.take()
        except Exception:   # the truncated last member: stop at the last complete one
            break
    with open(out_path, 'w', encoding='latin-1', newline='\n') as f:
        f.write('# Phantom Miro M310 (serial 17277) parameter tree, from "get *" in miroTelnet2.log,\n')
        f.write('# DiamondLightSource/miroCamera (Apache-2.0). Truncated by the camera at 64 KB;\n')
        f.write('# complete top-level structures only. One "name : value" per line.\n')
        for k, v in tree.items():
            f.write(f'{k} : {compact(v)}\n')
    print(f'{len(tree)} structures -> {out_path}')


if __name__ == '__main__':
    main(*sys.argv[1:3])
