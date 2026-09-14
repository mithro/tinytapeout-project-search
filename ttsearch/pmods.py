# SPDX-License-Identifier: Apache-2.0
"""Infer which PMODs a project can plug into, from its declared pinout.

Nothing in a project's info.yaml declares PMOD compatibility. What exists is
the set of recommended pinouts on https://tinytapeout.com/specs/pinouts/ and
the website's own check for one of them (functions/utils/tinyVGA.ts in the
tinytapeout_www repository, which this file mirrors for Tiny VGA). Each
detector below compares the project's pin names, position by position, with
one recommended pinout. A match means the pin names line up, not that anyone
has tested the project with that board, so the UI labels these as inferred.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

Pinout = dict[str, str]

SPEC_URL = "https://tinytapeout.com/specs/pinouts/"


def _name(pinout: Pinout, pin: str) -> str:
    return (pinout.get(pin) or "").lower()


def _tokens(name: str) -> set[str]:
    """Split a pin name into alphanumeric tokens: 'uart_tx (debug)' -> {uart, tx, debug}."""
    return set(re.findall(r"[a-z0-9]+", name))


def has(pinout: Pinout, pin: str, *alts: str) -> bool:
    """True if the pin's name contains one of alts as a whole token."""
    return bool(_tokens(_name(pinout, pin)) & set(alts))


def like(pinout: Pinout, pin: str, pattern: str) -> bool:
    """True if the pin's name matches the regex anywhere (loose, as the website does)."""
    return re.search(pattern, _name(pinout, pin)) is not None


# --------------------------------------------------------------------------
# Detectors, one per recommended pinout
# --------------------------------------------------------------------------

CS = ("cs", "ncs", "cs_n", "csn", "ce", "ss", "sel", "cs0", "flash")
MOSI = ("mosi", "sd0", "d0", "io0", "dq0", "si", "sdo", "copi")
MISO = ("miso", "sd1", "d1", "io1", "dq1", "so", "sdi", "cipo")
SCK = ("sck", "sclk", "clk", "spi_clk", "spiclk", "clock")
TX = ("tx", "txd", "tx_out", "uart_tx", "txo")
RX = ("rx", "rxd", "rx_in", "uart_rx", "rxi")
SCL = ("scl", "sck", "i2c_scl", "sclk")
SDA = ("sda", "i2c_sda")


def tiny_vga(p: Pinout) -> bool:
    """uo[0..7] = R1 G1 B1 vsync R0 G0 B0 hsync (mirrors tinyVGA.ts)."""
    if not like(p, "uo[3]", r"v_?sync") or not like(p, "uo[7]", r"h_?sync"):
        return False
    return all(like(p, f"uo[{i}]", c) for i, c in ((0, "r"), (1, "g"), (2, "b"), (4, "r"), (5, "g"), (6, "b")))


def tt_audio(p: Pinout) -> bool:
    """Audio on uo[7] or uio[7] (mono) or [6]/[7] (stereo)."""
    audio = ("audio", "sound", "speaker", "pdm", "pwm", "aud", "snd")
    return any(has(p, pin, *audio) for pin in ("uo[7]", "uio[7]"))


def spi_row(p: Pinout, base: int) -> bool:
    return (has(p, f"uio[{base}]", *CS) and has(p, f"uio[{base+1}]", *MOSI)
            and has(p, f"uio[{base+2}]", *MISO) and has(p, f"uio[{base+3}]", *SCK))


def spi(p: Pinout) -> bool:
    """CS MOSI MISO SCK on uio[0..3] (top row) or uio[4..7] (bottom row)."""
    return spi_row(p, 0) or spi_row(p, 4)


def qspi_pmod(p: Pinout) -> bool:
    """QSPI Flash/PSRAM PMOD: SPI on uio[0..3] plus SD2/SD3 or RAM chip selects."""
    if not spi_row(p, 0):
        return False
    return (has(p, "uio[4]", "sd2", "d2", "io2", "dq2") or has(p, "uio[5]", "sd3", "d3", "io3", "dq3")
            or has(p, "uio[6]", "cs1", "ram", "psram", "cs") or has(p, "uio[7]", "cs2", "ram", "psram", "cs"))


def gamepad(p: Pinout) -> bool:
    """ui[4] LATCH, ui[5] CLOCK, ui[6] DATA."""
    return (has(p, "ui[4]", "latch") and has(p, "ui[5]", "clk", "clock", "sclk")
            and has(p, "ui[6]", "data", "dat", "serial"))


def uart_pmod(p: Pinout) -> bool:
    """TXD on uio[1] and RXD on uio[2] (top row) or uio[5]/uio[6] (bottom row)."""
    return ((has(p, "uio[1]", *TX) and has(p, "uio[2]", *RX))
            or (has(p, "uio[5]", *TX) and has(p, "uio[6]", *RX)))


def i2c_pmod(p: Pinout) -> bool:
    """SCL on uio[2] and SDA on uio[3] (top row) or uio[6]/uio[7] (bottom row)."""
    return ((has(p, "uio[2]", *SCL) and has(p, "uio[3]", *SDA))
            or (has(p, "uio[6]", *SCL) and has(p, "uio[7]", *SDA)))


def uart_usb(p: Pinout) -> bool:
    """Demo board RP2040 UART bridge: ui[3] RX + uo[4] TX, or ui[1] RX + uo[0] TX."""
    return ((has(p, "ui[3]", *RX) and has(p, "uo[4]", *TX))
            or (has(p, "ui[1]", *RX) and has(p, "uo[0]", *TX)))


@dataclass(frozen=True)
class Pmod:
    id: str
    name: str
    pins: str                     # the recommended pinout, for the UI
    url: str
    detect: Callable[[Pinout], bool] = field(compare=False, repr=False)


PMODS: list[Pmod] = [
    Pmod("tiny-vga", "Tiny VGA", "uo[0..7] = R1 G1 B1 vsync R0 G0 B0 hsync",
         "https://github.com/mole99/tiny-vga", tiny_vga),
    Pmod("tt-audio", "TT Audio", "audio on uo[7] or uio[7]; stereo on [6] and [7]",
         "https://github.com/MichaelBell/tt-audio-pmod", tt_audio),
    Pmod("qspi-pmod", "QSPI Flash/PSRAM", "uio[0..7] = CS0 SD0 SD1 SCK SD2 SD3 CS1 CS2",
         "https://github.com/mole99/qspi-pmod", qspi_pmod),
    Pmod("gamepad", "Gamepad", "ui[4] latch, ui[5] clock, ui[6] data",
         "https://github.com/psychogenic/gamepad-pmod", gamepad),
    Pmod("spi", "SPI Pmod", "uio[0..3] or uio[4..7] = CS MOSI MISO SCK",
         SPEC_URL + "#spi", spi),
    Pmod("uart-pmod", "UART Pmod", "uio[1] TXD, uio[2] RXD (or uio[5], uio[6])",
         SPEC_URL + "#uart-optional-hardware-flow-control", uart_pmod),
    Pmod("i2c-pmod", "I2C Pmod", "uio[2] SCL, uio[3] SDA (or uio[6], uio[7])",
         SPEC_URL + "#i2c-optional-interrupt-and-reset", i2c_pmod),
    Pmod("uart-usb", "UART over demo board USB", "ui[3] RX + uo[4] TX, or ui[1] RX + uo[0] TX",
         SPEC_URL + "#uart-to-usb", uart_usb),
]

PMOD_BY_ID = {p.id: p for p in PMODS}


def detect_pmods(pinout: Pinout | None) -> list[str]:
    """Return the ids of every recommended pinout this project's pinout matches."""
    if not pinout:
        return []
    return [p.id for p in PMODS if p.detect(pinout)]
