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
        self.deleted = []

    def presigned_upload(self, key, content_type, max_bytes):
        return {"url": "https://bucket.example/", "fields": {"key": key, "Content-Type": content_type}}

    def head(self, key):
        return self.objects.get(key)

    def delete(self, keys):
        self.deleted.extend(keys)


@pytest.fixture
def s3(handler, monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(handler.__globals__["storage"], "presigned_upload", fake.presigned_upload)
    monkeypatch.setattr(handler.__globals__["storage"], "head", fake.head)
    monkeypatch.setattr(handler.__globals__["storage"], "delete", fake.delete)
    return fake


SQUARE = [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]]


def setup_marina(handler, user=USER):
    register(handler, user)
    assert call(handler, {"runmode": "create_marina", "name": "Harbourfront"}, user)[0] == 200


def uploaded_image(handler, s3, user=USER, content_type="image/jpeg", size=1234):
    """An image the 'browser' has uploaded to S3 but not confirmed yet -> image id."""
    status, body = call(handler, {"runmode": "create_image_upload", "content_type": content_type, "file_size": size}, user)
    assert status == 200
    s3.objects[body["upload"]["fields"]["key"]] = {"size": size, "content_type": content_type}
    return body["image_id"]


def add_reference(handler, s3, name="Main", user=USER):
    return call(handler, {
        "runmode": "create_reference", "image_id": uploaded_image(handler, s3, user),
        "width_px": 4000, "height_px": 3000, "name": name,
    }, user)


def upload_reference(handler, s3, user=USER, name="Main"):
    status, body = add_reference(handler, s3, name, user)
    assert status == 200, body
    return body["reference"]


def save(handler, slips, user=USER, reference_id=None):
    body = {"runmode": "save_slips", "slips": slips}
    if reference_id is not None:
        body["reference_id"] = reference_id
    return call(handler, body, user)


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


def query(handler, sql, params=()):
    with handler.__globals__["get_connection"]().cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def test_create_reference(handler, s3):
    setup_marina(handler)
    reference = upload_reference(handler, s3, name="North basin")
    assert reference["name"] == "North basin"
    assert (reference["width_px"], reference["height_px"]) == (4000, 3000)
    assert reference["slip_count"] == 0
    assert query(handler, "SELECT status FROM marina_images WHERE id = %s", (reference["image_id"],)) == [
        {"status": "uploaded"}
    ]


def test_create_reference_before_upload(handler, s3):
    setup_marina(handler)
    _, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/jpeg", "file_size": 10})
    status, body = call(handler, {
        "runmode": "create_reference", "image_id": body["image_id"], "width_px": 10, "height_px": 10, "name": "Main",
    })
    assert status == 400
    assert "uploaded" in body["error"]


def test_create_reference_wrong_type(handler, s3):
    setup_marina(handler)
    _, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/jpeg", "file_size": 10})
    s3.objects[body["upload"]["fields"]["key"]] = {"size": 10, "content_type": "text/html"}
    status, _ = call(handler, {
        "runmode": "create_reference", "image_id": body["image_id"], "width_px": 10, "height_px": 10, "name": "Main",
    })
    assert status == 400


def test_create_reference_from_other_marinas_image(handler, s3):
    setup_marina(handler)
    image_id = uploaded_image(handler, s3)
    setup_marina(handler, OTHER_USER)
    status, _ = call(handler, {
        "runmode": "create_reference", "image_id": image_id, "width_px": 1, "height_px": 1, "name": "Mine",
    }, OTHER_USER)
    assert status == 404


def test_reference_names_are_unique_and_required(handler, s3):
    setup_marina(handler)
    upload_reference(handler, s3, name="North")
    assert add_reference(handler, s3, "north")[0] == 409
    assert add_reference(handler, s3, "  ")[0] == 400


def test_confirming_a_reference_twice_creates_one(handler, s3):
    setup_marina(handler)
    image_id = uploaded_image(handler, s3)
    body = {"runmode": "create_reference", "image_id": image_id, "width_px": 9, "height_px": 9, "name": "Main"}
    first, second = call(handler, body)[1], call(handler, body)[1]
    assert first["reference"]["id"] == second["reference"]["id"]
    assert len(query(handler, "SELECT id FROM reference_photos")) == 1


def test_reference_thumbnail(handler, s3):
    setup_marina(handler)
    status, body = call(handler, {
        "runmode": "create_image_upload", "content_type": "image/png", "file_size": 5000, "thumbnail_size": 300,
    })
    assert status == 200
    thumb_key = body["thumbnail_upload"]["fields"]["key"]
    assert thumb_key.endswith("-thumb.jpg") and body["thumbnail_upload"]["fields"]["Content-Type"] == "image/jpeg"
    s3.objects[body["upload"]["fields"]["key"]] = {"size": 5000, "content_type": "image/png"}
    s3.objects[thumb_key] = {"size": 300, "content_type": "image/jpeg"}
    call(handler, {"runmode": "create_reference", "image_id": body["image_id"], "width_px": 9, "height_px": 9, "name": "A"})
    assert query(handler, "SELECT thumbnail_s3_key FROM marina_images WHERE id = %s", (body["image_id"],)) == [
        {"thumbnail_s3_key": thumb_key}
    ]

    # A thumbnail that never arrived is dropped; lists use the full photo instead
    _, body = call(handler, {"runmode": "create_image_upload", "content_type": "image/png", "file_size": 5, "thumbnail_size": 3})
    s3.objects[body["upload"]["fields"]["key"]] = {"size": 5, "content_type": "image/png"}
    call(handler, {"runmode": "create_reference", "image_id": body["image_id"], "width_px": 9, "height_px": 9, "name": "B"})
    assert query(handler, "SELECT thumbnail_s3_key FROM marina_images WHERE id = %s", (body["image_id"],)) == [
        {"thumbnail_s3_key": None}
    ]


def test_thumbnail_size_limit(handler, s3):
    setup_marina(handler)
    status, _ = call(handler, {
        "runmode": "create_image_upload", "content_type": "image/png", "file_size": 5000, "thumbnail_size": 3 * 1024 * 1024,
    })
    assert status == 400


def test_rename_reference(handler, s3):
    setup_marina(handler)
    north = upload_reference(handler, s3, name="North")
    upload_reference(handler, s3, name="South")
    status, body = call(handler, {"runmode": "rename_reference", "reference_id": north["id"], "name": "North basin"})
    assert status == 200 and body["reference"]["name"] == "North basin"
    assert call(handler, {"runmode": "rename_reference", "reference_id": north["id"], "name": "south"})[0] == 409
    # renaming to itself (e.g. a case change) is fine
    assert call(handler, {"runmode": "rename_reference", "reference_id": north["id"], "name": "NORTH BASIN"})[0] == 200

    setup_marina(handler, OTHER_USER)
    assert call(handler, {"runmode": "rename_reference", "reference_id": north["id"], "name": "X"}, OTHER_USER)[0] == 404


def test_several_reference_photos_each_keep_their_slips(handler, s3):
    setup_marina(handler)
    north = upload_reference(handler, s3, name="North")
    south = upload_reference(handler, s3, name="South")
    assert save(handler, [{"label": "A1", "polygon": SQUARE}])[0] == 400  # which photo?

    status, body = save(handler, [{"label": "A1", "dock": "A", "polygon": SQUARE}], reference_id=north["id"])
    assert status == 200 and body["slips"][0]["reference_id"] == north["id"]
    status, body = save(handler, [{"label": "B1", "dock": "B", "polygon": SQUARE}], reference_id=south["id"])
    assert status == 200

    # Saving one photo's layout leaves the other's alone
    status, body = save(handler, [{"label": "A2", "dock": "A", "polygon": SQUARE}], reference_id=north["id"])
    assert [s["label"] for s in body["slips"]] == ["A2"]
    assert sorted(row["label"] for row in query(handler, "SELECT label FROM slips")) == ["A2", "B1"]
    assert sorted(row["name"] for row in query(handler, "SELECT name FROM docks")) == ["A", "B"]


def test_slip_labels_are_unique_across_reference_photos(handler, s3):
    setup_marina(handler)
    north = upload_reference(handler, s3, name="North")
    south = upload_reference(handler, s3, name="South")
    save(handler, [{"label": "A1", "polygon": SQUARE}], reference_id=north["id"])
    status, body = save(handler, [{"label": "a1", "polygon": SQUARE}], reference_id=south["id"])
    assert status == 409
    assert "A1 (on 'North')" in body["error"]


def test_cannot_move_a_slip_to_another_reference_photo(handler, s3):
    setup_marina(handler)
    north = upload_reference(handler, s3, name="North")
    south = upload_reference(handler, s3, name="South")
    _, body = save(handler, [{"label": "A1", "polygon": SQUARE}], reference_id=north["id"])
    slip_id = body["slips"][0]["id"]
    assert save(handler, [{"id": slip_id, "label": "A1", "polygon": SQUARE}], reference_id=south["id"])[0] == 400


def analyzed_update(handler, s3, reference_id, slip_label):
    """An update compared with reference_id, analyzed, with crops, step images and a label."""
    _, update = upload_update(handler, s3, reference_id=reference_id)
    job_id, image_id = update["job"]["id"], update["image"]["id"]
    slip_id = query(handler, "SELECT id FROM slips WHERE label = %s", (slip_label,))[0]["id"]
    query(handler, "UPDATE marina_images SET annotated_s3_key = %s WHERE id = %s", (f"annotated/{image_id}.jpg", image_id))
    query(handler, "UPDATE analysis_jobs SET status = 'succeeded' WHERE id = %s", (job_id,))
    query(handler, "INSERT INTO slip_observations (job_id, image_id, slip_id, label, crop_s3_key, occupied, observed_at) "
                   "VALUES (%s, %s, %s, %s, %s, 1, CURRENT_TIMESTAMP)",
          (job_id, image_id, slip_id, slip_label, f"annotated/{image_id}/crops/{slip_id}.jpg"))
    query(handler, "INSERT INTO analysis_job_steps (job_id, attempt, step, status, artifacts) VALUES (%s, 1, 'align', "
                   "'succeeded', %s)", (job_id, json.dumps({"overlay": f"annotated/{image_id}-overlay.jpg", "matches": None})))
    assert label(handler, image_id, slip_id, "boat")[0] == 200
    return image_id


def test_delete_reference_removes_everything_that_depends_on_it(handler, s3):
    calibrate(handler, s3)  # "Main" with A1
    south = upload_reference(handler, s3, name="South")
    save(handler, [{"label": "B1", "dock": "B", "polygon": SQUARE}, {"label": "B2", "polygon": SQUARE}],
         reference_id=south["id"])
    main_id = query(handler, "SELECT id FROM reference_photos WHERE name = 'Main'")[0]["id"]
    kept = analyzed_update(handler, s3, main_id, "A1")
    gone = analyzed_update(handler, s3, south["id"], "B1")
    _, queued = upload_update(handler, s3, reference_id=south["id"])
    south_keys = {row["s3_key"] for row in query(
        handler, "SELECT s3_key FROM marina_images WHERE id IN (%s, %s, %s)", (south["image_id"], gone, queued["image"]["id"]))}

    status, body = call(handler, {"runmode": "delete_reference", "reference_id": south["id"]})
    assert status == 200
    assert body == {"deleted": {"slips": 2, "updates": 2, "labels": 1}, "calibrated": True}
    assert [row["label"] for row in query(handler, "SELECT label FROM slips")] == ["A1"]
    assert [row["name"] for row in query(handler, "SELECT name FROM reference_photos")] == ["Main"]
    assert query(handler, "SELECT name FROM docks") == []  # B was only used by B1
    # Only Main's update is left, with its result, label and step images
    assert [row["image_id"] for row in query(handler, "SELECT image_id FROM analysis_jobs")] == [kept]
    assert [row["image_id"] for row in query(handler, "SELECT image_id FROM slip_observations")] == [kept]
    assert [row["image_id"] for row in query(handler, "SELECT image_id FROM slip_labels")] == [kept]
    assert len(query(handler, "SELECT id FROM analysis_job_steps")) == 1
    remaining = {row["id"] for row in query(handler, "SELECT id FROM marina_images")}
    assert south["image_id"] not in remaining and gone not in remaining and kept in remaining
    # ...and South's photos, annotated copies, crops and step images are deleted from S3
    assert set(s3.deleted) >= south_keys | {f"annotated/{gone}.jpg", f"annotated/{gone}-overlay.jpg"}
    assert any(key.startswith(f"annotated/{gone}/crops/") for key in s3.deleted)
    kept_key = query(handler, "SELECT s3_key FROM marina_images WHERE id = %s", (kept,))[0]["s3_key"]
    assert not any(
        key in (kept_key, f"annotated/{kept}.jpg", f"annotated/{kept}-overlay.jpg") or key.startswith(f"annotated/{kept}/")
        for key in s3.deleted
    )
    assert register(handler)["marina"]["calibrated_at"] is not None


def test_deleting_the_last_reference_resets_calibration(handler, s3):
    calibrate(handler, s3)
    main_id = query(handler, "SELECT id FROM reference_photos")[0]["id"]
    analyzed_update(handler, s3, main_id, "A1")
    status, body = call(handler, {"runmode": "delete_reference", "reference_id": main_id})
    assert status == 200
    assert body == {"deleted": {"slips": 1, "updates": 1, "labels": 1}, "calibrated": False}
    assert register(handler)["marina"]["calibrated_at"] is None
    for table in ("reference_photos", "slips", "docks", "analysis_jobs", "slip_observations", "slip_labels", "marina_images"):
        assert query(handler, f"SELECT * FROM {table}") == [], table
    # back to the start of calibration: a new reference photo can be added and used
    reference = upload_reference(handler, s3, name="Main")
    assert save(handler, [{"label": "A1", "polygon": SQUARE}])[0] == 200
    assert call(handler, {"runmode": "finish_calibration"})[0] == 200
    assert upload_update(handler, s3, reference_id=reference["id"])[0] == 200


def test_cannot_delete_a_reference_while_an_update_is_analyzed_against_it(handler, s3):
    calibrate(handler, s3)
    main_id = query(handler, "SELECT id FROM reference_photos")[0]["id"]
    upload_update(handler, s3)
    query(handler, "UPDATE analysis_jobs SET status = 'running'")
    status, body = call(handler, {"runmode": "delete_reference", "reference_id": main_id})
    assert status == 409 and "being analyzed" in body["error"]
    assert len(query(handler, "SELECT id FROM reference_photos")) == 1


def test_delete_the_only_reference_while_setting_up(handler, s3):
    setup_marina(handler)
    reference = upload_reference(handler, s3)
    save(handler, [{"label": "A1", "polygon": SQUARE}])
    status, body = call(handler, {"runmode": "delete_reference", "reference_id": reference["id"]})
    assert status == 200 and body["calibrated"] is False
    assert query(handler, "SELECT id FROM reference_photos") == []


def test_delete_other_marinas_reference(handler, s3):
    setup_marina(handler)
    reference = upload_reference(handler, s3)
    setup_marina(handler, OTHER_USER)
    assert call(handler, {"runmode": "delete_reference", "reference_id": reference["id"]}, OTHER_USER)[0] == 404


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


def test_save_slips_requires_reference_photo(handler, s3):
    setup_marina(handler)
    assert save(handler, [{"label": "A1", "polygon": SQUARE}])[0] == 409


def test_cannot_edit_other_marinas_slips(handler, s3):
    setup_marina(handler)
    upload_reference(handler, s3)
    _, body = save(handler, [{"label": "A1", "polygon": SQUARE}])
    setup_marina(handler, OTHER_USER)
    other = upload_reference(handler, s3, OTHER_USER)
    status, _ = save(handler, [{"id": body["slips"][0]["id"], "label": "X", "polygon": SQUARE}], OTHER_USER)
    assert status == 400
    # nor save onto another marina's reference photo
    reference_id = query(handler, "SELECT id FROM reference_photos WHERE id <> %s", (other["id"],))[0]["id"]
    assert save(handler, [{"label": "X", "polygon": SQUARE}], OTHER_USER, reference_id)[0] == 404


def test_finish_calibration(handler, s3):
    setup_marina(handler)
    assert call(handler, {"runmode": "finish_calibration"})[0] == 409  # no reference photo
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


def upload_update(handler, s3, user=USER, reference_id=None):
    body = {"runmode": "confirm_image", "id": uploaded_image(handler, s3, user, size=99), "width_px": 4000, "height_px": 3000}
    if reference_id is not None:
        body["reference_id"] = reference_id
    return call(handler, body, user)


def test_update_queues_analysis_job(handler, s3):
    calibrate(handler, s3)
    status, body = upload_update(handler, s3)
    assert status == 200
    assert body["job"]["status"] == "pending"
    assert body["job"]["image_id"] == body["image"]["id"]
    assert body["job"]["reference_id"] == query(handler, "SELECT id FROM reference_photos")[0]["id"]


def test_update_is_compared_with_the_chosen_reference(handler, s3):
    calibrate(handler, s3)
    south = upload_reference(handler, s3, name="South")
    assert upload_update(handler, s3)[0] == 400  # several photos: say which
    assert upload_update(handler, s3, reference_id=south["id"])[0] == 409  # South has no slips yet
    save(handler, [{"label": "B1", "polygon": SQUARE}], reference_id=south["id"])
    status, body = upload_update(handler, s3, reference_id=south["id"])
    assert status == 200 and body["job"]["reference_id"] == south["id"]
    assert upload_update(handler, s3, reference_id=999)[0] == 404


def test_reference_photo_is_not_an_update_and_vice_versa(handler, s3):
    calibrate(handler, s3)
    reference = query(handler, "SELECT image_id FROM reference_photos")[0]
    status, _ = call(handler, {"runmode": "confirm_image", "id": reference["image_id"], "width_px": 1, "height_px": 1})
    assert status == 409
    _, body = upload_update(handler, s3)
    status, _ = call(handler, {
        "runmode": "create_reference", "image_id": body["image"]["id"], "width_px": 1, "height_px": 1, "name": "X",
    })
    assert status == 409


def test_confirming_update_twice_queues_once(handler, s3):
    calibrate(handler, s3)
    _, body = upload_update(handler, s3)
    image_id = body["image"]["id"]
    again = call(handler, {"runmode": "confirm_image", "id": image_id, "width_px": 1, "height_px": 1})
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


def test_reference_photo_has_no_job(handler, s3):
    setup_marina(handler)
    upload_reference(handler, s3)
    assert query(handler, "SELECT id FROM analysis_jobs") == []


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


def test_retry_with_another_reference_photo(handler, s3):
    calibrate(handler, s3)
    _, body = upload_update(handler, s3)
    south = upload_reference(handler, s3, name="South")
    query(handler, "UPDATE analysis_jobs SET status = 'failed'")
    status, retried = call(handler, {"runmode": "retry_analysis", "image_id": body["image"]["id"], "reference_id": south["id"]})
    assert status == 200 and retried["job"]["reference_id"] == south["id"]


def test_retry_other_marinas_job(handler, s3):
    calibrate(handler, s3)
    _, body = upload_update(handler, s3)
    setup_marina(handler, OTHER_USER)
    status, _ = call(handler, {"runmode": "retry_analysis", "image_id": body["image"]["id"]}, OTHER_USER)
    assert status == 404


def test_editing_calibration_keeps_past_results(handler, s3):
    calibrate(handler, s3)  # one slip, A1
    _, body = upload_update(handler, s3)
    connection = handler.__globals__["get_connection"]()
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM slips"); slip_id = cursor.fetchone()["id"]
        cursor.execute(
            "INSERT INTO slip_observations (job_id, image_id, slip_id, label, occupied, observed_at) "
            "VALUES (%s, %s, %s, 'A1', 1, CURRENT_TIMESTAMP)",
            (body["job"]["id"], body["image"]["id"], slip_id),
        )

    # Recalibrate after the photo was analyzed: drop A1, add two new slips
    status, saved = save(handler, [{"label": "B1", "polygon": SQUARE}, {"label": "B2", "polygon": SQUARE}])
    assert status == 200 and [s["label"] for s in saved["slips"]] == ["B1", "B2"]

    with connection.cursor() as cursor:
        cursor.execute("SELECT slip_id, label, occupied FROM slip_observations")
        assert cursor.fetchall() == [{"slip_id": None, "label": "A1", "occupied": 1}]


# --- labelling

def analyzed_slip(handler, s3, occupied=True, confidence=0.6):
    """A calibrated marina with one analyzed update; returns (image_id, slip_id)."""
    calibrate(handler, s3)
    _, body = upload_update(handler, s3)
    connection = handler.__globals__["get_connection"]()
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM slips"); slip_id = cursor.fetchone()["id"]
        cursor.execute(
            "INSERT INTO slip_observations (job_id, image_id, slip_id, label, occupied, confidence, model_version, "
            "crop_s3_key, observed_at) VALUES (%s, %s, %s, 'A1', %s, %s, 'clip-zeroshot-v1', 'crops/1.jpg', CURRENT_TIMESTAMP)",
            (body["job"]["id"], body["image"]["id"], slip_id, occupied, confidence),
        )
    return body["image"]["id"], slip_id


def label(handler, image_id, slip_id, value, user=USER):
    return call(handler, {"runmode": "label_slip", "image_id": image_id, "slip_id": slip_id, "label": value}, user)


def test_label_corrects_the_result_and_keeps_the_guess(handler, s3):
    image_id, slip_id = analyzed_slip(handler, s3, occupied=True, confidence=0.6)
    status, body = label(handler, image_id, slip_id, "empty")
    assert status == 200
    assert body["status"] == {"occupied": False, "confidence": 1.0, "label": "empty"}
    assert body["labels"] == {
        "total": 1, "decided": 1, "agreed": 0, "accuracy": 0.0, "by_label": {"boat": 0, "empty": 1, "unclear": 0},
    }

    connection = handler.__globals__["get_connection"]()
    with connection.cursor() as cursor:
        cursor.execute("SELECT label, predicted_occupied, predicted_confidence, model_version, crop_s3_key FROM slip_labels")
        row = cursor.fetchone()
    assert row["label"] == "empty" and row["predicted_occupied"] == 1 and float(row["predicted_confidence"]) == 0.6
    assert row["model_version"] == "clip-zeroshot-v1" and row["crop_s3_key"] == "crops/1.jpg"


def test_changing_an_answer_keeps_the_original_guess(handler, s3):
    image_id, slip_id = analyzed_slip(handler, s3, occupied=True, confidence=0.6)
    label(handler, image_id, slip_id, "empty")
    status, body = label(handler, image_id, slip_id, "boat")  # changed my mind
    assert status == 200 and body["status"]["occupied"] is True
    assert body["labels"]["accuracy"] == 1.0 and body["labels"]["by_label"]["boat"] == 1  # model guessed boat


def test_unclear_keeps_the_prediction(handler, s3):
    image_id, slip_id = analyzed_slip(handler, s3, occupied=True, confidence=0.6)
    status, body = label(handler, image_id, slip_id, "unclear")
    assert body["status"] == {"occupied": True, "confidence": 0.6, "label": "unclear"}
    assert body["labels"]["decided"] == 0 and body["labels"]["accuracy"] is None


@pytest.mark.parametrize("fields", [
    {"label": "maybe"},
    {"label": "boat", "image_id": "x"},
])
def test_label_validation(handler, s3, fields):
    image_id, slip_id = analyzed_slip(handler, s3)
    payload = {"runmode": "label_slip", "image_id": image_id, "slip_id": slip_id, **fields}
    assert call(handler, payload)[0] == 400


def test_cannot_label_another_marinas_photo(handler, s3):
    image_id, slip_id = analyzed_slip(handler, s3)
    setup_marina(handler, OTHER_USER)
    assert label(handler, image_id, slip_id, "boat", OTHER_USER)[0] == 404


def test_label_requires_a_result(handler, s3):
    image_id, slip_id = analyzed_slip(handler, s3)
    assert label(handler, image_id, slip_id + 99, "boat")[0] == 404


def label_id(handler):
    connection = handler.__globals__["get_connection"]()
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM slip_labels")
        return cursor.fetchone()["id"]


def test_remove_label_restores_the_models_result(handler, s3):
    image_id, slip_id = analyzed_slip(handler, s3, occupied=True, confidence=0.6)
    label(handler, image_id, slip_id, "empty")  # misclick
    status, body = call(handler, {"runmode": "remove_label", "label_id": label_id(handler)})
    assert status == 200 and body["labels"]["total"] == 0

    connection = handler.__globals__["get_connection"]()
    with connection.cursor() as cursor:
        cursor.execute("SELECT occupied, confidence FROM slip_observations WHERE slip_id = %s", (slip_id,))
        row = cursor.fetchone()
    assert row["occupied"] == 1 and float(row["confidence"]) == 0.6


def test_remove_other_marinas_label(handler, s3):
    image_id, slip_id = analyzed_slip(handler, s3)
    label(handler, image_id, slip_id, "boat")
    setup_marina(handler, OTHER_USER)
    assert call(handler, {"runmode": "remove_label", "label_id": label_id(handler)}, OTHER_USER)[0] == 404


# --- uploading several drone photos at once

def two_reference_marina(handler, s3):
    """A calibrated marina with Main (A1) and South (B1) -> (main id, south id)."""
    calibrate(handler, s3)
    south = upload_reference(handler, s3, name="South")
    save(handler, [{"label": "B1", "polygon": SQUARE}], reference_id=south["id"])
    main_id = query(handler, "SELECT id FROM reference_photos WHERE name = 'Main'")[0]["id"]
    return main_id, south["id"]


def start_batch(handler, s3, count, user=USER):
    status, body = call(handler, {
        "runmode": "create_upload_batch",
        "photos": [{"content_type": "image/jpeg", "file_size": 100 + i} for i in range(count)],
    }, user)
    if status == 200:
        for i, photo in enumerate(body["photos"]):  # the browser uploads each file
            s3.objects[photo["upload"]["fields"]["key"]] = {"size": 100 + i, "content_type": "image/jpeg"}
    return status, body


def confirm(handler, batch, photos, user=USER):
    return call(handler, {"runmode": "confirm_batch", "batch_id": batch["batch_id"], "photos": photos}, user)


def photo(entry, reference_id=None):
    item = {"image_id": entry["image_id"], "width_px": 4000, "height_px": 3000}
    if reference_id:
        item["reference_id"] = reference_id
    return item


def test_an_upload_takes_at_most_one_photo_per_reference_photo(handler, s3):
    main_id, south_id = two_reference_marina(handler, s3)
    upload_reference(handler, s3, name="Empty")  # no slips yet: doesn't count
    status, body = start_batch(handler, s3, 3)
    assert status == 400 and "at most one photo per reference photo (2)" in body["error"]
    status, body = start_batch(handler, s3, 2)
    assert status == 200 and len(body["photos"]) == 2
    assert query(handler, "SELECT photo_count FROM upload_batches") == [{"photo_count": 2}]


def test_upload_batch_requires_calibration_and_valid_photos(handler, s3):
    setup_marina(handler)
    assert start_batch(handler, s3, 1)[0] == 409
    for photos in ([], "x", [{"content_type": "image/gif", "file_size": 5}], [{"content_type": "image/jpeg", "file_size": 0}]):
        assert call(handler, {"runmode": "create_upload_batch", "photos": photos})[0] in (400, 409)


def test_confirm_batch_queues_auto_detect_and_chosen_photos(handler, s3):
    main_id, south_id = two_reference_marina(handler, s3)
    _, batch = start_batch(handler, s3, 2)
    first, second = batch["photos"]
    status, body = confirm(handler, batch, [photo(first), photo(second, south_id)])
    assert status == 200
    jobs = {job["image_id"]: job for job in body["jobs"]}
    assert jobs[first["image_id"]]["reference_id"] is None  # the worker will find it
    assert jobs[second["image_id"]]["reference_id"] == south_id
    assert {job["batch_id"] for job in body["jobs"]} == {batch["batch_id"]}
    assert all(job["status"] == "pending" for job in body["jobs"])

    # confirming again queues nothing new
    again = confirm(handler, batch, [photo(first), photo(second, south_id)])
    assert again[0] == 200 and len(query(handler, "SELECT id FROM analysis_jobs")) == 2


def test_confirm_batch_rules(handler, s3):
    main_id, south_id = two_reference_marina(handler, s3)
    _, batch = start_batch(handler, s3, 2)
    first, second = batch["photos"]
    assert confirm(handler, batch, [photo(first, south_id), photo(second, south_id)])[0] == 400  # same reference twice
    assert confirm(handler, batch, [photo(first), photo(first)])[0] == 400
    empty = upload_reference(handler, s3, name="Empty")
    assert confirm(handler, batch, [photo(first, empty["id"])])[0] == 409  # no slips to analyze
    _, small = start_batch(handler, s3, 1)
    assert confirm(handler, small, [photo(small["photos"][0]), photo(first)])[0] == 400  # started for 1

    # nothing is queued if a photo never reached S3
    del s3.objects[query(handler, "SELECT s3_key FROM marina_images WHERE id = %s", (second["image_id"],))[0]["s3_key"]]
    status, body = confirm(handler, batch, [photo(first), photo(second)])
    assert status == 400 and query(handler, "SELECT id FROM analysis_jobs") == []

    # a photo already uploaded on its own can't join
    _, update = upload_update(handler, s3, reference_id=main_id)
    single = {"image_id": update["image"]["id"]}
    assert confirm(handler, batch, [photo(single)])[0] == 409

    setup_marina(handler, OTHER_USER)
    assert confirm(handler, batch, [photo(first)], OTHER_USER)[0] == 404


def test_retrying_an_auto_matched_photo_matches_it_again(handler, s3):
    main_id, south_id = two_reference_marina(handler, s3)
    _, batch = start_batch(handler, s3, 2)
    confirm(handler, batch, [photo(batch["photos"][0]), photo(batch["photos"][1], south_id)])
    auto_image, chosen_image = batch["photos"][0]["image_id"], batch["photos"][1]["image_id"]
    query(handler, "UPDATE analysis_jobs SET status = 'failed'")
    query(handler, "UPDATE analysis_jobs SET reference_id = %s, match_scores = %s WHERE image_id = %s",
          (main_id, json.dumps({str(main_id): {"points": 50, "fits": True}}), auto_image))

    _, body = call(handler, {"runmode": "retry_analysis", "image_id": auto_image})
    assert body["job"]["reference_id"] is None and body["job"]["match_scores"] is None
    _, body = call(handler, {"runmode": "retry_analysis", "image_id": chosen_image})
    assert body["job"]["reference_id"] == south_id  # chosen by hand: kept


def test_resetting_the_marina_also_removes_uploads_waiting_to_be_matched(handler, s3):
    calibrate(handler, s3)
    main_id = query(handler, "SELECT id FROM reference_photos")[0]["id"]
    _, batch = start_batch(handler, s3, 1)
    confirm(handler, batch, [photo(batch["photos"][0])])
    status, body = call(handler, {"runmode": "delete_reference", "reference_id": main_id})
    assert status == 200 and body["deleted"]["updates"] == 1 and body["calibrated"] is False
    assert query(handler, "SELECT id FROM analysis_jobs") == []
    assert query(handler, "SELECT id FROM upload_batches") == []
