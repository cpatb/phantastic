# Test Phantastic with the camera

The window is laid out like PCC: toolbar at the top, camera and cine windows in the middle,
and the **Live | Play | Manager** tabs on the right.

## Connect

1. Connect the camera to an Ethernet port of the PC. Turn on the camera.
2. Give that Ethernet port the address `100.100.100.1` with the mask `255.255.0.0`.
   - Easiest: run `C:\Program Files\Phantom\Utilities\PCCNetConfig.exe` (installed with PCC).
   - Or: Windows Settings > Network > Ethernet > Edit IP assignment > Manual, IPv4.
3. Close PCC. Two programs connected to one camera can interfere.
4. Double-click **Phantastic** on the Desktop.
5. In the **Manager** tab, click the magnifier (Discover). Double-click the camera under
   **Cameras**. Its live image opens.
   - If Discover finds nothing, click the screen icon (Connect by IP) and type the camera's
     address (PCC shows it; it is usually 100.100.x.x).
   - The network icon at the bottom of the Manager tab shows the PC's adapters and which one is
     on the camera subnet.

## Test

6. Click the wrench at the bottom of the **Manager** tab (Run camera test), then **Start**.
   This test only reads from the camera. If no cine holds a recording yet, the format checks
   are skipped.
7. To test recording too: tick **Record a test clip into cine**, choose a cine that holds
   nothing you need (recording erases it), then click **Start** again.
8. Use the app as you use PCC:
   - **Live** tab: Cine Settings (Resolution, Sample Rate, Exposure Time, Last), **Apply**,
     then **Capture** and **Trigger** (Ctrl+R, Ctrl+T).
   - **Play** tab: choose the camera cine, review it, set **[** and **]**, then **Save Cine...**
     (Ctrl+S).

## If something goes wrong

- Every command and answer is written to a log. Click the gear at the bottom of the Manager tab
  (or Help > Open log folder): `%LOCALAPPDATA%\Phantastic\logs`. Each camera test also writes
  `report.txt` there.
- Tell Claude what you did. Claude reads the newest log and report.
