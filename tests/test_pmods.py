# SPDX-License-Identifier: Apache-2.0
from ttsearch.pmods import PMODS, PMOD_BY_ID, detect_pmods, has, tiny_vga


def pins(**kw):
    """Build a pinout dict from keyword args like uo0='R1', uio3='SCK'."""
    out = {}
    for k, v in kw.items():
        prefix = k.rstrip("0123456789")
        out[f"{prefix}[{k[len(prefix):]}]"] = v
    return out


def test_has_matches_whole_tokens_only():
    p = pins(uio1="uart_tx (debug)")
    assert has(p, "uio[1]", "tx")
    assert not has(p, "uio[1]", "t")
    assert not has(p, "uio[1]", "rx")


def test_tiny_vga_matches_official_pinout_and_variants():
    assert tiny_vga(pins(uo0="R1", uo1="G1", uo2="B1", uo3="VSync", uo4="R0", uo5="G0", uo6="B0", uo7="HSync"))
    assert tiny_vga(pins(uo0="r[1]", uo1="g[1]", uo2="b[1]", uo3="v_sync", uo4="r[0]", uo5="g[0]", uo6="b[0]", uo7="h_sync"))
    # Swapped sync pins do not match.
    assert not tiny_vga(pins(uo0="R1", uo1="G1", uo2="B1", uo3="hsync", uo4="R0", uo5="G0", uo6="B0", uo7="vsync"))
    assert not tiny_vga({})


def test_detect_pmods_ids():
    p = pins(uio0="CS", uio1="MOSI", uio2="MISO", uio3="SCK", uio4="SD2", uio5="SD3", uio6="CS1 (RAM)", uio7="CS2")
    assert detect_pmods(p) == ["qspi-pmod", "spi"]
    assert detect_pmods(pins(uio4="cs_n", uio5="copi", uio6="cipo", uio7="sclk")) == ["spi"]
    assert detect_pmods(pins(ui4="latch", ui5="clock", ui6="data")) == ["gamepad"]
    assert detect_pmods(pins(uio5="TXD", uio6="RXD")) == ["uart-pmod"]
    assert detect_pmods(pins(uio2="SCL", uio3="SDA")) == ["i2c-pmod"]
    assert detect_pmods(pins(ui3="uart rx", uo4="uart tx")) == ["uart-usb"]
    assert detect_pmods(pins(uo7="audio out")) == ["tt-audio"]
    assert detect_pmods(pins(uo7="segment g")) == []
    assert detect_pmods(None) == []


def test_pmod_table_is_consistent():
    ids = [p.id for p in PMODS]
    assert len(ids) == len(set(ids))
    assert all(PMOD_BY_ID[i].url.startswith("https://") for i in ids)
