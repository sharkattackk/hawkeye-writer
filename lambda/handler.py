import base64
import json


def write(payload, event):
    return {"message": "writer wrote"}


# runmode -> function(payload, event). Add new runmodes here.
RUNMODES = {
    "write": write,
}


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

    return _response(200, run(payload, event))


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
        "body": json.dumps(body),
    }
