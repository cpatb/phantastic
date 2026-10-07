"""File-name templates for Save Cine, Save All RAM Cines and image-sequence export.

Tokens in curly braces, after PCC's Automatic File Naming (PCC 3.11 manual p.66-73):

  {cinenr}   camera cine (partition) number           {serial}   camera serial number
  {camname}  camera name ({name} is PCC's spelling)   {source}   source file name (stem) or default base
  {date}     trigger date, PCC's sortable 'Yyyyymmdd' {time}     trigger time of day, PCC's 'Hhhmmss'
  {count}    position in this batch (1, 2, ...)       {image}    signed camera image number
  {+image}   image number made non-negative (image - first image of the range)

A trailing digit sets a minimum width with leading zeros (PCC's "minimum width feature", p.69):
``{image6}`` -> ``000123``; ``{image:06d}`` is accepted as the same thing. A negative number is
written ``m`` + its zero-padded magnitude (``{image6}`` of -123 -> ``m000123``), so a name never
starts with '-' and never mixes signs with padding. PCC calls the number "signed (+/-)" and does
not say how it writes a negative one; this spelling is Phantastic's. Names with ``m`` sort after
the non-negative ones; use ``{+image}`` when a third-party tool needs sorted, positive numbers.

Differences from PCC, on purpose: an unknown token is an error (PCC leaves it in the name, p.70),
and {count} counts within one batch (PCC's {save} is a persistent counter, p.67).
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

INT_TOKENS = ('cinenr', 'serial', 'count', 'image', 'image_pos')
STR_TOKENS = ('camname', 'date', 'time', 'source')
ALIASES = {'name': 'camname', '+image': 'image_pos'}          # PCC spelling -> field name
FORBIDDEN = '/\\:*?"<>|.'            # PCC p.69: not allowed in a file name; replaced by '_' in a VALUE
DEFAULT_CINE_TEMPLATE = 'cine{cinenr}_{serial}'                # today's Save Cine default name
DEFAULT_SEQUENCE_PATTERN = '{source}_{image6}'

_TOKEN = re.compile(r'\{([^{}]*)\}')
_INNER = re.compile(r'^(\+?[A-Za-z_]+?)(\d*)(?::0?(\d+)d)?$')


def _parse(inner: str) -> tuple[str, int]:
    m = _INNER.match(inner.strip())
    if not m:
        raise ValueError(f'malformed token {{{inner}}}')
    tok, digits, spec = m.group(1), m.group(2), m.group(3)
    if digits and spec:
        raise ValueError(f'token {{{inner}}} gives a width twice')
    key = ALIASES.get(tok, tok)
    if key not in INT_TOKENS + STR_TOKENS:
        raise ValueError(f'unknown token {{{tok}}} (known: '
                         + ', '.join('{' + t + '}' for t in INT_TOKENS[:-1] + STR_TOKENS + ('name', '+image')) + ')')
    width = int(digits or spec or 0)
    if width and key in STR_TOKENS:
        raise ValueError(f'token {{{inner}}}: a width applies only to numbers')
    return key, width


def tokens_in(template: str) -> set[str]:
    """Field names used by a template (aliases resolved); raises on unknown or malformed tokens."""
    _check_braces(template)
    return {_parse(m.group(1))[0] for m in _TOKEN.finditer(template)}


def _check_braces(template: str):
    if _TOKEN.sub('', template).count('{') or _TOKEN.sub('', template).count('}'):
        raise ValueError(f'unbalanced brace in {template!r}')


def format_number(v: int, width: int = 0) -> str:
    """Zero-padded to ``width``; negative numbers as 'm' + padded magnitude (see module docstring)."""
    v = int(v)
    return ('m' if v < 0 else '') + str(abs(v)).zfill(width)


def sanitize(value: str) -> str:
    """Replace characters PCC forbids in a file name with '_' (PCC p.69 does this for {name})."""
    return ''.join('_' if (c in FORBIDDEN or ord(c) < 32) else c for c in str(value))


def expand_name(template: str, **fields) -> str:
    """Replace every token in ``template`` with its field (see module docstring).

    Raises ValueError for an unknown or malformed token, an unbalanced brace, or a token whose
    field is missing (None). Literal text (including path separators) is kept as typed.
    """
    _check_braces(template)

    def sub(m):
        key, width = _parse(m.group(1))
        v = fields.get(key)
        if v is None:
            raise ValueError(f'token {{{m.group(1)}}} has no value here')
        if key in INT_TOKENS:
            return format_number(v, width)
        return sanitize(v)
    return _TOKEN.sub(sub, template)


def time_fields(unix_seconds: float | None) -> dict:
    """{date, time} in PCC's formats (p.67), local time, from a trigger time; None -> now.

    PCC does not say whether {date}/{time} are the trigger time or the save time; Phantastic uses
    the trigger time so a name does not change with when the file was saved.
    """
    t = _dt.datetime.fromtimestamp(unix_seconds) if unix_seconds else _dt.datetime.now()
    return {'date': t.strftime('Y%Y%m%d'), 'time': t.strftime('H%H%M%S')}


def cine_fields(cinenr: int, serial=None, camname=None, trigger_secs: float | None = None, count: int = 1) -> dict:
    """Token values for a camera cine. {camname} falls back to 'serial<N>' (PCC builds {name}
    from the serial number when the camera has none, p.68)."""
    if not camname and serial is not None:
        camname = f'serial{serial}'
    return dict(cinenr=int(cinenr), serial=None if serial is None else int(serial),
                camname=str(camname) if camname else None, count=count, **time_fields(trigger_secs))


def unique_path(path) -> Path:
    """``path`` if free, else the first free ``stem_1.ext``, ``stem_2.ext``, ... (never overwrites).

    PCC's own safety net appends '(2)', '(3)', ... (p.70); '_N' keeps names free of brackets.
    """
    p = Path(path)
    if not p.exists():
        return p
    for k in range(1, 10 ** 6):
        q = p.with_name(f'{p.stem}_{k}{p.suffix}')
        if not q.exists():
            return q
    raise FileExistsError(f'no free name for {p}')
