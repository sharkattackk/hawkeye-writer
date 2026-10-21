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


def test_unknown_runmode(handler):
    status, body = call(handler, {"runmode": "nope"})
    assert status == 400
    assert "register_user" in body["runmodes"]


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


# --- calibration

class FakeStorage:
    """Stands in for the storage module: records uploads the 'browser' made."""

    def __init__(self):
        self.objects = {}  # key -> {"size", "content_type"}

    def presigned_upload(self, key, content_type, max_bytes):
        return {"url": "https://bucket.example/", "fields": {"key": key, "Content-Type": content_type}}

    def head(self, key):
        return self.objects.get(key)


@pytest.fixture
def s3(handler, monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(handler.__globals__["storage"], "presigned_upload", fake.presigned_upload)
    monkeypatch.setattr(handler.__globals__["storage"], "head", fake.head)
    return fake


SQUARE = [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]]


def setup_marina(handler, user=USER):
    register(handler, user)
    assert call(handler, {"runmode": "create_marina", "name": "Harbourfront"}, user)[0] == 200


def upload_reference(handler, s3, user=USER, content_type="image/jpeg"):
    status, body = call(handler, {"runmode": "create_image_upload", "content_type": content_type, "file_size": 1234}, user)
    assert status == 200
    key = body["upload"]["fields"]["key"]
    s3.objects[key] = {"size": 1234, "content_type": content_type}
    status, body = call(handler, {"runmode": "confirm_image", "id": body["image_id"], "width_px": 4000, "height_px": 3000}, user)
    assert status == 200
    return body["image"]


def save(handler, slips, user=USER):
    return call(handler, {"runmode": "save_slips", "slips": slips}, user)


def test_create_image_upload(handler, s3):
    setup_marina(handler)
    status, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/png", "file_size": 5000})
    assert status == 200
    key = body["upload"]["fields"]["key"]
    assert key.startswith("marinas/1/images/") and key.endswith(".png")


@pytest.mark.parametrize("fields", [
    {"content_type": "image/gif", "file_size": 10},
    {"content_type": "image/jpeg", "file_size": 0},
    {"content_type": "image/jpeg", "file_size": 101 * 1024 * 1024},
    {"content_type": "image/jpeg"},
])
def test_create_image_upload_validation(handler, s3, fields):
    setup_marina(handler)
    assert call(handler, {"runmode": "create_image_upload", **fields})[0] == 400


def test_create_image_upload_requires_marina(handler, s3):
    register(handler)
    status, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/jpeg", "file_size": 10})
    assert status == 409


def test_confirm_image_sets_reference(handler, s3):
    setup_marina(handler)
    image = upload_reference(handler, s3)
    assert image["status"] == "uploaded"
    assert (image["width_px"], image["height_px"]) == (4000, 3000)
    assert register(handler)["marina"]["reference_image_id"] == image["id"]


def test_confirm_image_before_upload(handler, s3):
    setup_marina(handler)
    _, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/jpeg", "file_size": 10})
    status, body = call(handler, {"runmode": "confirm_image", "id": body["image_id"], "width_px": 10, "height_px": 10})
    assert status == 400
    assert "uploaded" in body["error"]


def test_confirm_image_wrong_type(handler, s3):
    setup_marina(handler)
    _, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/jpeg", "file_size": 10})
    s3.objects[body["upload"]["fields"]["key"]] = {"size": 10, "content_type": "text/html"}
    status, _ = call(handler, {"runmode": "confirm_image", "id": body["image_id"], "width_px": 10, "height_px": 10})
    assert status == 400


def test_confirm_other_marinas_image(handler, s3):
    setup_marina(handler)
    image = upload_reference(handler, s3)
    setup_marina(handler, OTHER_USER)
    status, _ = call(handler, {"runmode": "confirm_image", "id": image["id"], "width_px": 1, "height_px": 1}, OTHER_USER)
    assert status == 404


def test_cannot_replace_reference_once_slips_exist(handler, s3):
    setup_marina(handler)
    upload_reference(handler, s3)
    assert save(handler, [{"label": "A1", "polygon": SQUARE}])[0] == 200
    _, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/jpeg", "file_size": 10})
    s3.objects[body["upload"]["fields"]["key"]] = {"size": 10, "content_type": "image/jpeg"}
    status, _ = call(handler, {"runmode": "confirm_image", "id": body["image_id"], "width_px": 10, "height_px": 10})
    assert status == 409


def test_save_slips_creates_with_docks(handler, s3):
    setup_marina(handler)
    upload_reference(handler, s3)
    status, body = save(handler, [
        {"label": "A1", "dock": "A Dock", "polygon": SQUARE, "length_ft": 30},
        {"label": "A2", "dock": "A Dock", "polygon": [[0.2, 0.1], [0.3, 0.1], [0.3, 0.2], [0.2, 0.2]], "kind": "t_head"},
    ])
    assert status == 200
    a1, a2 = body["slips"]
    assert a1["label"] == "A1" and a1["dock"] == "A Dock" and a1["polygon"] == SQUARE
    assert float(a1["center_x"]) == pytest.approx(0.15) and float(a1["center_y"]) == pytest.approx(0.15)
    assert float(a1["length_ft"]) == 30
    assert a2["kind"] == "t_head"


def test_save_slips_updates_deletes_and_swaps_labels(handler, s3):
    setup_marina(handler)
    upload_reference(handler, s3)
    _, body = save(handler, [
        {"label": "A1", "dock": "A Dock", "polygon": SQUARE},
        {"label": "A2", "dock": "A Dock", "polygon": SQUARE},
        {"label": "B1", "dock": "B Dock", "polygon": SQUARE},
    ])
    ids = {slip["label"]: slip["id"] for slip in body["slips"]}

    # Swap A1/A2, drop B1 (and with it B Dock), add C1
    status, body = save(handler, [
        {"id": ids["A1"], "label": "A2", "dock": "A Dock", "polygon": SQUARE},
        {"id": ids["A2"], "label": "A1", "dock": "A Dock", "polygon": SQUARE},
        {"label": "C1", "polygon": SQUARE},
    ])
    assert status == 200
    by_label = {slip["label"]: slip for slip in body["slips"]}
    assert set(by_label) == {"A1", "A2", "C1"}
    assert by_label["A2"]["id"] == ids["A1"] and by_label["A1"]["id"] == ids["A2"]
    assert by_label["C1"]["dock"] is None

    connection = handler.__globals__["get_connection"]()
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM docks")
        assert [row["name"] for row in cursor.fetchall()] == ["A Dock"]


def test_save_empty_list_clears_slips(handler, s3):
    setup_marina(handler)
    upload_reference(handler, s3)
    save(handler, [{"label": "A1", "polygon": SQUARE}])
    assert save(handler, []) == (200, {"slips": []})


@pytest.mark.parametrize("slips", [
    "nope",
    [{"polygon": SQUARE}],
    [{"label": "A1", "polygon": SQUARE[:3]}],
    [{"label": "A1", "polygon": [[0.1, 0.1], [1.2, 0.1], [0.2, 0.2], [0.1, 0.2]]}],
    [{"label": "A1", "polygon": [[0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]]}],
    [{"label": "A1", "polygon": SQUARE, "kind": "boathouse"}],
    [{"label": "A1", "polygon": SQUARE, "length_ft": -5}],
    [{"label": "A1", "polygon": SQUARE}, {"label": "a1", "polygon": SQUARE}],
    [{"id": 999, "label": "A1", "polygon": SQUARE}],
])
def test_save_slips_validation(handler, s3, slips):
    setup_marina(handler)
    upload_reference(handler, s3)
    assert save(handler, slips)[0] == 400


def test_save_slips_requires_reference_image(handler, s3):
    setup_marina(handler)
    assert save(handler, [{"label": "A1", "polygon": SQUARE}])[0] == 409


def test_cannot_edit_other_marinas_slips(handler, s3):
    setup_marina(handler)
    upload_reference(handler, s3)
    _, body = save(handler, [{"label": "A1", "polygon": SQUARE}])
    setup_marina(handler, OTHER_USER)
    upload_reference(handler, s3, OTHER_USER)
    status, _ = save(handler, [{"id": body["slips"][0]["id"], "label": "X", "polygon": SQUARE}], OTHER_USER)
    assert status == 400


def test_finish_calibration(handler, s3):
    setup_marina(handler)
    assert call(handler, {"runmode": "finish_calibration"})[0] == 409  # no image
    upload_reference(handler, s3)
    assert call(handler, {"runmode": "finish_calibration"})[0] == 409  # no slips
    save(handler, [{"label": "A1", "polygon": SQUARE}])
    status, body = call(handler, {"runmode": "finish_calibration"})
    assert status == 200
    assert body["marina"]["calibrated_at"] is not None


# --- drone updates

def calibrate(handler, s3, user=USER):
    setup_marina(handler, user)
    upload_reference(handler, s3, user)
    save(handler, [{"label": "A1", "polygon": SQUARE}], user)
    assert call(handler, {"runmode": "finish_calibration"}, user)[0] == 200


def upload_update(handler, s3, user=USER):
    _, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/jpeg", "file_size": 99}, user)
    s3.objects[body["upload"]["fields"]["key"]] = {"size": 99, "content_type": "image/jpeg"}
    return call(
        handler,
        {"runmode": "confirm_image", "id": body["image_id"], "width_px": 4000, "height_px": 3000, "set_reference": False},
        user,
    )


def test_update_queues_analysis_job(handler, s3):
    calibrate(handler, s3)
    status, body = upload_update(handler, s3)
    assert status == 200
    assert body["job"]["status"] == "pending"
    assert body["job"]["image_id"] == body["image"]["id"]
    # the reference image is unchanged
    assert register(handler)["marina"]["reference_image_id"] != body["image"]["id"]


def test_confirming_update_twice_queues_once(handler, s3):
    calibrate(handler, s3)
    _, body = upload_update(handler, s3)
    image_id = body["image"]["id"]
    again = call(handler, {"runmode": "confirm_image", "id": image_id, "width_px": 1, "height_px": 1, "set_reference": False})
    assert again[0] == 200 and again[1]["job"]["id"] == body["job"]["id"]
    connection = handler.__globals__["get_connection"]()
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM analysis_jobs")
        assert cursor.fetchone()["n"] == 1


def test_update_requires_calibration(handler, s3):
    setup_marina(handler)
    upload_reference(handler, s3)
    status, body = upload_update(handler, s3)
    assert status == 409
    assert "Calibrate" in body["error"]


def test_reference_upload_has_no_job(handler, s3):
    setup_marina(handler)
    _, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/jpeg", "file_size": 9})
    s3.objects[body["upload"]["fields"]["key"]] = {"size": 9, "content_type": "image/jpeg"}
    status, body = call(handler, {"runmode": "confirm_image", "id": body["image_id"], "width_px": 1, "height_px": 1})
    assert status == 200 and body["job"] is None


def test_retry_failed_analysis(handler, s3):
    calibrate(handler, s3)
    _, body = upload_update(handler, s3)
    image_id = body["image"]["id"]
    assert call(handler, {"runmode": "retry_analysis", "image_id": image_id})[0] == 409  # still pending

    connection = handler.__globals__["get_connection"]()
    with connection.cursor() as cursor:
        cursor.execute("UPDATE analysis_jobs SET status = 'failed', message = 'boom', progress = 40")
    status, body = call(handler, {"runmode": "retry_analysis", "image_id": image_id})
    assert status == 200
    assert body["job"]["status"] == "pending" and body["job"]["message"] is None


def test_retry_other_marinas_job(handler, s3):
    calibrate(handler, s3)
    _, body = upload_update(handler, s3)
    setup_marina(handler, OTHER_USER)
    status, _ = call(handler, {"runmode": "retry_analysis", "image_id": body["image"]["id"]}, OTHER_USER)
    assert status == 404
