"""API surface used by agents: print endpoints, auth, and status."""

import base64
import io

from PIL import Image
import pytest

import app as app_module
from rendering import render_markdown
from s002_protocol import PRINT_WIDTH


@pytest.fixture()
def client():
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client
    with app_module.jobs_lock:
        app_module.jobs.clear()
    while not app_module.print_queue.empty():
        app_module.print_queue.get_nowait()


def png_bytes(size=(120, 60), color=200) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_print_text_queues_a_job(client):
    response = client.post("/api/print/text", json={"text": "bonjour", "align": "center"})
    assert response.status_code == 202
    body = response.get_json()
    assert body["status"] == "queued"
    assert body["source"] == "text"
    assert body["label"] == "bonjour"
    assert body["density"] == 12
    assert client.get(f"/api/jobs/{body['id']}").status_code == 200
    assert client.get(f"/api/jobs/{body['id']}/preview").status_code == 200


def test_density_accepts_names_and_rejects_junk(client):
    dark = client.post("/api/print/text", json={"text": "x", "density": "dark"})
    assert dark.get_json()["density"] == 15

    numeric = client.post("/api/print/text", json={"text": "x", "density": 7})
    assert numeric.get_json()["density"] == 7

    bad = client.post("/api/print/text", json={"text": "x", "density": "extra-dark"})
    assert bad.status_code == 400
    assert "density" in bad.get_json()["error"]


def test_threshold_and_font_size_are_bounded(client):
    assert client.post("/api/print/text", json={"text": "x", "threshold": 0}).status_code == 400
    assert client.post("/api/print/text", json={"text": "x", "threshold": 255}).status_code == 400
    assert client.post("/api/print/text", json={"text": "x", "font_size": 9}).status_code == 400


def test_empty_text_is_rejected(client):
    response = client.post("/api/print/text", json={"text": "   "})
    assert response.status_code == 400


def test_print_markdown_queues_a_job_labelled_by_its_heading(client):
    response = client.post(
        "/api/print/markdown",
        json={"markdown": "# Courses\n\n- pain\n- **beurre**\n\n> avant 18h\n"},
    )
    assert response.status_code == 202
    body = response.get_json()
    assert body["source"] == "markdown"
    assert body["label"] == "Courses"


def test_print_image_accepts_base64(client):
    response = client.post(
        "/api/print/image",
        json={"image_base64": base64.b64encode(png_bytes()).decode(), "filename": "ticket.png"},
    )
    assert response.status_code == 202
    assert response.get_json()["label"] == "ticket.png"
    assert response.get_json()["source"] == "image · floyd"


def test_print_image_accepts_a_data_uri(client):
    encoded = base64.b64encode(png_bytes()).decode()
    response = client.post(
        "/api/print/image",
        json={"image_base64": f"data:image/png;base64,{encoded}"},
    )
    assert response.status_code == 202


def test_print_image_rejects_bad_base64(client):
    response = client.post("/api/print/image", json={"image_base64": "not base64 at all!!"})
    assert response.status_code == 400


def test_print_image_requires_a_payload(client):
    response = client.post("/api/print/image", json={})
    assert response.status_code == 400
    assert "image_base64" in response.get_json()["error"]


def test_print_image_reads_an_allowed_path(client):
    inbox = app_module.ALLOWED_PATHS[0]
    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / "note.png"
    target.write_bytes(png_bytes())
    response = client.post("/api/print/image", json={"path": str(target)})
    assert response.status_code == 202
    assert response.get_json()["label"] == "note.png"


def test_print_image_refuses_a_path_outside_the_allowlist(client):
    response = client.post("/api/print/image", json={"path": "/etc/hostname"})
    assert response.status_code == 400
    assert "outside the allowed" in response.get_json()["error"]


def test_print_image_refuses_traversal_out_of_the_allowlist(client):
    escape = app_module.ALLOWED_PATHS[0] / ".." / ".." / "etc" / "hostname"
    response = client.post("/api/print/image", json={"path": str(escape)})
    assert response.status_code == 400


def test_multipart_upload_still_works(client):
    response = client.post(
        "/api/print/image",
        data={"file": (io.BytesIO(png_bytes()), "photo.png"), "density": "light"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    assert response.get_json()["density"] == 7


def test_web_ui_form_endpoint_is_untouched(client):
    form = client.post("/api/jobs", data={"text": "form", "density": "12"})
    assert form.status_code == 202
    assert form.get_json()["source"] == "text"


def test_print_image_honours_dither_options(client):
    response = client.post(
        "/api/print/image",
        json={"image_base64": base64.b64encode(png_bytes()).decode(), "dither": "atkinson"},
    )
    assert response.status_code == 202
    body = response.get_json()
    assert body["dither"] == "atkinson"
    assert body["source"] == "image · atkinson"


def test_print_image_rejects_an_unknown_dither(client):
    response = client.post(
        "/api/print/image",
        json={"image_base64": base64.b64encode(png_bytes()).decode(), "dither": "swirl"},
    )
    assert response.status_code == 400
    assert "dither" in response.get_json()["error"]


def test_print_image_bounds_the_adjustments(client):
    encoded = base64.b64encode(png_bytes()).decode()
    assert (
        client.post("/api/print/image", json={"image_base64": encoded, "contrast": 5}).status_code
        == 400
    )
    assert (
        client.post("/api/print/image", json={"image_base64": encoded, "brightness": 500}).status_code
        == 400
    )


def test_status_reports_the_queue(client):
    client.post("/api/print/text", json={"text": "un"})
    client.post("/api/print/text", json={"text": "deux"})
    body = client.get("/api/status").get_json()
    assert body["queued"] == 2
    assert body["busy"] is True
    assert body["printing_enabled"] is False
    assert body["last_job"]["label"] == "deux"


def test_spec_lists_the_print_endpoints(client):
    paths = {entry["path"] for entry in client.get("/api/spec").get_json()["endpoints"]}
    assert {"/api/print/text", "/api/print/markdown", "/api/print/image"} <= paths


def test_bearer_token_guards_the_api(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_TOKEN", "secret-token")
    monkeypatch.setattr(app_module, "BASIC_PASSWORD", "")

    assert client.post("/api/print/text", json={"text": "x"}).status_code == 401
    assert client.get("/api/status").status_code == 401

    wrong = client.get("/api/status", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401

    ok = client.get("/api/status", headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200

    api_key = client.get("/api/status", headers={"X-API-Key": "secret-token"})
    assert api_key.status_code == 200


def test_basic_auth_still_works_alongside_a_token(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_TOKEN", "secret-token")
    monkeypatch.setattr(app_module, "BASIC_USER", "tristan")
    monkeypatch.setattr(app_module, "BASIC_PASSWORD", "hunter2")

    anonymous = client.get("/api/status")
    assert anonymous.status_code == 401
    assert anonymous.headers["WWW-Authenticate"].startswith("Basic")

    assert client.get("/api/status", auth=("tristan", "hunter2")).status_code == 200
    assert client.get("/api/status", headers={"Authorization": "Bearer secret-token"}).status_code == 200
    assert client.get("/api/status", auth=("tristan", "wrong")).status_code == 401


def test_markdown_renders_at_printer_width_and_grows_with_content():
    short = render_markdown("# Titre\n\nUne ligne.")
    long = render_markdown(
        "# Titre\n\n"
        "Un paragraphe qui explique quelque chose.\n\n"
        "## Sous-titre\n\n"
        "- premier point\n"
        "- deuxieme point avec du `code` et du **gras**\n"
        "  - un point imbrique\n\n"
        "1. etape une\n"
        "2. etape deux\n\n"
        "> une citation\n\n"
        "```\nprint('hello')\n```\n\n"
        "---\n\n"
        "Fin avec un [lien](https://example.com).\n"
    )
    assert short.width == PRINT_WIDTH
    assert long.width == PRINT_WIDTH
    assert long.height > short.height
    assert long.getextrema()[0] == 0  # actually drew ink


def test_markdown_rejects_empty_input():
    with pytest.raises(ValueError):
        render_markdown("   \n\n")
