"""Unit tests for the pure thermal-printer transport helpers (no app/PIL/device)."""
from vera.business.print_transport_core import is_raw_lp, paced_chunks, find_server_device


def test_is_raw_lp_matches_usblp_only():
    assert is_raw_lp("/dev/vera-printer")
    assert is_raw_lp("/dev/usb/lp0")
    assert is_raw_lp("/dev/usb/lp3")
    assert not is_raw_lp("/dev/ttyUSB0")
    assert not is_raw_lp("/dev/ttyACM0")
    assert not is_raw_lp("")


def test_paced_chunks_splits_and_flags_last():
    data = b"x" * 1300  # chunk 512 -> 512, 512, 276
    out = list(paced_chunks(data, 512))
    assert [len(c) for c, _ in out] == [512, 512, 276]
    assert [more for _, more in out] == [True, True, False]
    assert b"".join(c for c, _ in out) == data


def test_paced_chunks_exact_multiple_flags_last_false():
    out = list(paced_chunks(b"y" * 1024, 512))
    assert [more for _, more in out] == [True, False]


def test_paced_chunks_empty():
    assert list(paced_chunks(b"", 512)) == []


def test_find_server_device_prefers_first_existing(tmp_path):
    lp = tmp_path / "lp0"; lp.write_bytes(b"")
    missing = str(tmp_path / "nope")
    assert find_server_device([missing, str(lp)]) == str(lp)
    assert find_server_device([missing]) == ""
