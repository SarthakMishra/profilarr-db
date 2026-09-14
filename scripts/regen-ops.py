#!/usr/bin/env python3
"""Regenerate ops/ from upstream trash-pcd + exports/{radarr,sonarr}/qualityprofile.json.

Strategy: we don't fork trash-pcd (PCD-to-PCD deps don't exist in Profilarr 2.0,
per Dictionarry-Hub/schema/docs/structure.md). Instead we vendor the upstream
CFs/regex/tags into ops/1.initial.sql and emit our own profile rows into
ops/2.profiles.sql. Re-run this script to pick up upstream CF updates.

Inputs:
  - All numbered ops/*.sql in Dictionarry-Hub/trash-pcd, using the pinned schema
  - exports/{radarr,sonarr}/qualityprofile.json
  - exports/{radarr,sonarr}/qualitydefinition.json
  - exports/{radarr,sonarr}/config-naming.json
  - exports/{radarr,sonarr}/config-mediamanagement.json
  - tweaks/delay-profiles.json (no arr API source for delay profiles)

Outputs:
  - ops/1.initial.sql  (vendored: TAGS, REGEX, CFs, conditions, naming presets)
  - ops/2.profiles.sql (local: profiles + quality defs + naming + media settings + delay)

Usage:
  python3 scripts/regen-ops.py            # uses cached upstream if present
  python3 scripts/regen-ops.py --refresh  # force re-download upstream
  python3 scripts/regen-ops.py --refresh --upstream-only  # no Arr exports needed
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORTS = REPO_ROOT / "exports"
OPS = REPO_ROOT / "ops"
TWEAKS = REPO_ROOT / "tweaks"
CACHE = REPO_ROOT / "scripts" / ".cache"

# Single name reused across radarr_naming, sonarr_naming, radarr_media_settings,
# sonarr_media_settings — Profilarr keys these tables by name and we have one
# active configuration per arr.
LOCAL_NAME = "Local"

# A 2,000-point HEVC advantage outweighs most group bonuses while leaving room
# for the existing -10,000 rejection scores, including Sonarr season packs.
CODEC_SCORES = {"x264": 1000, "x265": 3000}

# Absolute (minimum, preferred) MB/min targets. Preferred sizes are about 30%
# below the original HD settings; floors are about 50% below the originals.
# Fixed targets prevent repeated export/sync shrinkage.
# ponytail: size only approximates visual quality; tune per quality after sampling releases.
HEVC_SIZES = {
    "radarr": {
        "HDTV-720p": (8, 14),
        "WEBDL-720p": (6, 11),
        "WEBRip-720p": (6, 11),
        "Bluray-720p": (13, 21),
        "HDTV-1080p": (17, 28),
        "WEBDL-1080p": (6, 28),
        "WEBRip-1080p": (6, 28),
        "Bluray-1080p": (25, 42),
        "HDTV-2160p": (42, 70),
        "WEBDL-2160p": (17, 35),
        "WEBRip-2160p": (17, 35),
        "Bluray-2160p": (50, 77),
    },
    "sonarr": {
        "HDTV-720p": (8, 14),
        "WEBDL-720p": (8, 14),
        "WEBRip-720p": (8, 14),
        "Bluray-720p": (8, 14),
        "HDTV-1080p": (15, 28),
        "WEBDL-1080p": (8, 14),
        "WEBRip-1080p": (8, 14),
        "Bluray-1080p": (8, 18),
        "HDTV-2160p": (24, 39),
        "WEBDL-2160p": (20, 32),
        "WEBRip-2160p": (20, 32),
        "Bluray-2160p": (39, 60),
    },
}

TRASH_PCD_URL = "https://github.com/Dictionarry-Hub/trash-pcd"
SCHEMA_URL = "https://github.com/Dictionarry-Hub/schema"

# Keep local profile references stable across upstream renames/replacements.
UPSTREAM_CF_NAMES = {
    "x264 (Codec)": "x264",
    "x265 (Codec)": "x265",
    "WiTH AD": "WiTH.AD",
    "Hulu": "HULU",
}

# Only vendor formats and naming presets; profiles/settings belong to this repo.
# Dependency order matters when replaying the generated SQL with foreign keys on.
VENDORED_TABLES = (
    "tags", "regular_expressions", "custom_formats", "custom_format_conditions",
    "regular_expression_tags", "custom_format_tags", "condition_patterns",
    "condition_languages", "condition_sources", "condition_resolutions",
    "condition_quality_modifiers", "condition_indexer_flags", "condition_sizes",
    "condition_release_types", "condition_years", "custom_format_tests",
    "radarr_naming", "sonarr_naming",
)

# Normalize exported Arr names to the same names used by our vendored formats.
CF_NAME_REMAP = UPSTREAM_CF_NAMES

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
    target = CACHE / "trash-pcd.sql"
    schema_target = CACHE / "schema.sql"
    if refresh or not target.exists() or not schema_target.exists():
        version = json.loads((REPO_ROOT / "pcd.json").read_text())["dependencies"][SCHEMA_URL]
        with TemporaryDirectory() as tmp:
            upstream, schema = Path(tmp) / "upstream", Path(tmp) / "schema"
            for url, ref, path in ((TRASH_PCD_URL, "main", upstream),
                                   (SCHEMA_URL, version, schema)):
                subprocess.run(["git", "clone", "--quiet", "--depth", "1",
                                "--branch", ref, url, str(path)], check=True)
            upstream_version = json.loads((upstream / "pcd.json").read_text())["dependencies"][SCHEMA_URL]
            if upstream_version != version:
                raise ValueError(f"Upstream requires schema {upstream_version}; local manifest pins {version}")
            schema_sql = read_operations(schema)
            sql = snapshot_upstream(schema_sql, read_operations(upstream))
            commit = subprocess.check_output(
                ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
            schema_target.write_text(schema_sql)
            target.write_text(f"-- Upstream commit: {commit}\n" + sql)
    return target.read_text()


def read_operations(repo: Path) -> str:
    paths = sorted((repo / "ops").glob("*.sql"), key=lambda p: int(p.name.split(".")[0]))
    if not paths:
        raise ValueError(f"No SQL operations in {repo}")
    return "\n".join(path.read_text() for path in paths)


def snapshot_upstream(schema_sql: str, upstream_sql: str) -> str:
    with sqlite3.connect(":memory:") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(schema_sql + "\n" + upstream_sql)
        for upstream, local in UPSTREAM_CF_NAMES.items():
            db.execute("UPDATE custom_formats SET name = ? WHERE name = ?", (local, upstream))
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Upstream has foreign key violations")
        lines = []
        for table in VENDORED_TABLES:
            # Generated IDs and timestamps would cause a new diff on every run.
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")
                       if r[1] not in {"id", "created_at", "updated_at"}]
            names = ", ".join(columns)
            quoted = ", ".join(f"quote({column})" for column in columns)
            lines.extend(f"INSERT INTO {table} ({names}) VALUES ({', '.join(row)});"
                         for row in db.execute(f"SELECT {quoted} FROM {table} ORDER BY {names}"))
        return "\n".join(lines) + "\n"


# Matches an INSERT into custom_format_conditions, capturing the CF name (g1),
# the literal prefix up to and including ", " before arr_type (g2), the arr_type
# value (g3), and the trailing suffix from the closing quote onward (g4).
# Used to retag arr_type='radarr' to 'all' when a CF is used in a Sonarr
# profile but upstream provides no sonarr/all conditions for it (without the
# retag, Profilarr sends an empty `specifications` array to Sonarr and the
# sync 400s with "Specifications must not be empty").
CONDITION_INSERT_RE = re.compile(
    r"^(INSERT INTO custom_format_conditions \([^)]+\) VALUES "
    r"\('([^']+)', '[^']+', '[^']+', )'([a-z]+)'(.*)$"
)


def vendor_upstream(sql: str) -> str:
    """Apply the local codec policy to the upstream snapshot."""
    # Upstream's Sonarr x264 condition only excludes remux. Share the codec
    # title match too, otherwise every non-remux receives the x264 bonus.
    return sql.replace(
        "'x264', 'x|h264', 'release_title', 'radarr'",
        "'x264', 'x|h264', 'release_title', 'all'",
    )


def cfs_needing_sonarr_promotion(sql: str, sonarr_cfs: set[str]) -> set[str]:
    """CFs that are Sonarr-referenced but have only 'radarr' conditions upstream."""
    by_cf: dict[str, set[str]] = {}
    for line in sql.splitlines():
        m = CONDITION_INSERT_RE.match(line)
        if m:
            by_cf.setdefault(m.group(2), set()).add(m.group(3))
    return {cf for cf in sonarr_cfs if by_cf.get(cf) == {"radarr"}}


def promote_radarr_to_all(sql: str, cfs: set[str]) -> tuple[str, int]:
    """Rewrite arr_type='radarr' to 'all' on conditions for the given CFs."""
    if not cfs:
        return sql, 0
    out: list[str] = []
    promoted = 0
    for line in sql.splitlines():
        m = CONDITION_INSERT_RE.match(line)
        if m and m.group(2) in cfs and m.group(3) == "radarr":
            line = f"{m.group(1)}'all'{m.group(4)}"
            promoted += 1
        out.append(line)
    return "\n".join(out) + "\n", promoted


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
    """Always suffix profile names with [Radarr] or [Sonarr] for clarity."""
    for p in profiles:
        tag = " [Radarr]" if p["_arr"] == "radarr" else " [Sonarr]"
        p["_pcd_name"] = p["name"] + tag


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
            f"{int(CODEC_SCORES.get(cf_name, fmt['score']))});"
        )

    lang = p.get("language")
    if lang and lang.get("name"):
        buckets["quality_profile_languages"].append(
            "INSERT INTO quality_profile_languages "
            "(quality_profile_name, language_name, type) VALUES ("
            f"{sql_str(name)}, {sql_str(lang['name'])}, 'simple');"
        )


def sql_int_or_null(v) -> str:
    return "NULL" if v is None else str(int(v))


def load_json(path: Path) -> dict | list | None:
    return json.loads(path.read_text()) if path.exists() else None


def emit_quality_definitions_rows(buckets: dict[str, list[str]]) -> None:
    """*_quality_definitions rows from exports/*/qualitydefinition.json.

    Schema is INTEGER min/max/preferred so float arr values are rounded.
    Sonarr's Bluray-{1080,2160}p Remux are canonicalized to Remux-{1080,2160}p
    via the existing QUALITY_NAME_REMAP.
    """
    for arr in ("radarr", "sonarr"):
        defs = load_json(EXPORTS / arr / "qualitydefinition.json")
        if not defs:
            continue
        for d in defs:
            qname = canon_quality(arr, d["quality"]["name"])
            min_size, preferred_size = HEVC_SIZES[arr].get(
                qname, (round(d["minSize"]), round(d["preferredSize"]))
            )
            if not 0 <= min_size <= preferred_size <= round(d["maxSize"]):
                raise ValueError(f"{arr} {qname}: expected min <= preferred <= max")
            buckets[f"{arr}_quality_definitions"].append(
                f"INSERT INTO {arr}_quality_definitions "
                "(name, quality_name, min_size, max_size, preferred_size) VALUES ("
                f"{sql_str(LOCAL_NAME)}, {sql_str(qname)}, "
                f"{min_size}, {round(d['maxSize'])}, "
                f"{preferred_size});"
            )


def emit_naming_rows(buckets: dict[str, list[str]]) -> None:
    """radarr_naming + sonarr_naming rows from exports/*/config-naming.json."""
    r = load_json(EXPORTS / "radarr" / "config-naming.json")
    if r:
        buckets["radarr_naming"].append(
            "INSERT INTO radarr_naming "
            "(name, rename, movie_format, movie_folder_format, "
            "replace_illegal_characters, colon_replacement_format) VALUES ("
            f"{sql_str(LOCAL_NAME)}, {sql_bool(r.get('renameMovies'))}, "
            f"{sql_str(r['standardMovieFormat'])}, "
            f"{sql_str(r['movieFolderFormat'])}, "
            f"{sql_bool(r.get('replaceIllegalCharacters'))}, "
            f"{sql_str(r.get('colonReplacementFormat', 'smart'))});"
        )
    s = load_json(EXPORTS / "sonarr" / "config-naming.json")
    if s:
        custom_colon = s.get("customColonReplacementFormat") or None
        buckets["sonarr_naming"].append(
            "INSERT INTO sonarr_naming "
            "(name, rename, standard_episode_format, daily_episode_format, "
            "anime_episode_format, series_folder_format, season_folder_format, "
            "replace_illegal_characters, colon_replacement_format, "
            "custom_colon_replacement_format, multi_episode_style) VALUES ("
            f"{sql_str(LOCAL_NAME)}, {sql_bool(s.get('renameEpisodes'))}, "
            f"{sql_str(s['standardEpisodeFormat'])}, "
            f"{sql_str(s['dailyEpisodeFormat'])}, "
            f"{sql_str(s['animeEpisodeFormat'])}, "
            f"{sql_str(s['seriesFolderFormat'])}, "
            f"{sql_str(s['seasonFolderFormat'])}, "
            f"{sql_bool(s.get('replaceIllegalCharacters'))}, "
            f"{int(s.get('colonReplacementFormat', 4))}, "
            f"{sql_str(custom_colon)}, "
            f"{int(s.get('multiEpisodeStyle', 5))});"
        )


def emit_media_settings_rows(buckets: dict[str, list[str]]) -> None:
    """*_media_settings rows from exports/*/config-mediamanagement.json."""
    for arr in ("radarr", "sonarr"):
        cfg = load_json(EXPORTS / arr / "config-mediamanagement.json")
        if not cfg:
            continue
        buckets[f"{arr}_media_settings"].append(
            f"INSERT INTO {arr}_media_settings "
            "(name, propers_repacks, enable_media_info) VALUES ("
            f"{sql_str(LOCAL_NAME)}, "
            f"{sql_str(cfg.get('downloadPropersAndRepacks', 'doNotPrefer'))}, "
            f"{sql_bool(cfg.get('enableMediaInfo'))});"
        )


def emit_delay_profile_rows(buckets: dict[str, list[str]]) -> None:
    """delay_profiles rows from tweaks/delay-profiles.json (no arr API source)."""
    profiles = load_json(TWEAKS / "delay-profiles.json")
    if not profiles:
        return
    for d in profiles:
        buckets["delay_profiles"].append(
            "INSERT INTO delay_profiles "
            "(name, preferred_protocol, usenet_delay, torrent_delay, "
            "bypass_if_highest_quality, bypass_if_above_custom_format_score, "
            "minimum_custom_format_score) VALUES ("
            f"{sql_str(d['name'])}, {sql_str(d['preferred_protocol'])}, "
            f"{sql_int_or_null(d.get('usenet_delay'))}, "
            f"{sql_int_or_null(d.get('torrent_delay'))}, "
            f"{int(d.get('bypass_if_highest_quality', 0))}, "
            f"{int(d.get('bypass_if_above_custom_format_score', 0))}, "
            f"{sql_int_or_null(d.get('minimum_custom_format_score'))});"
        )


def render_profiles_sql(profiles: list[dict]) -> str:
    buckets: dict[str, list[str]] = {
        "quality_profiles": [],
        "quality_groups": [],
        "quality_group_members": [],
        "quality_profile_qualities": [],
        "quality_profile_custom_formats": [],
        "quality_profile_languages": [],
        "radarr_quality_definitions": [],
        "sonarr_quality_definitions": [],
        "radarr_naming": [],
        "sonarr_naming": [],
        "radarr_media_settings": [],
        "sonarr_media_settings": [],
        "delay_profiles": [],
    }
    for p in profiles:
        emit_profile_rows(p, buckets)
    emit_quality_definitions_rows(buckets)
    emit_naming_rows(buckets)
    emit_media_settings_rows(buckets)
    emit_delay_profile_rows(buckets)

    section_headers = {
        "quality_profiles": "QUALITY PROFILES",
        "quality_groups": "QUALITY GROUPS",
        "quality_group_members": "QUALITY GROUP MEMBERS",
        "quality_profile_qualities": "QUALITY PROFILE QUALITIES",
        "quality_profile_custom_formats": "QUALITY PROFILE CUSTOM FORMATS",
        "quality_profile_languages": "QUALITY PROFILE LANGUAGES",
        "radarr_quality_definitions": "RADARR QUALITY DEFINITIONS",
        "sonarr_quality_definitions": "SONARR QUALITY DEFINITIONS",
        "radarr_naming": "RADARR NAMING",
        "sonarr_naming": "SONARR NAMING",
        "radarr_media_settings": "RADARR MEDIA SETTINGS",
        "sonarr_media_settings": "SONARR MEDIA SETTINGS",
        "delay_profiles": "DELAY PROFILES",
    }

    out: list[str] = []
    out.append("-- Generated by scripts/regen-ops.py — do not edit by hand.")
    out.append("-- Sources: exports/{radarr,sonarr}/{qualityprofile,qualitydefinition,config-naming,config-mediamanagement}.json")
    out.append("--          tweaks/delay-profiles.json")
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
    ap.add_argument(
        "--upstream-only", action="store_true",
        help="sync formats using committed profiles; leave ops/2.profiles.sql unchanged",
    )
    args = ap.parse_args()

    raw = fetch_upstream(args.refresh)
    vendored = vendor_upstream(raw)

    if args.upstream_only:
        profiles_sql = (OPS / "2.profiles.sql").read_text()
    else:
        profiles = load_profiles()
        assign_pcd_names(profiles)
        print("profiles:", file=sys.stderr)
        for p in profiles:
            print(f"  [{p['_arr']:6}] {p['name']!r:30}  ->  {p['_pcd_name']!r}", file=sys.stderr)
        profiles_sql = render_profiles_sql(profiles)

    # Validate before touching either output, including when authoring profiles.
    with sqlite3.connect(":memory:") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript((CACHE / "schema.sql").read_text() + "\n" + vendored
                         + "\n" + profiles_sql)
        sonarr_cfs = {r[0] for r in db.execute(
            "SELECT custom_format_name FROM quality_profile_custom_formats "
            "WHERE arr_type IN ('sonarr', 'all')")}
    to_promote = cfs_needing_sonarr_promotion(vendored, sonarr_cfs)
    vendored, n = promote_radarr_to_all(vendored, to_promote)
    if to_promote:
        print(
            f"\npromoted {n} condition(s) on {len(to_promote)} CF(s) "
            f"from arr_type='radarr' to 'all' (Sonarr-referenced, no sonarr conditions upstream):\n  "
            + ", ".join(sorted(to_promote)),
            file=sys.stderr,
        )

    header = (
        "-- ============================================================================\n"
        f"-- VENDORED FROM {TRASH_PCD_URL}\n"
        "-- Regenerated by scripts/regen-ops.py. Do not edit by hand.\n"
        "-- ============================================================================\n\n"
    )
    OPS.mkdir(exist_ok=True)
    (OPS / "1.initial.sql").write_text(header + vendored)

    if not args.upstream_only:
        (OPS / "2.profiles.sql").write_text(profiles_sql)

    init_size = (OPS / "1.initial.sql").stat().st_size
    prof_size = (OPS / "2.profiles.sql").stat().st_size
    print(
        f"\nwrote ops/1.initial.sql ({init_size} B)"
        f"\n{'preserved' if args.upstream_only else 'wrote'} ops/2.profiles.sql ({prof_size} B)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
