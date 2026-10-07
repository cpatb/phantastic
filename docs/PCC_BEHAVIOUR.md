# What PCC and the Phantom SDK do to your data

Measured, not assumed. Each item says how it was established. "Vendor SDK" means the
`PhPy`/`PhCon`/`PhFile` libraries installed with PCC 3.11.11.806, driven through their public
Python binding as a black box (nothing was decompiled).

## Files

### Decimation ("save every N-th image")
PCC keeps the images whose number is a multiple of N, counting from the trigger (image 0), and
renumbers them n/N. The kept images are bit-identical to the source, with the source time stamps
and exposures.

*How:* every image of two PCC-decimated cines on the lab drive was matched to its source by
pixel MD5 and by time stamp: x1000 (`drop1_dec1000.cine`, 10/10 images inside the source range)
and x100 (`drop1_dec100.cine`, 25/25). Phantastic's `decimate_cine(..., align='trigger')`
reproduces PCC's x1000 file image for image (pixels, time stamps and exposures identical, 10/10).

PCC does not change `SETUP.FrameRate` in a decimated cine (it still says 200000 fps for the
x1000 file), so any reader that derives time from the frame rate is wrong by N. Use the per-image
time stamps (Phantastic and the ImageJ reader do).

### "Unprocessed" images from the SDK are not raw
With `NoProcessing = 1` the SDK still maps each value through approximately
`floor((raw - BlackLevel) * 4096 / (WhiteLevel - BlackLevel))`, clipped to [0, 4095].
On the lab files (Black 64, White 4064) every raw value below 64 becomes 0: 2671 of 1 310 720
pixels in the first 20 images of one recording. The map is one-to-one per value (no spatial
filter); the integer formula above matches 98% of levels exactly, all within 1 DN.

*How:* `tools/vendor_oracle.py` compared the SDK's output with the bytes in the file.

### TIFF export (8-bit and 16-bit)
PCC's TIFF export of a 12-bit cine is a per-value lookup table: every raw value maps to exactly
one output value (no spatial filter), with rows written top-down. The table follows the display
settings stored in the cine (on the lab files: Black 64, White 4064, a 6-point tone curve
"ExpIndex400000", and either fGain 1.021 / fGamma 1.148 (45 files) or fGain 1.0 / fGamma 1.172
(99 files)).

* **The 16-bit TIFF is processed too, not raw.** It is the same curve at 12-bit precision, shifted
  left by 4: raw 1000 is exported as 47920 = 2995 x 16. Anything measured on a PCC 16-bit TIFF
  went through gain, gamma and the tone curve.
* **"No processing" does not apply to export:** the SDK's NoProcessing switch gives byte-identical
  8-bit and 16-bit exports.
* **The curve is strong and not invertible in 8-bit:** raw 1000 of 4095 becomes 187/255 and raw
  2000 becomes 244/255; 935 raw levels in the calibration images map to 186 output levels.

*How:* a ramp cine holding every 12-bit value, carrying the SETUP of a real file, was exported by
the vendor SDK; the result is a 4096-entry table per settings group
(`phantastic/data/pcc_tables/`). Check: `phantastic.pcc_render.render(raw, table)` reproduces all 8
PCC 8-bit exports on the drive on 100% of pixels; the other group's table matches 5-9%.
A parametric model (normalise, gain, gamma, tone curve) does not reproduce the table (errors up
to 240 of 4095 levels), so some further stage is applied that the stored settings alone do not
explain.

The multi-page `*.tiff` files next to the recordings were written by the lab's own Python
pipeline (`Software = tifffile.py`), not by PCC.

### Stored display settings
SETUP carries gain, gamma, tone curve, white balance, colour matrices and an image filter
(`ImFilter`). They are display settings: the stored pixels are never altered by them. The
camera keeps a per-recording copy of the same settings (`c#.adj.*`: gain, offset, gamma,
tone, matrix, filter, ...), and the SDK reads all of them before every download.

## Camera protocol (captured)

`tools/capture_client.py` runs Phantastic's simulated camera and logs every command a client
sends. The vendor SDK discovered it, accepted it as a camera (serial, resolution, frame rate,
exposure and partition count read back correctly), recorded, triggered and downloaded from it.
Full captures: `docs/captures/`.

What the vendor SDK sends:

* **On connect:** `get info.features`, then `attach {port: N}` (the host opens the data
  connection to the camera's TCP 7116), then a sweep of about 60 `get` requests (info, cam,
  auto, video, mag, hw, eth, irig, preset).
* **It sets the camera clock and time zone on every connect:** `setrtc <PC unix time>` and
  `set cam.timezone:18000` (UTC-5 here). The camera's time stamps therefore follow the PC clock
  of whichever machine connected last.
* **Record deletes every partition:** `del {cine: 1}` ... `del {cine: 4}` before
  `rec {cine: 1}`. Recordings in other partitions are erased, not only the target.
* **Download:** reads `c1.res`, `c1.in`, `c1.out`, all 25 `c1.adj.*` processing parameters
  and `c1.meta.*`, then `img {cine:1, start:<n>, cnt:<k>, fmt:P12L}` when the camera offers
  P12L, otherwise `fmt:P16`.
* **P16 is full-scale 16-bit:** the SDK divides P16 values by 16 to get 12-bit values (all
  pixels within 1 DN of value/16 against known simulator frames), consistent with the protocol
  spec's "range 0-65535". Phantastic stores P16 exactly as received (RealBPP 16).
  With a frame holding every 12-bit value v sent as 16·v, the SDK returns v·4095/4064 (1000 →
  1007, 4064 → 4095): the divide by 16 followed by a white-level stretch.
* **P10 decode agrees with Phantastic's.** A frame holding every 12-bit value, sent as P10 codes
  (Phantastic's packing and inverse table), comes back from the SDK linear in the original value
  (correlation 0.99999; every pixel within 1 DN of (v − 64)·1024/4000). A different bit order or
  table would give scrambled values.
* **Which format the SDK asks for** depends on the camera's `info.imgformats`: P12L when the
  full list is offered, P10 when only P10/P12L are offered. Captures: `tools/vendor_format_probe.py`.
* Rows arrive top-down (the SDK's image matches the transmitted rows unflipped).

## Not yet established

* **P12L on the wire.** The SDK requests P12L but rejects the simulator's P12L stream: its
  output is a constant placeholder rectangle (values 2016/4064, 672 pixels), not decoded data.
  Excluded as causes: 3-byte MSB/LE layouts in either pixel order, 16/32/64-bit word swaps, and
  a numeric format code of 268 (0x100+12, by analogy with P10 266 and P16 272). Something else
  about P12L transfers (size, header, or code) is not known. Phantastic's P12L decode follows
  every open-source client (MSB-first) and is unverified; use P16 (verified, lossless) until a
  real camera settles it (hardware checklist step 7).
* P10/P12L packed *files* written by Phantastic: the vendor SDK refuses them, while it reads a
  real PCC P10 file and Phantastic's 8/16/24/48-bit files identically. Phantastic writes 16-bit
  files by default.
* Anything against real camera hardware. The protocol layer is checked against the published
  spec, real Miro M310 transcripts and the vendor SDK (through the simulator); the first session
  with a camera should run `tests/hardware_checklist.md`.
