import importlib.util
import json
import sys

import pytest

from pipeline.lambda_stack import LAMBDA_DIR
from tests.fake_db import FakeConnection

USER = "user-1"
OTHER_USER = "user-2"


@pytest.fixture
def handler(monkeypatch):
    # "lambda" is a Python keyword, so load the module by path; it imports db from there
    if str(LAMBDA_DIR) not in sys.path:
        sys.path.insert(0, str(LAMBDA_DIR))
    spec = importlib.util.spec_from_file_location("handler", LAMBDA_DIR / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    connection = FakeConnection()
    monkeypatch.setattr(module, "get_connection", lambda: connection)
    return module.handler


def call(handler, body, user=USER):
    event = {"body": json.dumps(body)}
    if user:
        event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": user, "email": f"{user}@example.com"}}}}
    response = handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def create(handler, content="hello", user=USER):
    status, body = call(handler, {"runmode": "create_item", "content": content}, user)
    assert status == 200
    return body["item"]


def test_write_runmode(handler):
    assert call(handler, {"runmode": "write"}) == (200, {"message": "writer written"})


def test_create_item(handler):
    item = create(handler, "  hello  ")
    assert item["content"] == "hello"
    assert item["created_by"] == USER
    assert item["id"] >= 1


@pytest.mark.parametrize("content", ["", "   ", None, 5, "x" * 256])
def test_create_item_rejects_bad_content(handler, content):
    status, _ = call(handler, {"runmode": "create_item", "content": content})
    assert status == 400


def test_create_item_requires_user(handler):
    status, _ = call(handler, {"runmode": "create_item", "content": "hi"}, user=None)
    assert status == 401


def test_update_own_item(handler):
    item = create(handler)
    status, body = call(handler, {"runmode": "update_item", "id": item["id"], "content": "changed"})
    assert status == 200
    assert body["item"]["content"] == "changed"


def test_update_other_users_item(handler):
    item = create(handler, user=OTHER_USER)
    status, _ = call(handler, {"runmode": "update_item", "id": item["id"], "content": "changed"})
    assert status == 403


def test_update_missing_item(handler):
    status, _ = call(handler, {"runmode": "update_item", "id": 999, "content": "changed"})
    assert status == 404


@pytest.mark.parametrize("item_id", [None, "1", 0, -1, True])
def test_rejects_bad_id(handler, item_id):
    status, _ = call(handler, {"runmode": "delete_item", "id": item_id})
    assert status == 400


def test_delete_own_item(handler):
    item = create(handler)
    assert call(handler, {"runmode": "delete_item", "id": item["id"]}) == (200, {"deleted": item["id"]})
    status, _ = call(handler, {"runmode": "delete_item", "id": item["id"]})
    assert status == 404


def test_delete_other_users_item(handler):
    item = create(handler, user=OTHER_USER)
    status, _ = call(handler, {"runmode": "delete_item", "id": item["id"]})
    assert status == 403


def test_unknown_runmode(handler):
    status, body = call(handler, {"runmode": "nope"})
    assert status == 400
    assert "create_item" in body["runmodes"]


def test_invalid_body(handler):
    response = handler({"body": "not json"}, None)
    assert response["statusCode"] == 400


# --- accounts and marinas

def register(handler, user=USER):
    status, body = call(handler, {"runmode": "register_user"}, user)
    assert status == 200
    return body


def complete_profile(handler, user=USER):
    return call(handler, {"runmode": "update_profile", "first_name": "Ada", "last_name": "Lovelace"}, user)


def test_register_user(handler):
    body = register(handler)
    assert body["user"]["email"] == f"{USER}@example.com"
    assert body["user"]["profile_completed_at"] is None
    assert body["marina"] is None


def test_register_user_is_idempotent(handler):
    first = register(handler)
    second = register(handler)
    assert first["user"]["id"] == second["user"]["id"]


def test_register_user_requires_email(handler):
    event = {
        "body": json.dumps({"runmode": "register_user"}),
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": USER}}}},
    }
    assert handler(event, None)["statusCode"] == 400


def test_update_profile_completes_setup(handler):
    register(handler)
    status, body = call(
        handler,
        {"runmode": "update_profile", "first_name": " Ada ", "last_name": "Lovelace", "phone": "555-0100"},
    )
    assert status == 200
    assert body["user"]["first_name"] == "Ada"
    assert body["user"]["phone"] == "555-0100"
    assert body["user"]["profile_completed_at"] is not None


def test_update_profile_keeps_first_completion_time(handler):
    register(handler)
    _, first = complete_profile(handler)
    _, second = call(handler, {"runmode": "update_profile", "first_name": "Ada", "last_name": "King"})
    assert second["user"]["profile_completed_at"] == first["user"]["profile_completed_at"]
    assert second["user"]["last_name"] == "King"


def test_update_profile_requires_names(handler):
    register(handler)
    status, body = call(handler, {"runmode": "update_profile", "first_name": "Ada"})
    assert status == 400
    assert "last_name" in body["error"]


def test_update_profile_requires_registration(handler):
    status, _ = complete_profile(handler)
    assert status == 404


def test_create_marina_links_user(handler):
    register(handler)
    status, body = call(handler, {
        "runmode": "create_marina",
        "name": "Harbourfront Marina",
        "city": "Toronto",
        "country": "ca",
        "timezone": "America/Toronto",
        "latitude": 43.6387,
        "longitude": -79.3817,
    })
    assert status == 200
    assert body["marina"]["name"] == "Harbourfront Marina"
    assert body["marina"]["country"] == "CA"
    assert body["marina"]["created_by"] == body["user"]["id"]
    assert body["user"]["marina_id"] == body["marina"]["id"]


def test_create_marina_only_once(handler):
    register(handler)
    assert call(handler, {"runmode": "create_marina", "name": "One"})[0] == 200
    status, _ = call(handler, {"runmode": "create_marina", "name": "Two"})
    assert status == 409


@pytest.mark.parametrize("fields", [
    {},
    {"name": "  "},
    {"name": "M", "country": "CAN"},
    {"name": "M", "timezone": "Mars/Olympus"},
    {"name": "M", "latitude": 91},
    {"name": "M", "longitude": "west"},
    {"name": "x" * 256},
])
def test_create_marina_validation(handler, fields):
    register(handler)
    status, _ = call(handler, {"runmode": "create_marina", **fields})
    assert status == 400


def test_create_marina_requires_registration(handler):
    status, _ = call(handler, {"runmode": "create_marina", "name": "M"})
    assert status == 404


def test_create_marina_rolls_back_on_failure(handler, monkeypatch):
    register(handler)
    connection = handler.__globals__["get_connection"]()
    real_commit = connection.commit

    def failing_commit():
        raise RuntimeError("boom")

    monkeypatch.setattr(connection, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        call(handler, {"runmode": "create_marina", "name": "M"})
    monkeypatch.setattr(connection, "commit", real_commit)

    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM marinas")
        assert cursor.fetchone()["n"] == 0
