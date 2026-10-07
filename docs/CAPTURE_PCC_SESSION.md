# Record what PCC sends to a camera

Use this procedure to record the traffic between PCC and a real Phantom camera. The record
shows every command that PCC sends, every answer, and the image data. Phantastic then turns
the record into a readable transcript.

This procedure uses `pktmon`, which is part of Windows 10 (version 2004 and later) and
Windows 11. You do not have to install other software.

## Before you start

- You must have administrator rights on the PC.
- Connect the camera, then start PCC once. Make sure that PCC finds the camera.
- Write down the IP address of the camera (for example, 100.100.1.7).
- Close PCC.

## Procedure

1. Open PowerShell as administrator.
2. Make a folder for the record:
   `mkdir C:\captures`
3. Remove old filters:
   `pktmon filter remove`
4. Add a filter for the camera address. Use the IP address of your camera:
   `pktmon filter add CAM -i 100.100.1.7`
5. Add a filter for the discovery messages:
   `pktmon filter add DISC -t UDP -p 7380`
6. If the camera has a 10 Gb connection, add a filter for its raw image frames:
   `pktmon filter add XIMG -d 0x88B7`
7. Start the record:
   `pktmon start --capture --pkt-size 0 --file-name C:\captures\pcc.etl`
8. Start PCC. Do the operations that you want to record, for example:
   - Connect to the camera.
   - Change the resolution, the frame rate and the exposure.
   - Record into a partition that has no data that you must keep, and then trigger.
   - Save a short range of the cine to a file (16-bit cine, and also 8-bit TIFF).
9. Close PCC.
10. Stop the record:
    `pktmon stop`
11. Convert the record to a pcapng file:
    `pktmon etl2pcap C:\captures\pcc.etl --out C:\captures\pcc.pcapng`
12. Make the transcript:
    `python tools\parse_capture.py C:\captures\pcc.pcapng C:\captures\pcc_out`

If a `pktmon` command gives an error, type `pktmon filter add help` or `pktmon start help`.
The option names can be different in some Windows versions.

## Result

The folder `C:\captures\pcc_out` contains:

- `transcript.txt`: each command from PCC and the answer from the camera, with the time.
- `discovery.txt`: the discovery messages.
- `summary.json`: the connections, the image requests and the data sizes.
- `img_0000.npz`, `img_0001.npz`, ...: the image data of each request, as the camera sent it.

Compare the `img_*.npz` data with the file that PCC saved. Use
`python tools\compare_cines.py` for cine files. This shows exactly what PCC changes between
the camera and the file.

## Caution

- The record contains all the image data that PCC downloads. It can be very large. Record
  only a short range.
- Do not record into a partition that contains data that you must keep. PCC deletes the
  recordings in all partitions when it starts a new recording.
- 10 Gb raw frames (EtherType 0x88B7) are recorded but `parse_capture.py` does not decode
  them yet.
