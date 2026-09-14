"""Offline regression check: python3 scripts/test_hevc_policy.py."""

import json
import runpy
import sqlite3
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    root = Path(__file__).resolve().parent.parent
    regen = runpy.run_path(str(root / "scripts/regen-ops.py"))
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE quality_profiles (name, description, upgrades_allowed,
            minimum_custom_format_score, upgrade_until_score, upgrade_score_increment);
        CREATE TABLE quality_profile_custom_formats
            (quality_profile_name, custom_format_name, arr_type, score);
        CREATE TABLE radarr_quality_definitions
            (name, quality_name, min_size, max_size, preferred_size);
        CREATE TABLE sonarr_quality_definitions
            (name, quality_name, min_size, max_size, preferred_size);
    """)
    # Exercise rendering from old exports, including unrelated rejection scores.
    for arr in ("radarr", "sonarr"):
        buckets = defaultdict(list)
        regen["emit_profile_rows"]({
            "_pcd_name": arr, "_arr": arr, "items": [],
            "formatItems": [{"name": n, "score": s} for n, s in
                            (("x264", 0), ("x265", -10000), ("LQ", -10000))],
        }, buckets)
        db.executescript("\n".join(buckets["quality_profile_custom_formats"]))
        scores = dict(db.execute(
            "SELECT custom_format_name, score FROM quality_profile_custom_formats "
            "WHERE arr_type = ?", (arr,)))
        assert scores == {"x264": 1000, "x265": 3000, "LQ": -10000}
        assert scores["x265"] > scores["x264"] + 1800 + 100 + 7
        assert scores["x265"] + 4000 + 1800 + 150 + 7 + scores["LQ"] < 0
    db.execute("DELETE FROM quality_profile_custom_formats")

    # Re-exporting the new settings must not shrink them again; remux is excluded.
    with TemporaryDirectory() as tmp:
        regen["emit_quality_definitions_rows"].__globals__["EXPORTS"] = Path(tmp)
        for minimum, preferred in ((12.5, 40), (9, 28), (6, 28)):
            path = Path(tmp) / "radarr/qualitydefinition.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps([
                {"quality": {"name": "WEBDL-1080p"}, "minSize": minimum,
                 "preferredSize": preferred, "maxSize": 70},
                {"quality": {"name": "Remux-1080p"}, "minSize": 100,
                 "preferredSize": 110, "maxSize": 120},
            ]))
            buckets = defaultdict(list)
            regen["emit_quality_definitions_rows"](buckets)
            db.executescript("\n".join(buckets["radarr_quality_definitions"]))
            assert db.execute("SELECT * FROM radarr_quality_definitions").fetchall() == [
                ("Local", "WEBDL-1080p", 6, 70, 28),
                ("Local", "Remux-1080p", 100, 120, 110),
            ]
            db.execute("DELETE FROM radarr_quality_definitions")

    # Check the shipped configuration, including every profile and size definition.
    for line in (root / "ops/2.profiles.sql").read_text().splitlines():
        if any(line.startswith(f"INSERT INTO {table} (") for table in (
            "quality_profiles", "quality_profile_custom_formats",
            "radarr_quality_definitions", "sonarr_quality_definitions",
        )):
            db.execute(line)
    profiles = db.execute("SELECT name, upgrades_allowed FROM quality_profiles").fetchall()
    assert len(profiles) == 8
    for name, upgrades in profiles:
        assert upgrades == 0
        assert dict(db.execute(
            "SELECT custom_format_name, score FROM quality_profile_custom_formats "
            "WHERE quality_profile_name = ? AND custom_format_name IN ('x264', 'x265')",
            (name,))) == {"x264": 1000, "x265": 3000}
    for arr in ("radarr", "sonarr"):
        sizes = db.execute(f"SELECT quality_name, min_size, max_size, preferred_size "
                           f"FROM {arr}_quality_definitions").fetchall()
        assert len(regen["HEVC_SIZES"][arr]) == 12
        for quality, minimum, maximum, preferred in sizes:
            assert 0 <= minimum <= preferred <= maximum
            if quality in regen["HEVC_SIZES"][arr]:
                assert (minimum, preferred) == regen["HEVC_SIZES"][arr][quality]

    condition = ("INSERT INTO custom_format_conditions (custom_format_name, name, type, "
                 "arr_type, negate, required) VALUES "
                 "('x264', 'x|h264', 'release_title', 'radarr', 0, 1);\n")
    expected = condition.replace("'radarr'", "'all'")
    assert regen["vendor_upstream"](condition) == expected
    assert regen["vendor_upstream"](expected) == expected
    assert expected in (root / "ops/1.initial.sql").read_text()
    print("HEVC policy checks passed")


if __name__ == "__main__":
    main()
