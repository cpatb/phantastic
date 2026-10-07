"""12-byte time records: exposure and sub-microsecond time from exptime32 / frac32."""
import struct

from phantastic import protocol as P
from phantastic.camera import stamp_exposure64, stamp_time64

# One record read from a Phantom v2512 (s/n 21598, 1000 ns exposure setting, 200 000 fps), 2026-10-07
V2512_RECORD = struct.pack('>IHHHH', 16218, 0, 0x071F, 54445, 26775)


def test_v2512_exposure_equals_what_pcc_stores():
    s = P.decode_time_stamps(V2512_RECORD, 12, 1)[0]
    assert (s.exptime_us, s.exptime32, s.frac32) == (0, 54445, 26775)
    # PCC's cine of that camera at that setting holds 3568 in tag 1003 (830.74 ns): an anchor from a
    # different file and program than this record. floor(54445/65536 us * 2^32 / 1e6) = floor(3568.10)
    assert stamp_exposure64(s, 1000) == 3568
    # the whole-microsecond field alone says 0 us: the old rule stored 0
    old = P.TimeStamp(s.csecs, s.exptime_us, s.frac)          # no extension, as an 8-byte record
    assert stamp_exposure64(old, 1000) == 0


def test_sub_microsecond_time_is_added_in_65536ths():
    s = P.decode_time_stamps(V2512_RECORD, 12, 1)[0]
    sec, frac = stamp_time64(s, 946684800)
    usec = (16218 % 100) * 10000 + (0x071F >> 2)              # 180455 us into the second
    assert sec == 946684800 + 162
    assert frac == (usec * 65536 + 26775) * 2 ** 32 // (10 ** 6 * 65536)
    whole = P.TimeStamp(s.csecs, s.exptime_us, s.frac)
    sub_ns = (frac - stamp_time64(whole, 946684800)[1]) / 2 ** 32 * 1e9
    assert abs(sub_ns - 26775 / 65536 * 1000) < 0.5           # 408.6 ns, below one microsecond


def test_eight_byte_records_keep_the_setting_rule():
    s = P.TimeStamp(100, 90, 0)                                # 90 us stamp, setting 90 500 ns
    assert stamp_exposure64(s, 90500) == 90500 * 2 ** 32 // 10 ** 9
    assert stamp_exposure64(s, 250000) == 90 * 2 ** 32 // 10 ** 6   # disagreeing setting: the stamp wins
