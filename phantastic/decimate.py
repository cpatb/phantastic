"""Frame selection, lossless decimation and transparent export of cine files.

Selection rule (explicit, never "whatever the software picks"):
  align='trigger'  keep image numbers that are multiples of ``step`` (trigger frame = 0 is on the
                   grid). This is exactly what PCC does: on the test drive, every frame of a
                   PCC x1000 and x100 decimated cine is the source frame numbered k*N,
                   pixel-identical, with the same time stamp (10/10 and 25/25 frames).
  align='first'    keep the first image of the range and every ``step``-th after it.

Decimated cines are written by copying the stored pixel bytes, time stamps and per-image
exposures (tag 1003, seconds * 2^32; on the lab's files 830.7 ns against SETUP.ShutterNs = 1000 ns,
so it is the camera's measured value, not a copy of the setting) verbatim.
TIFF export writes the raw sensor values (16-bit), top-down, with no LUT, gain, gamma or filter.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np

from .cine import TAG_EXPOSURE_ONLY, TAG_TIME_ONLY, TIME64_SCALE, CineReader, CineWriter


def select_numbers(lo: int, hi: int, step: int = 1, align: str = 'trigger') -> np.ndarray:
    """Image numbers in [lo, hi] kept by the rule (see module docstring)."""
    if step < 1:
        raise ValueError('step must be >= 1')
    if hi < lo:
        return np.zeros(0, np.int64)
    if align == 'trigger':
        start = math.ceil(lo / step) * step
    elif align == 'first':
        start = lo
    else:
        raise ValueError(f"align must be 'trigger' or 'first', not {align!r}")
    return np.arange(start, hi + 1, step, dtype=np.int64)


def last_for_count(first: int, count: int, step: int = 1, align: str = 'trigger') -> int:
    """Last image number such that [first, last] keeps exactly ``count`` images under the rule.

    The first kept image is ``first`` itself (align='first') or the first multiple of ``step`` at or
    after it (align='trigger'); the range then ends at the count-th kept image. The caller clips
    to what the recording holds, which can leave fewer than ``count``.
    """
    if count < 1:
        raise ValueError('count must be >= 1')
    start = int(select_numbers(first, first + step - 1, step, align)[0])
    return start + (count - 1) * step


def renumbering(numbers: np.ndarray, step: int) -> tuple[int, int]:
    """(first output number, offset) such that source image n becomes (n - offset) // step.

    A cine numbers its images consecutively from FirstImageNo, so a decimated file cannot keep the
    source numbers. With align='trigger' the offset is 0 and source n becomes n/step, which is PCC's
    numbering (the trigger stays image 0). With align='first' the offset is first mod step.
    """
    start = int(numbers[0])
    offset = start % step
    return (start - offset) // step, offset


DESCRIPTION_BYTES = 4095      # SETUP.Description is char[4096]
_MAPPING_PHRASE = re.compile(r'image k = (?:camera|source) image k\*\d+\+\d+', re.IGNORECASE)


def append_note(desc: str, note: str) -> str:
    """Append ``note`` to a Description without ever losing a mapping phrase.

    Mapping phrases are read oldest-first to compose repeated decimations, so they are kept in
    order; when the field would overflow, only the free text of the old description is trimmed.
    """
    out = (desc + '\n' + note).strip()
    if len(out.encode('latin-1', 'replace')) <= DESCRIPTION_BYTES:
        return out
    kept = ' '.join(m.group(0) + ';' for m in _MAPPING_PHRASE.finditer(desc))
    room = DESCRIPTION_BYTES - len(note) - len(kept) - 12
    if room < 0:
        raise ValueError('description cannot hold the decimation history; shorten the source description')
    return (desc[:room] + ' [...] ' + kept + '\n' + note).strip()


def _indices(r: CineReader, first, last, step, align) -> np.ndarray:
    lo = r.first if first is None else max(first, r.first)
    hi = r.first + len(r) - 1 if last is None else min(last, r.first + len(r) - 1)
    return select_numbers(lo, hi, step, align) - r.first


def decimate_cine(src, dst, step: int, align: str = 'trigger', first: int | None = None,
                  last: int | None = None, progress=None, crop=None) -> dict:
    """Write a new cine holding the selected images, bit-for-bit (pixels, time stamps, exposures).

    Output image k is source image k*step + offset (see :func:`renumbering`); with the default
    align='trigger' this is PCC's numbering and the trigger frame stays image 0. True times are
    kept in the per-image time stamps; SETUP.FrameRate keeps the recording rate, as PCC does.

    ``crop`` = (x, y, w, h) (top-down, see :mod:`phantastic.crop`): only that rectangle is written,
    values unchanged (re-packed in the source's packing; P10 codes are copied as codes). The file's
    ImWidth/ImHeight become w/h and the Description records the crop; ImPosXAcq/ImPosYAcq are left
    alone because their meaning is not established. Written to ``dst + '.part'`` and renamed.
    """
    from .crop import check_crop, crop_image, crop_note
    from .export import atomic_path, check_not_source
    check_not_source(src, dst)
    with CineReader(src) as r:
        idx = _indices(r, first, last, step, align)
        if len(idx) == 0:
            raise ValueError('no images selected')
        crop = check_crop(crop, r.width, r.height, r.packing)
        if crop and r.setup.get('CFA', 0) and not r.is_color and any(v % 2 for v in crop):
            # Raw colour-sensor data: an odd offset shifts the colour-filter phase that SETUP.CFA describes.
            # Whether CFA counts from the top row or the first STORED row (the bottom, for 16-bit files) is not
            # established, so x, y, w and h must all be even: then both edges keep their phase.
            raise ValueError(f'this is raw colour-sensor data (SETUP.CFA = {r.setup["CFA"]}): crop x, y, w and h '
                             'must all be even, or the colour pattern would no longer match the file\'s CFA field')
        n_src = len(r)
        times = r.image_times_raw() if r.has_complete_times() else None
        synthesized = times is None
        if synthesized:
            # No (complete) source stamps: write them from number / recording rate, so the
            # decimated file (renumbered, same FrameRate) still carries the true times.
            tsec, tfrac = r.header['TriggerTime']
            t64 = (np.int64(tsec) << 32) + tfrac + np.rint(r.image_numbers / r.frame_rate * TIME64_SCALE).astype(np.int64)
            times = np.stack([t64 & 0xFFFFFFFF, t64 >> 32], axis=1)
        exps = r.exposures_raw()
        if exps is not None and len(exps) != n_src:
            exps = None
        # Every other tagged block that holds one fixed-size record per image (e.g. 1007 time
        # code) is sliced and kept; blocks that are not per-image cannot be decimated and are
        # listed in the description as dropped.
        extra, dropped = {}, []
        for tag, (off, size) in r.blocks.items():
            if tag in (TAG_TIME_ONLY, TAG_EXPOSURE_ONLY):
                continue
            if size and size % n_src == 0:
                rec = size // n_src
                body = np.frombuffer(r.block_bytes(tag), np.uint8).reshape(n_src, rec)
                extra[tag] = body[idx].tobytes()
            else:
                dropped.append(tag)
        first_no = int(r.first + idx[0])
        out_first, offset = renumbering(r.first + idx, step)
        desc = r.setup.get('Description', '')
        note = (f'Phantastic: decimated x{step} (align={align}) from {Path(src).name} images '
                f'{first_no}..{int(r.first + idx[-1])}; image k = source image k*{step}+{offset}; '
                f'pixels copied verbatim; time stamps '
                f'{"SYNTHESIZED from number/frame rate (source had none)" if synthesized else "copied"}'
                f'{"; dropped non-per-image blocks " + str(dropped) if dropped else ""}.')
        fields = {}
        if crop:
            note += ' ' + crop_note(crop, r.width, r.height)
            fields.update(ImWidth=crop[2], ImHeight=crop[3])
        fields['Description'] = append_note(desc, note)
        w_out, h_out = (crop[2], crop[3]) if crop else (r.width, r.height)
        with atomic_path(dst) as part:
            w = CineWriter(part, w_out, h_out, len(idx), r.packing, first_image_no=out_first,
                           setup=r.setup_raw, setup_fields=fields,
                           trigger_time=r.header['TriggerTime'], compression=r.header['Compression'],
                           first_movie_image=r.header['FirstMovieImage'], total_image_count=r.header['TotalImageCount'],
                           with_times=True, with_exposures=exps is not None,
                           clr_important=r.bitmap['biClrImportant'],
                           pels_per_meter=(r.bitmap['biXPelsPerMeter'], r.bitmap['biYPelsPerMeter']),
                           stride=None if crop else r.stride, extra_blocks=extra)
            with w:
                for k, i in enumerate(idx):
                    t = (int(times[i, 1]), int(times[i, 0]))
                    e = None if exps is None else int(exps[i])
                    if crop is None:
                        w.append_stored(r.stored_bytes(int(i)), time=t, exposure=e)
                    else:   # decode, cut, re-pack: CineWriter takes P10 as codes, everything else as values
                        img = r.read_codes10(int(i)) if r.packing == 'packed10' else r.read(int(i))
                        w.append(crop_image(img, crop), time=t, exposure=e)
                    if progress:
                        progress(k + 1, len(idx))
    return dict(src=str(src), dst=str(dst), count=len(idx), first=first_no, step=step, align=align,
                first_out=out_first, offset=offset, crop=crop)


def load_pcc_table(src, pcc_table):
    """(table array, its path) for ``pcc_table`` = 'auto' (the shipped table matching ``src``) or a path."""
    from .pcc_render import find_table, load_table
    if pcc_table == 'auto':
        found = find_table(src)
        if found is None:
            raise ValueError('no measured PCC table matches this file\'s display settings '
                             '(derive one with tools/make_lut_probe.py + tools/vendor_export_probe.py)')
        pcc_table = found['csv']
    return load_table(pcc_table), str(pcc_table)


def export_tiff(src, dst, first: int | None = None, last: int | None = None, step: int = 1,
                align: str = 'trigger', progress=None, pcc_table=None, crop=None) -> dict:
    """Export selected images as a multi-page TIFF of raw sensor values (no processing).

    With ``pcc_table`` (a measured PCC export table, see :mod:`phantastic.pcc_render`) the pages
    are instead what PCC's TIFF export would write for the same settings, bit for bit; the
    metadata says so. ``crop`` = (x, y, w, h), top-down (see :mod:`phantastic.crop`).

    Mono: uint8 or uint16 pages exactly as decoded (P10 -> 12-bit linear). Colour: RGB pages.
    Per-frame image numbers and times go into a JSON ImageDescription on page 0 and a JSON
    sidecar (``<dst>.json``, including the frame interval). ImageJ's own frame-interval tag is not
    written, so Fiji shows the interval only via the sidecar. Both are written under temporary
    names and renamed on success (:func:`phantastic.export.write_tiff_stack`).
    """
    from .export import FileFrames, check_not_source, write_tiff_stack
    check_not_source(src, dst)
    check_not_source(src, str(dst) + '.json')
    table, name = load_pcc_table(src, pcc_table) if pcc_table is not None else (None, None)
    with FileFrames(src, first, last, step, align) as fr:
        return write_tiff_stack(fr, dst, crop=crop, table=table, table_name=name, progress=progress)


__all__ = ['select_numbers', 'decimate_cine', 'export_tiff', 'TAG_EXPOSURE_ONLY']
