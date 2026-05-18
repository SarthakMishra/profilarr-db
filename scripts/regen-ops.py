#!/usr/bin/env python3
"""Regenerate ops/ from upstream trash-pcd + exports/{radarr,sonarr}/qualityprofile.json.

Strategy: we don't fork trash-pcd (PCD-to-PCD deps don't exist in Profilarr 2.0,
per Dictionarry-Hub/schema/docs/structure.md). Instead we vendor the upstream
CFs/regex/tags into ops/1.initial.sql and emit our own profile rows into
ops/2.profiles.sql. Re-run this script to pick up upstream CF updates.

Inputs:
  - https://raw.githubusercontent.com/Dictionarry-Hub/trash-pcd/main/ops/1.initial.sql
  - exports/radarr/qualityprofile.json
  - exports/sonarr/qualityprofile.json

Outputs:
  - ops/1.initial.sql  (vendored: TAGS, REGEX, CFs, conditions, naming, qdefs)
  - ops/2.profiles.sql (your profiles + groups + scores + languages)

Usage:
  python3 scripts/regen-ops.py            # uses cached upstream if present
  python3 scripts/regen-ops.py --refresh  # force re-download upstream
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORTS = REPO_ROOT / "exports"
OPS = REPO_ROOT / "ops"
CACHE = REPO_ROOT / "scripts" / ".cache"

TRASH_PCD_URL = (
    "https://raw.githubusercontent.com/Dictionarry-Hub/trash-pcd/main/ops/1.initial.sql"
)

# Upstream sections we replace with our own data.
DROP_SECTIONS = {
    "QUALITY PROFILES",
    "QUALITY GROUPS",
    "QUALITY PROFILE TAGS",
    "QUALITY GROUP MEMBERS",
    "QUALITY PROFILE QUALITIES",
    "QUALITY PROFILE CUSTOM FORMATS",
}

# Local CF names that don't match upstream canonical naming.
# Extend this if more drift is discovered.
CF_NAME_REMAP = {
    "WiTH AD": "WiTH.AD",
}

# Arr API quality names -> PCD canonical names. Sonarr renames the remux
# qualities; see schema/ops/2.qualities.sql quality_api_mappings.
QUALITY_NAME_REMAP = {
    "radarr": {},
    "sonarr": {
        "Bluray-1080p Remux": "Remux-1080p",
        "Bluray-2160p Remux": "Remux-2160p",
    },
}


def canon_quality(arr: str, name: str) -> str:
    return QUALITY_NAME_REMAP.get(arr, {}).get(name, name)


# ----- upstream vendoring ----------------------------------------------------


def fetch_upstream(refresh: bool) -> str:
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / "trash-pcd-1.initial.sql"
    if refresh or not target.exists():
        print(f"fetching {TRASH_PCD_URL}", file=sys.stderr)
        urllib.request.urlretrieve(TRASH_PCD_URL, target)
    return target.read_text()


SECTION_HEADER_RE = re.compile(r"^-- ([A-Z][A-Z0-9 _-]+)$")


def vendor_upstream(sql: str) -> str:
    """Walk the upstream file and drop the profile-related sections."""
    lines = sql.splitlines()
    out: list[str] = []
    keep = True
    i = 0
    while i < len(lines):
        line = lines[i]
        is_header = (
            line.startswith("-- ====")
            and i + 2 < len(lines)
            and lines[i + 2].startswith("-- ====")
        )
        if is_header:
            m = SECTION_HEADER_RE.match(lines[i + 1])
            if m:
                section = m.group(1).strip()
                keep = section not in DROP_SECTIONS
                if keep:
                    out.extend(lines[i : i + 3])
                i += 3
                continue
        if keep:
            out.append(line)
        i += 1
    return "\n".join(out).rstrip() + "\n"


# ----- profile rendering -----------------------------------------------------


def sql_str(s: str | None) -> str:
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def sql_bool(b: bool | None) -> str:
    return "1" if b else "0"


def load_profiles() -> list[dict]:
    profiles = []
    for arr in ("radarr", "sonarr"):
        path = EXPORTS / arr / "qualityprofile.json"
        if not path.exists():
            sys.exit(f"missing {path} — run scripts/export-arr.sh first")
        for p in json.loads(path.read_text()):
            p["_arr"] = arr
            profiles.append(p)
    return profiles


def assign_pcd_names(profiles: list[dict]) -> None:
    """Suffix [Radarr]/[Sonarr] when the same name appears in both arrs."""
    counts: dict[str, int] = {}
    for p in profiles:
        counts[p["name"]] = counts.get(p["name"], 0) + 1
    for p in profiles:
        if counts[p["name"]] > 1:
            tag = " [Radarr]" if p["_arr"] == "radarr" else " [Sonarr]"
            p["_pcd_name"] = p["name"] + tag
        else:
            p["_pcd_name"] = p["name"]


def cutoff_item(p: dict) -> dict | None:
    """Find the item (single quality or group) matching the profile's cutoff id."""
    cutoff_id = p.get("cutoff")
    for it in p["items"]:
        if it.get("items"):
            if it.get("id") == cutoff_id:
                return it
        else:
            if it.get("quality", {}).get("id") == cutoff_id:
                return it
    return None


def emit_profile_rows(p: dict, buckets: dict[str, list[str]]) -> None:
    name = p["_pcd_name"]
    arr = p["_arr"]

    buckets["quality_profiles"].append(
        "INSERT INTO quality_profiles "
        "(name, description, upgrades_allowed, minimum_custom_format_score, "
        "upgrade_until_score, upgrade_score_increment) VALUES ("
        f"{sql_str(name)}, NULL, {sql_bool(p.get('upgradeAllowed'))}, "
        f"{int(p.get('minFormatScore', 0))}, "
        f"{int(p.get('cutoffFormatScore', 0))}, 1);"
    )

    # PCD orders items best-first (position 0 = top). Radarr/Sonarr JSON gives
    # worst-first, so reverse.
    items = list(reversed(p["items"]))
    cutoff = cutoff_item(p)

    for it in items:
        if not it.get("items"):
            continue
        gname = it["name"]
        buckets["quality_groups"].append(
            "INSERT INTO quality_groups (quality_profile_name, name) VALUES ("
            f"{sql_str(name)}, {sql_str(gname)});"
        )
        for member in it["items"]:
            mq = canon_quality(arr, member["quality"]["name"])
            buckets["quality_group_members"].append(
                "INSERT INTO quality_group_members "
                "(quality_profile_name, quality_group_name, quality_name) VALUES ("
                f"{sql_str(name)}, {sql_str(gname)}, "
                f"{sql_str(mq)});"
            )

    for pos, it in enumerate(items):
        is_group = bool(it.get("items"))
        qname = "NULL" if is_group else sql_str(canon_quality(arr, it["quality"]["name"]))
        gname = sql_str(it["name"]) if is_group else "NULL"
        enabled = sql_bool(it.get("allowed"))
        upgrade_until = sql_bool(it is cutoff)
        buckets["quality_profile_qualities"].append(
            "INSERT INTO quality_profile_qualities "
            "(quality_profile_name, quality_name, quality_group_name, "
            "position, enabled, upgrade_until) VALUES ("
            f"{sql_str(name)}, {qname}, {gname}, {pos}, {enabled}, {upgrade_until});"
        )

    for fmt in p.get("formatItems", []):
        cf_name = CF_NAME_REMAP.get(fmt["name"], fmt["name"])
        buckets["quality_profile_custom_formats"].append(
            "INSERT INTO quality_profile_custom_formats "
            "(quality_profile_name, custom_format_name, arr_type, score) VALUES ("
            f"{sql_str(name)}, {sql_str(cf_name)}, {sql_str(arr)}, "
            f"{int(fmt['score'])});"
        )

    lang = p.get("language")
    if lang and lang.get("name"):
        buckets["quality_profile_languages"].append(
            "INSERT INTO quality_profile_languages "
            "(quality_profile_name, language_name, type) VALUES ("
            f"{sql_str(name)}, {sql_str(lang['name'])}, 'simple');"
        )


def render_profiles_sql(profiles: list[dict]) -> str:
    buckets: dict[str, list[str]] = {
        "quality_profiles": [],
        "quality_groups": [],
        "quality_group_members": [],
        "quality_profile_qualities": [],
        "quality_profile_custom_formats": [],
        "quality_profile_languages": [],
    }
    for p in profiles:
        emit_profile_rows(p, buckets)

    section_headers = {
        "quality_profiles": "QUALITY PROFILES",
        "quality_groups": "QUALITY GROUPS",
        "quality_group_members": "QUALITY GROUP MEMBERS",
        "quality_profile_qualities": "QUALITY PROFILE QUALITIES",
        "quality_profile_custom_formats": "QUALITY PROFILE CUSTOM FORMATS",
        "quality_profile_languages": "QUALITY PROFILE LANGUAGES",
    }

    out: list[str] = []
    out.append("-- Generated by scripts/regen-ops.py — do not edit by hand.")
    out.append("-- Source: exports/{radarr,sonarr}/qualityprofile.json")
    out.append("")
    for key, header in section_headers.items():
        rows = buckets[key]
        if not rows:
            continue
        out.append("-- ============================================================================")
        out.append(f"-- {header}")
        out.append("-- ============================================================================")
        out.append("")
        out.extend(rows)
        out.append("")
    return "\n".join(out)


# ----- main ------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--refresh", action="store_true", help="re-download trash-pcd upstream"
    )
    args = ap.parse_args()

    raw = fetch_upstream(args.refresh)
    vendored = vendor_upstream(raw)
    header = (
        "-- ============================================================================\n"
        f"-- VENDORED FROM {TRASH_PCD_URL}\n"
        "-- Regenerated by scripts/regen-ops.py. Do not edit by hand.\n"
        "-- ============================================================================\n\n"
    )
    OPS.mkdir(exist_ok=True)
    (OPS / "1.initial.sql").write_text(header + vendored)

    profiles = load_profiles()
    assign_pcd_names(profiles)
    print("profiles:", file=sys.stderr)
    for p in profiles:
        print(f"  [{p['_arr']:6}] {p['name']!r:30}  ->  {p['_pcd_name']!r}", file=sys.stderr)

    (OPS / "2.profiles.sql").write_text(render_profiles_sql(profiles))

    init_size = (OPS / "1.initial.sql").stat().st_size
    prof_size = (OPS / "2.profiles.sql").stat().st_size
    print(
        f"\nwrote ops/1.initial.sql ({init_size} B)"
        f"\nwrote ops/2.profiles.sql ({prof_size} B)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
