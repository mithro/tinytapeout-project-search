# SPDX-License-Identifier: Apache-2.0
from ttsearch.fetch import merge_project, parse_info_md, parse_info_yaml, split_tags

OLD_YAML = """
project:
  wokwi_id: 0
  top_module: "adamgreig_tt02_adc_dac"
documentation:
  author: "Adam Greig"
  title: "Sigma-Delta ADC/DAC"
  description: "Simple ADC and DAC"
  how_it_works: |
    Pulse density modulation.
  how_to_test: |
    Clock in[0].
  external_hw: "Comparator"
  language: "Amaranth"
  tag: "adc, DAC ,serial"
  clock_hz: 6000
  inputs:
    - clock
    - reset
    - adc_in
    - none
  outputs:
    - adc_out
    - none
"""

NEW_YAML = """
project:
  title: "8bit ALU"
  author: "David Parent"
  description: "Building a simple ALU"
  language: "Verilog"
  clock_hz: 1000
  tiles: "1x1"
  top_module: "tt_um_8bitALU"
  source_files:
    - "ALU_test.v"
pinout:
  ui[0]: "ui_in"
yaml_version: 6
"""

INFO_MD = """<!---
Template comment to be removed.
-->

## How it works

Counts VGA pixels.

## How to test

Plug in a monitor.

## External hardware

VGA PMOD
"""


def test_parse_old_yaml():
    r = parse_info_yaml(OLD_YAML)
    assert r["language"] == "Amaranth"
    assert r["tags"] == ["adc", "dac", "serial"]
    assert r["how_it_works"].strip() == "Pulse density modulation."
    assert r["how_to_test"].strip() == "Clock in[0]."
    assert r["external_hw"] == "Comparator"
    assert r["title"] == "Sigma-Delta ADC/DAC"
    assert r["top_module"] == "adamgreig_tt02_adc_dac"
    assert r["pinout_from_yaml"] == {
        "ui[0]": "clock", "ui[1]": "reset", "ui[2]": "adc_in", "uo[0]": "adc_out",
    }


def test_parse_new_yaml():
    r = parse_info_yaml(NEW_YAML)
    assert r["yaml_version"] == 6
    assert r["language"] == "Verilog"
    assert r["tags"] == []
    assert r["top_module"] == "tt_um_8bitALU"
    assert r["source_files"] == ["ALU_test.v"]
    assert "how_it_works" not in r
    assert "pinout_from_yaml" not in r


def test_parse_bad_yaml():
    assert "yaml_error" in parse_info_yaml("project: [unclosed")


def test_parse_info_md():
    r = parse_info_md(INFO_MD)
    assert r["how_it_works"] == "Counts VGA pixels."
    assert r["how_to_test"] == "Plug in a monitor."
    assert r["external_hw"] == "VGA PMOD"
    assert "Template comment" not in r["docs_md"]
    assert r["docs_md"].startswith("## How it works")


def test_split_tags():
    assert split_tags(None) == []
    assert split_tags("") == []
    assert split_tags(["B", "a"]) == ["a", "b"]


def test_merge_prefers_index_and_falls_back_to_yaml_pins():
    entry = {
        "macro": "m", "address": 1, "title": "Index title", "author": "A",
        "description": "", "pinout": {"ui[0]": "", "uo[0]": ""}, "extra_field": 7,
    }
    docs = {
        "docs_source": ["info.yaml"], "title": "Yaml title", "description": "Yaml desc",
        "pinout_from_yaml": {"ui[0]": "clk"}, "how_it_works": "x",
    }
    rec = merge_project("tt02", entry, docs)
    assert rec["shuttle"] == "tt02"
    assert rec["title"] == "Index title"          # index wins
    assert rec["description"] == "Yaml desc"      # empty index value falls back
    assert rec["pinout"] == {"ui[0]": "clk"}      # empty index pinout falls back
    assert rec["how_it_works"] == "x"
    assert rec["extra_field"] == 7                # unknown index fields preserved
    assert "pinout_from_yaml" not in rec
