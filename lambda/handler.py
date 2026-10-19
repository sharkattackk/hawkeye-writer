import base64
import json
from datetime import date, datetime
from decimal import Decimal


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


# runmode -> function(payload, event). Add new runmodes here.
RUNMODES = {
    "write": write,
    "create_item": create_item,
    "update_item": update_item,
    "delete_item": delete_item,
}


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
