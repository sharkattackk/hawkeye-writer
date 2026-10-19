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
        event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": user}}}}
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
