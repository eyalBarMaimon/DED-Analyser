"""
Release packager — run via: python release.py
Creates releases/<version>.zip with all source files.
Version is read from VERSION file. Update VERSION before running.
"""
import zipfile
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
RELEASES = ROOT / "releases"

# Files and directories to include in the release
INCLUDE_FILES = [
    "app.py",
    "meltio_ded_analyzer.py",
    "m600_gcode_parser.py",
    "sensor_analyzer.py",
    "materials_database.json",
    "requirements.txt",
    "settings.json",
    "ux.html",
    "ux_m600.html",
    "hero.html",
    "VERSION",
    "DED Analyser-Heat map.bat",
]

INCLUDE_DIRS = [
    "engines",
    "jobs",
    "tests",
    "workflow",
]

# Patterns to exclude
EXCLUDE_SUFFIXES = {".pyc", ".tmp"}
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".claude"}


def should_exclude(path: pathlib.Path) -> bool:
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    for part in path.parts:
        if part in EXCLUDE_DIRS:
            return True
    return False


def main():
    version_file = ROOT / "VERSION"
    if not version_file.exists():
        print("ERROR: VERSION file not found"); sys.exit(1)

    version = version_file.read_text().strip()
    zip_name = f"DED-Analyser-Heat-map-v{version}.zip"
    zip_path = RELEASES / zip_name

    RELEASES.mkdir(exist_ok=True)

    if zip_path.exists():
        print(f"ERROR: {zip_path} already exists — bump VERSION first")
        sys.exit(1)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        count = 0
        for name in INCLUDE_FILES:
            p = ROOT / name
            if p.exists():
                zf.write(p, name)
                count += 1

        for dir_name in INCLUDE_DIRS:
            d = ROOT / dir_name
            if not d.exists():
                continue
            for p in sorted(d.rglob("*")):
                if p.is_file() and not should_exclude(p):
                    arc = p.relative_to(ROOT)
                    zf.write(p, arc)
                    count += 1

    size_kb = zip_path.stat().st_size // 1024
    print(f"OK {zip_name}  ({count} files, {size_kb} KB)")
    print(f"  -> {zip_path}")


if __name__ == "__main__":
    main()
