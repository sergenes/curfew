from __future__ import annotations

from pathlib import Path

from curfew.vendor import VendorDb, is_randomized, normalize_mac


def test_normalize_mac() -> None:
    assert normalize_mac("98-da-c4-00-00-01") == "98:DA:C4:00:00:01"
    assert normalize_mac("98dac4000001") == "98:DA:C4:00:00:01"
    assert normalize_mac("not a mac") == "NOT A MAC"


def test_is_randomized() -> None:
    assert is_randomized("4A:AB:D9:00:00:05")  # 0x4A -> bit 1 set
    assert is_randomized("EA:82:62:00:00:03")
    assert not is_randomized("98:DA:C4:00:00:01")
    assert not is_randomized("F0:03:8C:00:00:04")
    assert not is_randomized("")


def test_lookup_without_table_is_empty_but_flags_randomized(tmp_path: Path) -> None:
    db = VendorDb(tmp_path)
    assert not db.available
    assert db.lookup("98:DA:C4:00:00:01") == ""
    assert db.lookup("4A:AB:D9:00:00:05") == "(randomized MAC)"


def test_lookup_from_csv(tmp_path: Path) -> None:
    (tmp_path / "oui.csv").write_text(
        "Registry,Assignment,Organization Name,Organization Address\n"
        'MA-L,98DAC4,"TP-Link Corporation Limited",Shenzhen CN\n'
        "MA-L,F0038C,iRobot Corporation,Bedford US\n"
    )
    db = VendorDb(tmp_path)
    assert db.available
    assert db.lookup("98:da:c4:b3:03:1c") == "TP-Link Corporation Limited"
    assert db.lookup("F0:03:8C:00:00:04") == "iRobot Corporation"
    assert db.lookup("00:11:22:33:44:55") == ""
