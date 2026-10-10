"""Fetch and validate the point-only outage layer before preparing an update."""

import io
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

from csv_diff import compare, human_text, load_json


QUERY_URL = "https://ags.pge.esriemcs.com/arcgis/rest/services/43/outages/MapServer/5/query"
KEY = "F_OUTAGE_ID"


def query(**params):
    # Each query bypasses caches, including the empty-result confirmation.
    url = QUERY_URL + "?" + urlencode({
        "where": "1=1", "f": "json", "_": uuid4().hex, **params
    })
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def validate_response(data):
    if not isinstance(data, dict) or "error" in data:
        raise ValueError("Invalid ArcGIS response: {!r}".format(data))
    if data.get("exceededTransferLimit", False) is not False:
        raise ValueError("ArcGIS returned a truncated result; refusing to archive it")


def index_rows(rows):
    if not isinstance(rows, list):
        raise ValueError("Snapshot must be a JSON array")
    ids = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(KEY), str) or not row[KEY]:
            raise ValueError("Each outage must have a non-empty F_OUTAGE_ID string")
        if row[KEY] in ids:
            raise ValueError("Duplicate F_OUTAGE_ID: " + row[KEY])
        ids.add(row[KEY])
    return load_json(io.StringIO(json.dumps(rows)), key=KEY)


def snapshot(data):
    validate_response(data)
    fields = data.get("fields")
    if (not isinstance(fields, list)
            or not any(isinstance(f, dict) and f.get("name") == KEY for f in fields)
            or data.get("geometryType") != "esriGeometryPoint"):
        raise ValueError("ArcGIS response is missing the expected point-layer schema")
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError("ArcGIS response must contain a features array")
    rows = []
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("attributes"), dict):
            raise ValueError("Invalid ArcGIS feature attributes")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or not all(k in geometry for k in ("x", "y")):
            raise ValueError("Invalid ArcGIS point geometry")
        row = {k: v for k, v in feature["attributes"].items()
               if k not in ("blueSkyNotificationSubscription", "OBJECTID")}
        row.update(geometry_x=geometry["x"], geometry_y=geometry["y"])
        rows.append(row)
    index_rows(rows)
    return rows


def fetch_snapshot(fetch=query, pause=time.sleep):
    rows = snapshot(fetch(outFields="*"))
    count = fetch(returnCountOnly="true")
    validate_response(count)
    if type(count.get("count")) is not int or count["count"] != len(rows):
        raise ValueError("ArcGIS count does not match fetched rows; retry next run")
    if not rows:
        # A transient empty feature response must not erase the previous snapshot.
        pause(5)
        if snapshot(fetch(outFields="*")):
            raise ValueError("ArcGIS empty result was not confirmed; retry next run")
        print("Confirmed zero records in layer 5 (not all PG&E outages)")
    else:
        print("Validated {} outage records".format(len(rows)))
    return rows


def diff_rows(previous, current):
    previous, current = index_rows(previous), index_rows(current)
    if previous and current:
        return compare(previous, current)
    # csv-diff 1.2 assumes both sides contain a row. An empty array has no
    # inferred columns: report row additions/removals, not schema changes.
    return {
        "added": list(current.values()), "removed": list(previous.values()),
        "changed": [], "columns_added": [], "columns_removed": [],
    }


def prepare_update(previous_path, new_path, message_path, fetch=query, pause=time.sleep):
    previous_text = previous_path.read_text()
    previous = json.loads(previous_text)
    current = fetch_snapshot(fetch, pause)
    diff = diff_rows(previous, current)
    message = human_text(diff, key=KEY)
    # Preserve the exact bytes on a semantic no-op (including row reordering).
    new_text = json.dumps(current, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if not message:
        new_text = previous_text
    # No candidate or commit message is written until all validation succeeds.
    message_path.write_text(message + "\n")
    new_path.write_text(new_text)


if __name__ == "__main__":
    try:
        prepare_update(Path("outages.json"), Path("outages-new.json"), Path("message.txt"))
    except Exception as ex:
        print("Outage update failed: {}".format(ex), file=sys.stderr)
        sys.exit(1)
