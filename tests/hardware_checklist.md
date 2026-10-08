# First session with a real camera

Nothing in Phantastic has run against camera hardware yet. Do these in order, and stop at the
first one that fails. Each step says what result proves it works.

Before you start: if you can, record the session with `docs/CAPTURE_PCC_SESSION.md`. Do one
PCC session (connect, record, save a short range) while the record runs, so the real PCC commands
are on file. Then connect the camera with a direct cable. Give the PC an address in the camera's
subnet (normally 100.100.x.x, mask 255.255.0.0). Do not record anything you need to keep until
step 6 passes. The `rec` command deletes the recording in the cine it records into.

1. **Discovery.** Run `phantastic discover`. The camera must be listed with its serial number.
   If it is not listed, run `phantastic info --ip <camera ip>`. If that works, discovery is
   blocked (firewall or broadcast route), not the protocol.
2. **Read-only queries.** Run `phantastic info --ip <ip>`. Compare model, serial, resolution, rate and
   exposure with what PCC shows for the same camera.
3. **Transcript capture.** Run `phantastic info --ip <ip> --transcript info.txt` and keep the file.
   Every response must parse. A parse error is a bug: report it with the file.
4. **Live image.** Run `phantastic live --ip <ip> --out live.tif --format P16`. Look at the
   image. Check: is it upright, and do the values look MSB-aligned (multiples of 16)? Repeat
   with `--format 8`.
5. **Record and trigger.** Use a scratch partition. Run `phantastic record --ip <ip> --cine 1`,
   then `phantastic trigger --ip <ip>`. `phantastic info` must show `STR` for cine 1.
6. **Download compared with PCC.** Download a short range with
   `phantastic download --ip <ip> --cine 1 --first -10 --last 10 --format P16 --out p.cine`.
   Then save the same range from PCC as a cine. Run
   `python tools/compare_cines.py p.cine pcc.cine`. Expected result: Phantastic values = PCC
   values x 16, or equal. The script reports which, plus the time stamps and the image numbers.
   Then download again with `--as-12bit`. If it succeeds, the file must equal PCC's file exactly.
7. **Packed formats.** Repeat step 6 with `--format P10` and `--format P12L`. This settles the
   P12L bit order: P12L values must equal PCC's 12-bit values exactly.
8. **Uncorrected formats.** Repeat step 6 with `--format P16R`. The difference from P16 is the
   camera's FPN/PRNU correction. Keep both files.
9. **Decimation.** Download with `--step 10`. Every image must equal the corresponding
   image of step 6's full download, and the time stamps must match.

## Camera settings (these steps change the camera state)

Do steps 10 to 19 in the desktop app, with a scratch partition. Keep the session log
(Help > Open log folder). The log records each `set` command and the value that the camera
reads back. Before step 10, write down the PCC values of each setting that you change.

10. **No write on connect.** Connect the camera. Open the session log. Make sure that it
    contains only `get`, `attach`, `img`, `time` and `cstats` commands.
11. **Read-back of each selector.** Open Camera Signals, Advanced Settings, Auto Exposure and
    Image-Based Auto-Trigger. Compare each value with the value that PCC shows.
12. **Leaf set.** In Camera Signals, change "Trigger filter". Click "Apply". The camera must
    accept `set cam.trigfilt:<value>`. The field must show the read-back value. Set the PCC value again.
13. **Value meanings.** Change one setting in PCC, for example the trigger edge. Disconnect
    Phantastic and connect again. Record which number agrees with which PCC selection.
14. **Struct set.** In Advanced Settings, change "Burst count" to 2. Click "Apply". The camera must
    accept `set defc {bcount:2}` and keep its resolution and rate. Set "Burst count" to 0 again.
15. **Text set.** In Cine Settings, write a "Name" and a "Description" with spaces. Click "Apply".
    Record a cine and save it. PCC must show the name and the description in Cine Info.
16. **Auto-trigger area.** Click "Draw on live image" and make a rectangle. Click "Apply". Look at
    the area in PCC. Record if PCC shows the same rectangle. If not, the x and y convention is wrong.
17. **CSR.** Cover the lens. Click "CSR". Record the progress values in the session log, and the
    time until the camera reports the end of the CSR.
18. **Set Time.** Click "Set Time..", then "Set". The camera clock must agree with the computer
    clock to 2 s. Record if `irig.sec` is UTC or local time.
19. **Flagged pixels in the live image.** Put the cursor on a pixel that the camera flags. The
    status bar must show "flagged by camera" and the raw value.

Record the results in `docs/HARDWARE_RESULTS.md` (camera model, firmware `info.swver`/`info.fver`,
date, pass/fail per step).
