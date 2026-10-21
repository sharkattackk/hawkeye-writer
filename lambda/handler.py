import base64
import json
import uuid
from datetime import date, datetime
from decimal import Decimal


from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pymysql

import storage
from db import get_connection

def register_user(payload, event):
    """Create the caller's users row on first login (safe to repeat) -> {"user", "marina"}"""
    sub = user_id(event)
    email = _claims(event).get("email")
    if not email:
        raise RunmodeError(400, "Login token has no email")

    connection = get_connection()
    with connection.cursor() as cursor:
        if _get_user(cursor, sub) is None:
            try:
                cursor.execute("INSERT INTO users (cognito_sub, email) VALUES (%s, %s)", (sub, email))
            except pymysql.err.IntegrityError:
                pass  # registered by a concurrent request
        return _get_account(cursor, sub)


def update_profile(payload, event):
    """{"first_name", "last_name", "phone"?} -> {"user", "marina"}. Completes profile setup."""
    first_name = _text(payload, "first_name", 100, required=True)
    last_name = _text(payload, "last_name", 100, required=True)
    phone = _text(payload, "phone", 32)

    sub = user_id(event)
    with get_connection().cursor() as cursor:
        _require_user(cursor, sub)
        cursor.execute(
            "UPDATE users SET first_name = %s, last_name = %s, phone = %s, "
            "profile_completed_at = COALESCE(profile_completed_at, CURRENT_TIMESTAMP) "
            "WHERE cognito_sub = %s",
            (first_name, last_name, phone, sub),
        )
        return _get_account(cursor, sub)


def create_marina(payload, event):
    """{"name", ...address/contact fields} -> {"user", "marina"}. Links the marina to the caller."""
    marina = {
        "name": _text(payload, "name", 255, required=True),
        "address_line1": _text(payload, "address_line1", 255),
        "address_line2": _text(payload, "address_line2", 255),
        "city": _text(payload, "city", 100),
        "region": _text(payload, "region", 100),
        "postal_code": _text(payload, "postal_code", 20),
        "country": _country(payload),
        "timezone": _timezone(payload),
        "latitude": _coordinate(payload, "latitude", 90),
        "longitude": _coordinate(payload, "longitude", 180),
        "phone": _text(payload, "phone", 32),
        "email": _text(payload, "email", 255),
    }

    sub = user_id(event)
    connection = get_connection()
    with connection.cursor() as cursor:
        user = _require_user(cursor, sub)
        if user["marina_id"] is not None:
            raise RunmodeError(409, "Marina already configured")

        # Create the marina and link it in one transaction, so no marina is left orphaned
        connection.begin()
        try:
            columns = ", ".join([*marina, "created_by"])
            placeholders = ", ".join(["%s"] * (len(marina) + 1))
            cursor.execute(
                f"INSERT INTO marinas ({columns}) VALUES ({placeholders})",
                (*marina.values(), user["id"]),
            )
            cursor.execute(
                "UPDATE users SET marina_id = %s WHERE id = %s AND marina_id IS NULL",
                (cursor.lastrowid, user["id"]),
            )
            if cursor.rowcount != 1:
                raise RunmodeError(409, "Marina already configured")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        return _get_account(cursor, sub)


IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_IMAGE_BYTES = 100 * 1024 * 1024
SLIP_KINDS = ("slip", "t_head", "side_tie", "mooring")
MAX_SLIPS = 2000


def create_image_upload(payload, event):
    """{"content_type", "file_size"} -> {"image_id", "upload": {"url", "fields"}}

    The browser POSTs the file to upload.url with upload.fields, then calls confirm_image.
    """
    content_type = payload.get("content_type")
    if content_type not in IMAGE_TYPES:
        raise RunmodeError(400, f"content_type must be one of {', '.join(IMAGE_TYPES)}")
    file_size = _positive_int(payload, "file_size")
    if file_size > MAX_IMAGE_BYTES:
        raise RunmodeError(400, f"Images must be at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB")

    with get_connection().cursor() as cursor:
        user = _require_marina_user(cursor, user_id(event))
        key = f"marinas/{user['marina_id']}/images/{uuid.uuid4().hex}.{IMAGE_TYPES[content_type]}"
        cursor.execute(
            "INSERT INTO marina_images (marina_id, s3_key, content_type, file_size_bytes, uploaded_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user["marina_id"], key, content_type, file_size, user["id"]),
        )
        image_id = cursor.lastrowid

    # S3 rejects anything other than this type, or larger than the declared size
    return {"image_id": image_id, "upload": storage.presigned_upload(key, content_type, file_size)}


def confirm_image(payload, event):
    """{"id", "width_px", "height_px", "set_reference"?: true} -> {"image", "job"}

    Checks the file reached S3 and records its size and dimensions. Then either:
    - set_reference true (default): makes it the marina's reference image, the
      coordinate frame for slip polygons (calibration); job is None.
    - set_reference false: treats it as a drone update of a calibrated marina and
      queues an analysis job for hawkeye-worker; job is the new job.
    """
    image_id = _positive_int(payload, "id")
    width_px = _positive_int(payload, "width_px")
    height_px = _positive_int(payload, "height_px")
    set_reference = payload.get("set_reference", True) is not False

    connection = get_connection()
    with connection.cursor() as cursor:
        user = _require_marina_user(cursor, user_id(event))
        marina_id = user["marina_id"]
        image = _get_image(cursor, image_id, marina_id)
        marina = _get_marina(cursor, marina_id)

        if set_reference:
            if marina["reference_image_id"] not in (None, image_id) and _slip_count(cursor, marina_id):
                # Slip polygons are relative to the current reference image
                raise RunmodeError(409, "Slips are already labelled on another reference image")
        else:
            if marina["calibrated_at"] is None:
                raise RunmodeError(409, "Calibrate your marina before uploading drone updates")
            if image_id == marina["reference_image_id"]:
                raise RunmodeError(409, "That is the reference image")

        uploaded = storage.head(image["s3_key"])
        if uploaded is None:
            raise RunmodeError(400, "The file hasn't been uploaded yet")
        if uploaded["content_type"] != image["content_type"]:
            cursor.execute("UPDATE marina_images SET status = 'failed' WHERE id = %s", (image_id,))
            raise RunmodeError(400, "Uploaded file type doesn't match")

        connection.begin()
        try:
            cursor.execute(
                "UPDATE marina_images SET status = 'uploaded', file_size_bytes = %s, width_px = %s, height_px = %s "
                "WHERE id = %s",
                (uploaded["size"], width_px, height_px, image_id),
            )
            if set_reference:
                cursor.execute("UPDATE marinas SET reference_image_id = %s WHERE id = %s", (image_id, marina_id))
            elif _get_job(cursor, image_id) is None:  # confirming twice must not queue twice
                cursor.execute(
                    "INSERT INTO analysis_jobs (marina_id, image_id) VALUES (%s, %s)", (marina_id, image_id)
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        return {
            "image": _get_image(cursor, image_id, marina_id),
            "job": None if set_reference else _get_job(cursor, image_id),
        }


def retry_analysis(payload, event):
    """{"image_id"} -> {"job"}. Puts a failed analysis job back in the queue."""
    image_id = _positive_int(payload, "image_id")
    with get_connection().cursor() as cursor:
        user = _require_marina_user(cursor, user_id(event))
        _get_image(cursor, image_id, user["marina_id"])  # 404 unless it's this marina's
        job = _get_job(cursor, image_id)
        if job is None:
            raise RunmodeError(404, "No analysis job for that image")
        if job["status"] != "failed":
            raise RunmodeError(409, f"Only failed jobs can be retried (this one is {job['status']})")
        cursor.execute(
            "UPDATE analysis_jobs SET status = 'pending', progress = NULL, message = NULL, worker_id = NULL, "
            "queued_at = CURRENT_TIMESTAMP, started_at = NULL, heartbeat_at = NULL, finished_at = NULL "
            "WHERE id = %s",
            (job["id"],),
        )
        return {"job": _get_job(cursor, image_id)}


def save_slips(payload, event):
    """{"slips": [{"id"?, "label", "polygon", "dock"?, "kind"?, "length_ft"?, "width_ft"?}]} -> {"slips"}

    Saves the marina's whole slip layout: slips with an id are updated, slips without
    one are created, and existing slips missing from the list are deleted.
    Polygons are 4 [x, y] corners, normalized 0-1 from the reference image's top-left.
    """
    slips = payload.get("slips")
    if not isinstance(slips, list):
        raise RunmodeError(400, "slips must be a list")
    if len(slips) > MAX_SLIPS:
        raise RunmodeError(400, f"At most {MAX_SLIPS} slips")
    slips = [_parse_slip(slip, i) for i, slip in enumerate(slips)]

    labels = [slip["label"].lower() for slip in slips]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise RunmodeError(400, f"Duplicate slip labels: {', '.join(duplicates)}")

    connection = get_connection()
    with connection.cursor() as cursor:
        user = _require_marina_user(cursor, user_id(event))
        marina_id = user["marina_id"]
        if _get_marina(cursor, marina_id)["reference_image_id"] is None:
            raise RunmodeError(409, "Upload a reference image first")

        cursor.execute("SELECT id FROM slips WHERE marina_id = %s", (marina_id,))
        existing_ids = {row["id"] for row in cursor.fetchall()}
        kept_ids = [slip["id"] for slip in slips if slip["id"] is not None]
        unknown = sorted(set(kept_ids) - existing_ids)
        if unknown:
            raise RunmodeError(400, f"Unknown slip ids: {', '.join(map(str, unknown))}")

        connection.begin()
        try:
            _delete_slips_except(cursor, marina_id, kept_ids)
            dock_ids = _ensure_docks(cursor, marina_id, slips)

            # Park kept labels first, so swapping two labels can't hit the unique key
            for slip_id in kept_ids:
                cursor.execute("UPDATE slips SET label = %s WHERE id = %s", (f"~{slip_id}", slip_id))

            for slip in slips:
                values = (
                    dock_ids.get(slip["dock"]),
                    slip["label"],
                    slip["kind"],
                    json.dumps(slip["polygon"]),
                    slip["center_x"],
                    slip["center_y"],
                    slip["length_ft"],
                    slip["width_ft"],
                )
                if slip["id"] is None:
                    cursor.execute(
                        "INSERT INTO slips (dock_id, label, kind, polygon, center_x, center_y, length_ft, width_ft, "
                        "marina_id, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (*values, marina_id, user["id"]),
                    )
                else:
                    cursor.execute(
                        "UPDATE slips SET dock_id = %s, label = %s, kind = %s, polygon = %s, center_x = %s, "
                        "center_y = %s, length_ft = %s, width_ft = %s WHERE id = %s",
                        (*values, slip["id"]),
                    )

            # Remove docks no slip uses any more
            cursor.execute(
                "DELETE FROM docks WHERE marina_id = %s AND id NOT IN "
                "(SELECT dock_id FROM slips WHERE marina_id = %s AND dock_id IS NOT NULL)",
                (marina_id, marina_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        return {"slips": _list_slips(cursor, marina_id)}


def finish_calibration(payload, event):
    """Marks the marina calibrated (needs a reference image and at least one slip) -> {"user", "marina"}"""
    sub = user_id(event)
    with get_connection().cursor() as cursor:
        user = _require_marina_user(cursor, sub)
        marina = _get_marina(cursor, user["marina_id"])
        if marina["reference_image_id"] is None:
            raise RunmodeError(409, "Upload a reference image first")
        if not _slip_count(cursor, marina["id"]):
            raise RunmodeError(409, "Label at least one slip first")
        cursor.execute(
            "UPDATE marinas SET calibrated_at = COALESCE(calibrated_at, CURRENT_TIMESTAMP) WHERE id = %s",
            (marina["id"],),
        )
        return _get_account(cursor, sub)


LABELS = ("boat", "empty", "unclear")


def label_slip(payload, event):
    """{"image_id", "slip_id", "label": "boat" | "empty" | "unclear"} -> {"status", "labels"}

    A person's answer to "is this correct?" for one slip in one analyzed photo. Saved as
    a training example (with the crop the model saw and its original guess), and a
    boat/empty answer also corrects that photo's result. Answering again changes it.
    """
    image_id = _positive_int(payload, "image_id")
    slip_id = _positive_int(payload, "slip_id")
    label = payload.get("label")
    if label not in LABELS:
        raise RunmodeError(400, f"label must be one of {', '.join(LABELS)}")

    connection = get_connection()
    with connection.cursor() as cursor:
        user = _require_marina_user(cursor, user_id(event))
        cursor.execute(
            "SELECT o.id, o.occupied, o.confidence, o.model_version, o.crop_s3_key FROM slip_observations o "
            "JOIN marina_images i ON i.id = o.image_id "
            "WHERE o.image_id = %s AND o.slip_id = %s AND i.marina_id = %s",
            (image_id, slip_id, user["marina_id"]),
        )
        observation = cursor.fetchone()
        if observation is None:
            raise RunmodeError(404, "No result for that slip in that photo")

        cursor.execute("SELECT id FROM slip_labels WHERE image_id = %s AND slip_id = %s", (image_id, slip_id))
        existing = cursor.fetchone()

        connection.begin()
        try:
            if existing:
                # Keep the model's original guess from the first answer
                cursor.execute(
                    "UPDATE slip_labels SET label = %s, labeled_by = %s WHERE id = %s",
                    (label, user["id"], existing["id"]),
                )
            else:
                cursor.execute(
                    "INSERT INTO slip_labels (marina_id, image_id, slip_id, label, crop_s3_key, predicted_occupied, "
                    "predicted_confidence, model_version, labeled_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        user["marina_id"], image_id, slip_id, label, observation["crop_s3_key"],
                        observation["occupied"], observation["confidence"], observation["model_version"], user["id"],
                    ),
                )
            if label != "unclear":
                cursor.execute(
                    "UPDATE slip_observations SET occupied = %s, confidence = 1.0 WHERE id = %s",
                    (label == "boat", observation["id"]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        cursor.execute("SELECT occupied, confidence FROM slip_observations WHERE id = %s", (observation["id"],))
        status = cursor.fetchone()
        return {
            "status": {"occupied": bool(status["occupied"]), "confidence": status["confidence"], "label": label},
            "labels": label_stats(cursor, user["marina_id"]),
        }


def label_stats(cursor, marina_id):
    """How big the training set is, and how often the model's guess matched the answer."""
    cursor.execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN label <> 'unclear' THEN 1 ELSE 0 END), 0) AS decided, "
        "COALESCE(SUM(CASE WHEN label <> 'unclear' AND predicted_occupied = (label = 'boat') THEN 1 ELSE 0 END), 0) AS agreed "
        "FROM slip_labels WHERE marina_id = %s",
        (marina_id,),
    )
    row = cursor.fetchone()
    decided = int(row["decided"])
    return {
        "total": int(row["total"]),
        "decided": decided,
        "agreed": int(row["agreed"]),
        "accuracy": round(int(row["agreed"]) / decided, 3) if decided else None,
    }


# runmode -> function(payload, event). Add new runmodes here.
RUNMODES = {
    "register_user": register_user,
    "update_profile": update_profile,
    "create_marina": create_marina,
    "create_image_upload": create_image_upload,
    "confirm_image": confirm_image,
    "save_slips": save_slips,
    "finish_calibration": finish_calibration,
    "retry_analysis": retry_analysis,
    "label_slip": label_slip,
}


USER_COLUMNS = "id, email, first_name, last_name, phone, marina_id, profile_completed_at, created_at"
MARINA_COLUMNS = (
    "id, name, address_line1, address_line2, city, region, postal_code, country, "
    "timezone, latitude, longitude, phone, email, created_by, created_at, reference_image_id, calibrated_at"
)
IMAGE_COLUMNS = "id, marina_id, s3_key, content_type, file_size_bytes, width_px, height_px, status, created_at"
SLIP_COLUMNS = (
    "s.id, s.label, s.kind, s.polygon, s.center_x, s.center_y, s.length_ft, s.width_ft, d.name AS dock"
)


def _get_user(cursor, sub):
    cursor.execute(f"SELECT {USER_COLUMNS} FROM users WHERE cognito_sub = %s", (sub,))
    return cursor.fetchone()


def _require_user(cursor, sub):
    user = _get_user(cursor, sub)
    if user is None:
        raise RunmodeError(404, "User not registered")
    return user


def _require_marina_user(cursor, sub):
    user = _require_user(cursor, sub)
    if user["marina_id"] is None:
        raise RunmodeError(409, "Configure your marina first")
    return user


def _get_marina(cursor, marina_id):
    cursor.execute(f"SELECT {MARINA_COLUMNS} FROM marinas WHERE id = %s", (marina_id,))
    return cursor.fetchone()


def _get_image(cursor, image_id, marina_id):
    cursor.execute(
        f"SELECT {IMAGE_COLUMNS} FROM marina_images WHERE id = %s AND marina_id = %s", (image_id, marina_id)
    )
    image = cursor.fetchone()
    if image is None:
        raise RunmodeError(404, "Image not found")
    return image


JOB_COLUMNS = "id, image_id, status, progress, message, attempts, queued_at, started_at, finished_at"


def _get_job(cursor, image_id):
    cursor.execute(f"SELECT {JOB_COLUMNS} FROM analysis_jobs WHERE image_id = %s", (image_id,))
    return cursor.fetchone()


def _slip_count(cursor, marina_id):
    cursor.execute("SELECT COUNT(*) AS n FROM slips WHERE marina_id = %s", (marina_id,))
    return cursor.fetchone()["n"]


def _list_slips(cursor, marina_id):
    cursor.execute(
        f"SELECT {SLIP_COLUMNS} FROM slips s LEFT JOIN docks d ON d.id = s.dock_id "
        "WHERE s.marina_id = %s ORDER BY s.label",
        (marina_id,),
    )
    slips = cursor.fetchall()
    for slip in slips:
        if isinstance(slip["polygon"], str):  # pymysql returns JSON columns as text
            slip["polygon"] = json.loads(slip["polygon"])
    return slips


def _delete_slips_except(cursor, marina_id, kept_ids):
    if kept_ids:
        placeholders = ", ".join(["%s"] * len(kept_ids))
        cursor.execute(
            f"DELETE FROM slips WHERE marina_id = %s AND id NOT IN ({placeholders})", (marina_id, *kept_ids)
        )
    else:
        cursor.execute("DELETE FROM slips WHERE marina_id = %s", (marina_id,))


def _ensure_docks(cursor, marina_id, slips):
    """Dock name -> id, creating docks in the order they first appear."""
    cursor.execute("SELECT id, name FROM docks WHERE marina_id = %s", (marina_id,))
    dock_ids = {row["name"].lower(): row["id"] for row in cursor.fetchall()}
    result = {}
    for order, name in enumerate(dict.fromkeys(slip["dock"] for slip in slips if slip["dock"])):
        if name.lower() not in dock_ids:
            cursor.execute(
                "INSERT INTO docks (marina_id, name, sort_order) VALUES (%s, %s, %s)", (marina_id, name, order)
            )
            dock_ids[name.lower()] = cursor.lastrowid
        result[name] = dock_ids[name.lower()]
    return result


def _parse_slip(slip, index):
    if not isinstance(slip, dict):
        raise RunmodeError(400, f"slips[{index}] must be an object")
    where = f"slips[{index}]"

    slip_id = slip.get("id")
    if slip_id is not None and (not isinstance(slip_id, int) or isinstance(slip_id, bool) or slip_id < 1):
        raise RunmodeError(400, f"{where}.id must be a positive integer")

    kind = slip.get("kind") or "slip"
    if kind not in SLIP_KINDS:
        raise RunmodeError(400, f"{where}.kind must be one of {', '.join(SLIP_KINDS)}")

    polygon = slip.get("polygon")
    if not (isinstance(polygon, list) and len(polygon) == 4):
        raise RunmodeError(400, f"{where}.polygon must have exactly 4 corners")
    corners = []
    for point in polygon:
        if not (
            isinstance(point, list)
            and len(point) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in point)
        ):
            raise RunmodeError(400, f"{where}.polygon corners must be [x, y] with values from 0 to 1")
        corners.append([round(float(point[0]), 6), round(float(point[1]), 6)])

    return {
        "id": slip_id,
        "label": _text(slip, "label", 32, required=True, where=where),
        "dock": _text(slip, "dock", 64, where=where),
        "kind": kind,
        "polygon": corners,
        "center_x": round(sum(x for x, _ in corners) / 4, 6),
        "center_y": round(sum(y for _, y in corners) / 4, 6),
        "length_ft": _feet(slip, "length_ft", where),
        "width_ft": _feet(slip, "width_ft", where),
    }


def _feet(payload, field, where):
    value = payload.get(field)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 10000:
        raise RunmodeError(400, f"{where}.{field} must be a number of feet")
    return round(float(value), 2)


def _positive_int(payload, field):
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RunmodeError(400, f"{field} must be a positive integer")
    return value


def _get_account(cursor, sub):
    user = _get_user(cursor, sub)
    marina = None
    if user and user["marina_id"] is not None:
        cursor.execute(f"SELECT {MARINA_COLUMNS} FROM marinas WHERE id = %s", (user["marina_id"],))
        marina = cursor.fetchone()
    return {"user": user, "marina": marina}


def _claims(event):
    return event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})


def _text(payload, field, max_length, required=False, where=None):
    """Optional text field: trimmed, blank -> None."""
    name = f"{where}.{field}" if where else field
    value = payload.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RunmodeError(400, f"{name} is required")
        return None
    if not isinstance(value, str):
        raise RunmodeError(400, f"{name} must be a string")
    value = value.strip()
    if len(value) > max_length:
        raise RunmodeError(400, f"{name} must be at most {max_length} characters")
    return value


def _country(payload):
    country = _text(payload, "country", 2)
    if country is None:
        return None
    if not (len(country) == 2 and country.isalpha()):
        raise RunmodeError(400, "country must be a 2-letter code, e.g. CA")
    return country.upper()


def _timezone(payload):
    timezone = _text(payload, "timezone", 64)
    if timezone is None:
        return None
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise RunmodeError(400, f"Unknown timezone '{timezone}'") from None
    return timezone


def _coordinate(payload, field, limit):
    value = payload.get(field)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -limit <= value <= limit:
        raise RunmodeError(400, f"{field} must be a number between -{limit} and {limit}")
    return round(float(value), 6)


def handler(event, context):
    """Entry point. Expects a JSON body like {"runmode": "register_user", ...}."""
    try:
        payload = _parse_body(event)
    except ValueError:
        return _response(400, {"error": "Request body must be a JSON object"})

    runmode = payload.get("runmode")
    run = RUNMODES.get(runmode)
    if run is None:
        return _response(400, {"error": f"Unknown runmode '{runmode}'", "runmodes": sorted(RUNMODES)})

    try:
        return _response(200, run(payload, event))
    except RunmodeError as e:
        return _response(e.status, {"error": e.message})


class RunmodeError(Exception):
    """Raise from a runmode to return an error, e.g. raise RunmodeError(404, "Item not found")."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def user_id(event):
    """Cognito user ID (sub) of the signed-in caller."""
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        raise RunmodeError(401, "No signed-in user") from None


def _parse_body(event):
    # Direct invokes (console, CLI) pass the payload as the event itself
    if "body" not in event:
        return event

    body = event["body"] or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError from e
    if not isinstance(payload, dict):
        raise ValueError
    return payload


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def _json_default(value):
    # MySQL returns datetimes in UTC (the RDS default time zone)
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")
