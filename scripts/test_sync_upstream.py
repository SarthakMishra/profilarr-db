"""Sync regression check. Run after regen-ops.py --refresh --upstream-only."""

import runpy
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    root = Path(__file__).resolve().parent.parent
    regen = runpy.run_path(str(root / "scripts/regen-ops.py"))
    schema = (root / "scripts/.cache/schema.sql").read_text()
    with TemporaryDirectory() as tmp:
        clean = Path(tmp)
        # A runner has committed SQL and scripts, but no private Arr exports.
        for name in ("scripts/regen-ops.py", "ops/1.initial.sql", "ops/2.profiles.sql",
                     "scripts/.cache/schema.sql", "scripts/.cache/trash-pcd.sql"):
            target = clean / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
        original_profiles = (clean / "ops/2.profiles.sql").read_bytes()
        command = [sys.executable, str(clean / "scripts/regen-ops.py"), "--upstream-only"]
        subprocess.run(command, check=True, capture_output=True)
        first = (clean / "ops/1.initial.sql").read_bytes()
        subprocess.run(command, check=True, capture_output=True)
        assert (clean / "ops/1.initial.sql").read_bytes() == first, "Sync must be repeatable"
        assert (clean / "ops/2.profiles.sql").read_bytes() == original_profiles
        with sqlite3.connect(":memory:") as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(schema + "\n" + first.decode() + "\n" + original_profiles.decode())
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("SELECT count(*) FROM quality_profiles").fetchone()[0] == 8
            # No Sonarr-referenced CF may lose all its applicable conditions.
            assert db.execute("""
                SELECT DISTINCT p.custom_format_name FROM quality_profile_custom_formats p
                WHERE p.arr_type IN ('sonarr', 'all') AND NOT EXISTS (
                    SELECT 1 FROM custom_format_conditions c
                    WHERE c.custom_format_name = p.custom_format_name
                      AND c.arr_type IN ('sonarr', 'all'))
            """).fetchall() == []

        # An upstream removal must fail before overwriting either committed file.
        with (clean / "ops/2.profiles.sql").open("a") as f:
            f.write("\nINSERT INTO quality_profile_custom_formats VALUES "
                    "('Any [Sonarr]', 'Missing upstream format', 'sonarr', 0);\n")
        invalid_profiles = (clean / "ops/2.profiles.sql").read_bytes()
        result = subprocess.run(command, capture_output=True, text=True)
        assert result.returncode != 0 and "FOREIGN KEY" in result.stderr
        assert (clean / "ops/1.initial.sql").read_bytes() == first
        assert (clean / "ops/2.profiles.sql").read_bytes() == invalid_profiles

        # Apply migrations numerically, retain SQL quoting, and omit timestamps.
        operations = clean / "upstream/ops"
        operations.mkdir(parents=True)
        (operations / "1.initial.sql").write_text("""
            INSERT INTO custom_formats (name) VALUES ('x264 (Codec)');
            INSERT INTO custom_format_conditions
              (custom_format_name, name, type, arr_type, negate, required)
              VALUES ('x264 (Codec)', 'x|h264', 'release_title', 'radarr', 0, 1);
            INSERT INTO regular_expressions (name, pattern) VALUES ('quote', 'a''b');
            INSERT INTO condition_patterns VALUES ('x264 (Codec)', 'x|h264', 'quote');
            INSERT INTO quality_profiles (name) VALUES ('Upstream only');
        """)
        (operations / "2.update.sql").write_text(
            "UPDATE regular_expressions SET pattern = 'second';")
        (operations / "10.update.sql").write_text(
            "UPDATE regular_expressions SET pattern = 'last''s';")
        sql = regen["read_operations"](operations.parent)
        snapshot = regen["snapshot_upstream"](schema, sql)
        assert snapshot == regen["snapshot_upstream"](schema, sql + """
            UPDATE custom_formats SET created_at = '2099-01-01', updated_at = '2099-01-01';
        """)
        with sqlite3.connect(":memory:") as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(schema + snapshot)
            assert db.execute("SELECT pattern FROM regular_expressions").fetchall() == [("last's",)]
            assert db.execute("SELECT name FROM custom_formats").fetchall() == [("x264",)]
            assert db.execute("SELECT custom_format_name FROM condition_patterns").fetchall() == [("x264",)]
            assert db.execute("SELECT name FROM quality_profiles").fetchall() == []
    print("Sync checks passed: profiles preserved, repeatable output, ordered migrations, FK failures blocked")


if __name__ == "__main__":
    main()
