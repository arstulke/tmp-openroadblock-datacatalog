#!/usr/bin/env python3
"""Build a static WZDx feed registry from dummy-data/ into dist/.

The machine-readable output mirrors the USDOT "Work Zone Data Feed Registry"
(https://data.transportation.gov/d/69qe-yiui), so consumers that already
read the USDOT registry can read this one the same way:

    feed-registry.json   same field names as the Socrata JSON API
                         (data.transportation.gov/resource/69qe-yiui.json)
    feed-registry.csv    same columns as the Socrata CSV export
                         (data.transportation.gov/api/views/69qe-yiui/rows.csv)

Both add the columns bounding_box and disclaimer. The registry is not
affiliated with USDOT; it only reuses the WZDx data format (see DISCLAIMER).

The pages, sitemap and robots.txt are rendered from the Jinja templates in
scripts/templates/; the page's CSS and JS live in scripts/static/.

Layout of the input:   dummy-data/<dataset-id>/wzdx/<file>.json
                       <dataset-id> starts with an ISO 3166-2 code in lower
                       case and with "_" instead of "-", e.g. de_ni_...
Layout of the output:
    dist/index.html                          landing page, links to dist/wzdx/
    dist/wzdx/index.html                     human-readable list of all feeds
    dist/wzdx/feed-registry.json             registry, Socrata JSON layout
    dist/wzdx/feed-registry.csv              registry, Socrata CSV layout
    dist/wzdx/static/                        CSS and JS of the page
    dist/data/<dataset-id>/wzdx/<file>       copies of the WZDx feeds
    dist/sitemap.xml                         lists the landing and registry page
    dist/robots.txt                          points to the sitemap

Every file below a wzdx/ directory becomes one registry entry. Files in
other directories are skipped.

The registry's sdate (start date of an entry) is the date the feed file was
first committed to git, so the checkout needs the full history.

Paths are resolved relative to the current working directory, so run it
from the repository root.

Usage:
    nix run                              # via the flake
    uv run scripts/build_catalog.py      # inside `nix develop`
"""

import csv
import io
import json
import math
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

# Relative to the working directory: when run via `nix run` the script itself
# lives in the read-only Nix store, so __file__ can't be used to find the repo.
ROOT = Path.cwd()
SRC_DIR = ROOT / "dummy-data"
OUT_DIR = ROOT / "dist"

# The page templates and their CSS/JS ship next to this script (also in the
# Nix store, where the flake copies the whole scripts/ directory).
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Public URL dist/ is served from (GitHub Pages of this repo).
BASE_URL = "https://www.arne-stulken.de/tmp-openroadblock-datacatalog"

CATALOG_TITLE = "OpenRoadblock WZDx Feed Registry"
CATALOG_DESCRIPTION = (
    "Registry of Work Zone Data Exchange (WZDx) feeds for road works and road closures."
)

WZDX_SPEC = "https://github.com/usdot-jpo-ode/wzdx"

# This registry only borrows the WZDx data format (and the USDOT registry's
# layout); it must not look like official USDOT content.
DISCLAIMER = (
    "This registry is not affiliated with, endorsed by or operated by the U.S. Department "
    "of Transportation (USDOT) or the WZDx project. It only reuses the WZDx specification "
    "as data format. The data of each feed are published by the organization named as "
    "its issuing organization."
)

# The USDOT registry stores the lower-case name of the US state. Outside the
# US we use the lower-case English name of the ISO 3166-2 subdivision.
SUBDIVISIONS = {
    "DE-BW": "baden-württemberg",
    "DE-BY": "bavaria",
    "DE-BE": "berlin",
    "DE-BB": "brandenburg",
    "DE-HB": "bremen",
    "DE-HH": "hamburg",
    "DE-HE": "hesse",
    "DE-MV": "mecklenburg-western pomerania",
    "DE-NI": "lower saxony",
    "DE-NW": "north rhine-westphalia",
    "DE-RP": "rhineland-palatinate",
    "DE-SL": "saarland",
    "DE-SN": "saxony",
    "DE-ST": "saxony-anhalt",
    "DE-SH": "schleswig-holstein",
    "DE-TH": "thuringia",
}

# Columns of the USDOT registry: (Socrata JSON field name, CSV column name).
COLUMNS = [
    ("state", "State"),
    ("issuingorganization", "issuingOrganization"),
    ("feedname", "feedName"),
    ("url", "url"),
    ("format", "format"),
    ("active", "active"),
    ("datafeed_frequency_update", "DatafeedUpdateFrequency"),
    ("version", "version"),
    ("sdate", "sdate"),
    ("edate", "edate"),
    ("needapikey", "needAPIKey"),
    ("apikeyurl", "apiKeyURL"),
    ("geocoded_column", "StateCoordinate"),
    # Not in the USDOT registry: the area the feed covers, as a Socrata
    # Polygon column, so consumers can skip feeds outside their area.
    ("bounding_box", "BoundingBox"),
    # Not in the USDOT registry: JSON and CSV have no room for a file-level
    # notice without breaking the Socrata layout, so every row carries it.
    ("disclaimer", "disclaimer"),
]


# --------------------------------------------------------------------------
# Reading feeds
# --------------------------------------------------------------------------

def _walk_coords(obj):
    """Yield all [lon, lat] pairs from a GeoJSON coordinates array."""
    if isinstance(obj, (list, tuple)):
        if len(obj) >= 2 and all(isinstance(v, (int, float)) for v in obj[:2]):
            yield obj[0], obj[1]
        else:
            for item in obj:
                yield from _walk_coords(item)


def _geojson_bbox(doc):
    if isinstance(doc.get("bbox"), list) and len(doc["bbox"]) >= 4:
        return doc["bbox"][:4]
    coords = [
        c
        for f in doc.get("features", [])
        for c in _walk_coords((f.get("geometry") or {}).get("coordinates"))
    ]
    if not coords:
        return None
    lons, lats = zip(*coords)
    return [min(lons), min(lats), max(lons), max(lats)]


def read_feed(path):
    """Extract the registry-relevant metadata from a WZDx feed."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    info = doc["feed_info"]
    return {
        "publisher": info["publisher"],
        "version": info["version"],
        "license": info.get("license"),
        "update_date": info.get("update_date"),
        "update_frequency": info.get("update_frequency"),
        "contact_name": info.get("contact_name"),
        "contact_email": info.get("contact_email"),
        "disclaimer": doc.get("disclaimer"),
        "bbox": _geojson_bbox(doc),
        "record_count": len(doc.get("features", [])),
    }


def collect(src):
    feeds = []
    for ds_dir in sorted(p for p in src.iterdir() if p.is_dir()):
        for file in sorted(p for p in ds_dir.rglob("*") if p.is_file()):
            rel = file.relative_to(ds_dir)
            if len(rel.parts) < 2 or rel.parts[0] != "wzdx":
                print(f"warning: skipping {file}: not below a wzdx/ directory")
                continue
            try:
                meta = read_feed(file)
            except (ValueError, KeyError, TypeError, UnicodeDecodeError) as e:
                print(f"warning: skipping {file}: not a readable WZDx feed ({e!r})")
                continue
            feeds.append({
                "id": f"{ds_dir.name}-{file.stem}",
                "dataset": ds_dir.name,
                "path": Path(ds_dir.name) / rel,
                "src": file,
                "state": state_for(ds_dir.name),
                "sdate": first_commit_date(file),
                **meta,
            })
    return feeds


def state_for(dataset_id):
    """Map a dataset id like 'de_ni_dummy_city-a' to the registry's state."""
    parts = dataset_id.split("_")
    code = "-".join(parts[:2]).upper() if len(parts) >= 2 else ""
    return SUBDIVISIONS.get(code, "n/a")


def first_commit_date(path):
    """Date the file was first committed, or None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "log", "--follow", "--diff-filter=A", "--format=%cI", "--", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    return out[-1][:10] if out else None


# --------------------------------------------------------------------------
# Registry entries
# --------------------------------------------------------------------------

def frequency(seconds):
    """Format seconds like the registry does: '30s', '15m', '24h'."""
    if not seconds:
        return None
    if seconds % 3600 == 0 and seconds > 3600:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def centroid(bbox):
    if not bbox:
        return None
    w, s, e, n = bbox
    return [round((w + e) / 2, 5), round((s + n) / 2, 5)]


def bbox_polygon(bbox):
    """The bbox as a closed GeoJSON polygon ring, rounded outwards to 5 decimals
    (about 1 m) so the rounded box still covers every work zone."""
    if not bbox:
        return None
    w, s, e, n = bbox
    w, s = (math.floor(round(v * 1e5, 6)) / 1e5 for v in (w, s))
    e, n = (math.ceil(round(v * 1e5, 6)) / 1e5 for v in (e, n))
    return [[[w, s], [e, s], [e, n], [w, n], [w, s]]]


def feed_url(feed, base):
    return f"{base}/data/{feed['path'].as_posix()}"


def registry_entry(feed, base):
    """One row in the Socrata JSON layout (unset fields are omitted)."""
    entry = {
        "state": feed["state"],
        "issuingorganization": feed["publisher"],
        "feedname": feed["id"],
        "url": {"url": feed_url(feed, base)},
        "format": "geojson",
        "active": True,
        "datafeed_frequency_update": frequency(feed["update_frequency"]),
        "version": feed["version"],
        "sdate": f"{feed['sdate']}T00:00:00.000" if feed["sdate"] else None,
        "needapikey": False,
    }
    if point := centroid(feed["bbox"]):
        entry["geocoded_column"] = {"type": "Point", "coordinates": point}
    if ring := bbox_polygon(feed["bbox"]):
        entry["bounding_box"] = {"type": "Polygon", "coordinates": ring}
    entry["disclaimer"] = (
        "Not affiliated with the U.S. Department of Transportation (USDOT) or the WZDx project; "
        "this registry only reuses the WZDx specification as data format. "
        f"The data of this feed are published by {feed['publisher']}."
    )
    return {k: v for k, v in entry.items() if v is not None}


def registry_csv(entries):
    """The registry in the Socrata CSV export layout."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(name for _, name in COLUMNS)
    for entry in entries:
        row = []
        for field, _ in COLUMNS:
            value = entry.get(field)
            if isinstance(value, bool):
                value = str(value).lower()
            elif field in ("url", "apikeyurl") and value:
                value = value["url"]
            elif field in ("sdate", "edate") and value:
                value = datetime.fromisoformat(value).strftime("%m/%d/%Y %I:%M:%S %p")
            elif field == "geocoded_column" and value:
                value = "POINT ({} {})".format(*value["coordinates"])
            elif field == "bounding_box" and value:
                value = "POLYGON (({}))".format(", ".join(f"{x} {y}" for x, y in value["coordinates"][0]))
            row.append("" if value is None else value)
        writer.writerow(row)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Human-readable page
# --------------------------------------------------------------------------

# Short names for licenses that feeds commonly use; others show their URL.
LICENSES = {
    "https://creativecommons.org/publicdomain/zero/1.0/": "CC0 1.0",
    "https://creativecommons.org/licenses/by/4.0/": "CC BY 4.0",
    "https://www.govdata.de/dl-de/zero-2-0": "dl-de/zero-2-0",
    "https://www.govdata.de/dl-de/by-2-0": "dl-de/by-2-0",
    "https://opendatacommons.org/licenses/odbl/1-0/": "ODbL 1.0",
}


def format_timestamp(value):
    """'2026-10-18T08:00:00Z' -> '2026-10-18 08:00 UTC' (unparsable values pass through)."""
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return value
    if ts.tzinfo:
        ts = ts.astimezone(timezone.utc)
        return ts.strftime("%Y-%m-%d %H:%M UTC")
    return ts.strftime("%Y-%m-%d %H:%M")


def template_env():
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html.j2", "xml.j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["timestamp"] = format_timestamp
    env.filters["license_name"] = lambda url: LICENSES.get(url, url)
    return env


def page_feed(feed, entry, base):
    """The fields of one feed that the page template shows."""
    search = [feed["publisher"], feed["state"], feed["id"], feed["contact_name"]]
    bbox = [round(v, 4) for v in feed["bbox"]] if feed["bbox"] else None
    return {
        **feed,
        "url": feed_url(feed, base),
        "frequency": entry.get("datafeed_frequency_update"),
        "bbox": bbox,
        "map_url": "https://www.openstreetmap.org/?minlon={}&minlat={}&maxlon={}&maxlat={}".format(*bbox) if bbox else None,
        "search": " ".join(filter(None, search)).lower(),
    }


def page_context(feeds, entries, base):
    """Everything the templates need; they do no lookups of their own."""
    # Group by region so a long list stays scannable.
    groups = {}
    for feed, entry in sorted(zip(feeds, entries), key=lambda fe: (fe[0]["state"], fe[0]["publisher"].lower())):
        groups.setdefault(feed["state"], []).append(page_feed(feed, entry, base))
    return {
        "title": CATALOG_TITLE,
        "description": CATALOG_DESCRIPTION,
        "disclaimer": DISCLAIMER,
        "wzdx_spec": WZDX_SPEC,
        "base": base,
        "groups": groups,
        "feed_count": len(feeds),
        "zone_count": sum(f["record_count"] for f in feeds),
        "today": date.today().isoformat(),
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main():
    base = BASE_URL.rstrip("/")
    out = OUT_DIR

    feeds = collect(SRC_DIR)
    entries = [registry_entry(f, base) for f in feeds]

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for feed in feeds:
        target = out / "data" / feed["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(feed["src"], target)

    write(out / "wzdx" / "feed-registry.json", json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
    write(out / "wzdx" / "feed-registry.csv", registry_csv(entries))
    env = template_env()
    context = page_context(feeds, entries, base)
    write(out / "index.html", env.get_template("root.html.j2").render(context))
    write(out / "wzdx" / "index.html", env.get_template("index.html.j2").render(context))
    write(out / "sitemap.xml", env.get_template("sitemap.xml.j2").render(context))
    write(out / "robots.txt", env.get_template("robots.txt.j2").render(context))
    # Contents only: under `nix run` the sources are read-only store files,
    # and copied permissions would stop the next build from cleaning dist/.
    for file in (p for p in STATIC_DIR.rglob("*") if p.is_file()):
        target = out / "wzdx" / "static" / file.relative_to(STATIC_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)

    built = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    print(f"Built feed registry with {len(feeds)} WZDx feeds in {out} at {built}")


if __name__ == "__main__":
    main()
