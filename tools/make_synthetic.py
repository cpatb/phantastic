"""Write synthetic cines covering every pixel code, for black-box checks against the vendor decoder.

Each file holds a known ramp so the vendor's decoded output reveals its exact decoding:
  ramp_mono16.cine    every 12-bit value 0..4095, once per pixel (64 x 64)
  ramp_packed10.cine  every 10-bit code 0..1023 (32 x 32)  -> vendor output = linearisation table
  ramp_packed12L.cine every 12-bit value 0..4095 (64 x 64) -> confirms 12L bit order
  ramp_mono8.cine     0..255 (16 x 16)
  ramp_bgr24.cine / ramp_bgr48.cine  distinct R, G, B ramps (channel order + bottom-up check)
BlackLevel = 0 and WhiteLevel = 2^bits - 1 are written so that the vendor's black/white
normalisation is (ideally) the identity; any remaining difference is decoding, not scaling.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phantastic.cine import CineWriter  # noqa: E402


def setup_fields(bits, w, h, color=False):
    return dict(FrameRate=1000, FrameRate16=1000, ShutterNs=500_000, Shutter=500, Shutter16=500,
                RealBPP=bits, BlackLevel=0, WhiteLevel=(1 << bits) - 1, ImWidth=w, ImHeight=h,
                fGain=1.0, fGamma=1.0, fSaturation=1.0, fGain16_8=1.0, fGainR=1.0, fGainG=1.0, fGainB=1.0,
                bEnableColor=1 if color else 0, CFA=0, cmUser=[1, 0, 0, 0, 1, 0, 0, 0, 1],
                cmCalib=[1, 0, 0, 0, 1, 0, 0, 0, 1], TonePoints=0, Description='Phantastic synthetic ramp')


def write(path, img_list, packing, bits):
    h, w = img_list[0].shape[:2]
    color = packing in ('bgr24', 'bgr48')
    with CineWriter(path, w, h, len(img_list), packing, first_image_no=0,
                    setup_fields=setup_fields(bits, w, h, color), trigger_time=(1_700_000_000, 0)) as cw:
        for i, img in enumerate(img_list):
            cw.append(img, time=(1_700_000_000, i * (2 ** 32 // 1000)), exposure=0)


def main(outdir):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    ramp12 = np.arange(4096, dtype=np.uint16).reshape(64, 64)
    ramp10 = np.arange(1024, dtype=np.uint16).reshape(32, 32)
    ramp8 = np.arange(256, dtype=np.uint8).reshape(16, 16)
    write(out / 'ramp_mono16.cine', [ramp12, ramp12[::-1].copy()], 'mono16', 12)
    write(out / 'ramp_packed10.cine', [ramp10, ramp10[::-1].copy()], 'packed10', 12)
    write(out / 'ramp_packed12L.cine', [ramp12, ramp12[::-1].copy()], 'packed12L', 12)
    write(out / 'ramp_mono8.cine', [ramp8], 'mono8', 8)
    yy, xx = np.mgrid[0:16, 0:32]
    rgb8 = np.stack([xx * 8, yy * 16, 255 - xx * 8], -1).astype(np.uint8)
    write(out / 'ramp_bgr24.cine', [rgb8], 'bgr24', 8)
    rgb16 = np.stack([xx * 128, yy * 256, 4095 - xx * 128], -1).astype(np.uint16)
    write(out / 'ramp_bgr48.cine', [rgb16], 'bgr48', 12)
    print('wrote synthetic cines to', out)


if __name__ == '__main__':
    main(sys.argv[1])
