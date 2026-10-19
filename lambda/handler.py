import base64
import json
from datetime import date, datetime
from decimal import Decimal


from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pymysql

from db import get_connection

MAX_CONTENT_LENGTH = 255  # test_items.content is VARCHAR(255)


def write(payload, event):
    return {"message": "writer written"}


def create_item(payload, event):
    """{"runmode": "create_item", "content": "..."} -> {"item": {...}}"""
    content = _content(payload)
    with get_connection().cursor() as cursor:
        cursor.execute(
            "INSERT INTO test_items (content, created_by) VALUES (%s, %s)",
            (content, user_id(event)),
        )
        return {"item": _get_item(cursor, cursor.lastrowid)}


def update_item(payload, event):
    """{"runmode": "update_item", "id": 1, "content": "..."} -> {"item": {...}}"""
    item_id = _item_id(payload)
    content = _content(payload)
    with get_connection().cursor() as cursor:
        _get_own_item(cursor, item_id, user_id(event))
        cursor.execute("UPDATE test_items SET content = %s WHERE id = %s", (content, item_id))
        return {"item": _get_item(cursor, item_id)}


def delete_item(payload, event):
    """{"runmode": "delete_item", "id": 1} -> {"deleted": 1}"""
    item_id = _item_id(payload)
    with get_connection().cursor() as cursor:
        _get_own_item(cursor, item_id, user_id(event))
        cursor.execute("DELETE FROM test_items WHERE id = %s", (item_id,))
    return {"deleted": item_id}


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


# runmode -> function(payload, event). Add new runmodes here.
RUNMODES = {
    "write": write,
    "create_item": create_item,
    "update_item": update_item,
    "delete_item": delete_item,
    "register_user": register_user,
    "update_profile": update_profile,
    "create_marina": create_marina,
}


USER_COLUMNS = "id, email, first_name, last_name, phone, marina_id, profile_completed_at, created_at"
MARINA_COLUMNS = (
    "id, name, address_line1, address_line2, city, region, postal_code, country, "
    "timezone, latitude, longitude, phone, email, created_by, created_at"
)


def _get_user(cursor, sub):
    cursor.execute(f"SELECT {USER_COLUMNS} FROM users WHERE cognito_sub = %s", (sub,))
    return cursor.fetchone()


def _require_user(cursor, sub):
    user = _get_user(cursor, sub)
    if user is None:
        raise RunmodeError(404, "User not registered")
    return user


def _get_account(cursor, sub):
    user = _get_user(cursor, sub)
    marina = None
    if user and user["marina_id"] is not None:
        cursor.execute(f"SELECT {MARINA_COLUMNS} FROM marinas WHERE id = %s", (user["marina_id"],))
        marina = cursor.fetchone()
    return {"user": user, "marina": marina}


def _claims(event):
    return event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})


def _text(payload, field, max_length, required=False):
    """Optional text field: trimmed, blank -> None."""
    value = payload.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RunmodeError(400, f"{field} is required")
        return None
    if not isinstance(value, str):
        raise RunmodeError(400, f"{field} must be a string")
    value = value.strip()
    if len(value) > max_length:
        raise RunmodeError(400, f"{field} must be at most {max_length} characters")
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


def _content(payload):
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RunmodeError(400, "content must be a non-empty string")
    content = content.strip()
    if len(content) > MAX_CONTENT_LENGTH:
        raise RunmodeError(400, f"content must be at most {MAX_CONTENT_LENGTH} characters")
    return content


def _item_id(payload):
    item_id = payload.get("id")
    if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id < 1:
        raise RunmodeError(400, "id must be a positive integer")
    return item_id


def _get_item(cursor, item_id):
    cursor.execute(
        "SELECT id, content, created_by, created_at, updated_at FROM test_items WHERE id = %s",
        (item_id,),
    )
    return cursor.fetchone()


def _get_own_item(cursor, item_id, owner):
    """Users may only change their own items."""
    item = _get_item(cursor, item_id)
    if item is None:
        raise RunmodeError(404, "Item not found")
    if item["created_by"] != owner:
        raise RunmodeError(403, "You can only change your own items")
    return item


def handler(event, context):
    """Entry point. Expects a JSON body like {"runmode": "write", ...}."""
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
