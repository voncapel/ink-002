"""Small, single-printer web queue for the Snap & Tag S002."""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import threading
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from queue import Queue

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image

from parcel import (
    ParcelError,
    ParcelOutput,
    analyze_parcel,
    automatic_layout,
    compose_manual_parcel,
    prepare_document,
)
from mtg import (
    DEFAULT_LANG,
    MAX_BATCH_BYTES,
    MAX_BATCH_HEIGHT,
    MTG_RENDER_MODES,
    BatchInfo,
    MtgDeck,
    MtgError,
    RenderedCard,
    SUPPORTED_LANGS,
    _estimated_bytes,
    build_batches,
    parse_decklist,
    render_card_image,
    resolve_deck,
)
from rendering import DITHER_PRESETS, render_markdown, render_text, render_upload
from s002_protocol import encode_print_job, resolve_transport, send_print_job

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.environ.get("S002_DATA_DIR", BASE_DIR / "data"))
JOBS_DIR = DATA_DIR / "jobs"
PARCELS_DIR = DATA_DIR / "parcels"
MTG_DIR = DATA_DIR / "mtg"
MTG_CACHE_DIR = MTG_DIR / "cache"
PRINTER_MAC = os.environ.get("S002_MAC", "06:03:86:00:97:AB")
PRINTER_CHANNEL = int(os.environ.get("S002_CHANNEL", "1"))
PRINTER_TRANSPORT = resolve_transport(os.environ.get("S002_TRANSPORT", "auto"))
PRINTER_PORT = os.environ.get("S002_PORT", "/dev/cu.S002")
PRINTER_BAUD = int(os.environ.get("S002_BAUD", "115200"))
SERIAL_CHUNK_SIZE = int(os.environ.get("S002_SERIAL_CHUNK_SIZE", "64"))
SERIAL_CHUNK_DELAY = float(os.environ.get("S002_SERIAL_CHUNK_DELAY", "0.05"))
RFCOMM_CHUNK_SIZE = int(os.environ.get("S002_RFCOMM_CHUNK_SIZE", "0"))
RFCOMM_CHUNK_DELAY = float(os.environ.get("S002_RFCOMM_CHUNK_DELAY", "0"))
BASIC_USER = os.environ.get("S002_WEB_USER", "")
BASIC_PASSWORD = os.environ.get("S002_WEB_PASSWORD", "")
API_TOKEN = os.environ.get("S002_API_TOKEN", "")
PRINTING_ENABLED = os.environ.get("S002_PRINTING", "1") != "0"
MAX_HISTORY = 40
MAX_PARCELS = 8
MAX_MTG_DECKS = 8
MAX_UPLOAD_BYTES = 16 * 1024 * 1024

# Agents name densities; the printer wants the raw vendor values.
DENSITY_NAMES = {"light": 7, "medium": 12, "normal": 12, "dark": 15}

# Files printable by path. The unit runs with systemd's ProtectHome=true, so
# home directories stay unreachable no matter what this allowlist says.
ALLOWED_PATHS = tuple(
    Path(entry).expanduser().resolve()
    for entry in os.environ.get("S002_ALLOWED_PATHS", str(DATA_DIR / "inbox")).split(":")
    if entry.strip()
)

JOBS_DIR.mkdir(parents=True, exist_ok=True)
PARCELS_DIR.mkdir(parents=True, exist_ok=True)
MTG_DIR.mkdir(parents=True, exist_ok=True)
for _allowed_root in ALLOWED_PATHS:
    if _allowed_root == DATA_DIR or DATA_DIR in _allowed_root.parents:
        _allowed_root.mkdir(parents=True, exist_ok=True)


def clear_transient_files() -> None:
    """Remove private previews that cannot be recovered after a process restart."""
    for path in JOBS_DIR.glob("*.png"):
        path.unlink(missing_ok=True)
    for pattern in ("*-preview.png", "*-roll.png", "*-page.png", "*-source.*"):
        for path in PARCELS_DIR.glob(pattern):
            path.unlink(missing_ok=True)
    for path in MTG_DIR.glob("*-card-*.png"):
        path.unlink(missing_ok=True)
    for path in MTG_DIR.glob("*-roll-*.png"):
        path.unlink(missing_ok=True)


clear_transient_files()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    label: str
    source: str
    density: int
    threshold: int
    dither: str | None = None
    status: str = "queued"
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    sent_bytes: int | None = None
    reply_bytes: int | None = None
    elapsed_seconds: float | None = None
    error: str | None = None

    def public(self) -> dict:
        result = asdict(self)
        result["preview_url"] = f"/api/jobs/{self.id}/preview"
        return result


jobs: OrderedDict[str, Job] = OrderedDict()
jobs_lock = threading.Lock()
print_queue: Queue[str] = Queue()


@dataclass
class ParcelSession:
    id: str
    carrier: str
    confidence: float
    document_side: str
    notes: str
    label_width_mm: float
    label_height_mm: float
    band_heights_mm: tuple[float, ...]
    roll_height: int
    model: str
    created_at: str

    def public(self) -> dict:
        return {
            **asdict(self),
            "preview_url": f"/api/parcels/{self.id}/preview",
            "roll_url": f"/api/parcels/{self.id}/roll",
            "band_count": len(self.band_heights_mm),
        }


parcel_sessions: OrderedDict[str, ParcelSession] = OrderedDict()
parcel_lock = threading.Lock()


@dataclass
class ParcelDraft:
    id: str
    filename: str
    suffix: str
    width_mm: float
    height_mm: float
    preview_width: int
    preview_height: int
    created_at: str

    def public(self) -> dict:
        return {
            **asdict(self),
            "page_url": f"/api/parcels/drafts/{self.id}/page",
        }


parcel_drafts: OrderedDict[str, ParcelDraft] = OrderedDict()
draft_lock = threading.Lock()


mtg_decks: OrderedDict[str, MtgDeck] = OrderedDict()
mtg_lock = threading.Lock()


def clear_mtg_files(deck_id: str) -> None:
    for path in MTG_DIR.glob(f"{deck_id}-*"):
        path.unlink(missing_ok=True)


def prune_mtg_decks() -> None:
    with mtg_lock:
        while len(mtg_decks) > MAX_MTG_DECKS:
            old_id, _ = mtg_decks.popitem(last=False)
            clear_mtg_files(old_id)


def get_mtg_deck(deck_id: str) -> MtgDeck | None:
    with mtg_lock:
        return mtg_decks.get(deck_id)


def save_parcel_draft(filename: str, content: bytes) -> ParcelDraft:
    document = prepare_document(filename, content)
    draft_id = uuid.uuid4().hex[:12]
    source_path = PARCELS_DIR / f"{draft_id}-source{document.suffix}"
    source_path.write_bytes(content)
    document.ai_image.save(PARCELS_DIR / f"{draft_id}-page.png", format="PNG", optimize=True)
    draft = ParcelDraft(
        id=draft_id,
        filename=Path(filename).name[:160],
        suffix=document.suffix,
        width_mm=round(document.width_points * 25.4 / 72, 1),
        height_mm=round(document.height_points * 25.4 / 72, 1),
        preview_width=document.ai_image.width,
        preview_height=document.ai_image.height,
        created_at=now_iso(),
    )
    with draft_lock:
        parcel_drafts[draft_id] = draft
        while len(parcel_drafts) > MAX_PARCELS:
            old_id, old = parcel_drafts.popitem(last=False)
            (PARCELS_DIR / f"{old_id}-page.png").unlink(missing_ok=True)
            (PARCELS_DIR / f"{old_id}-source{old.suffix}").unlink(missing_ok=True)
    return draft


def get_parcel_draft(draft_id: str) -> ParcelDraft | None:
    with draft_lock:
        return parcel_drafts.get(draft_id)


def read_parcel_draft(draft: ParcelDraft) -> bytes:
    return (PARCELS_DIR / f"{draft.id}-source{draft.suffix}").read_bytes()


def save_parcel(output: ParcelOutput) -> ParcelSession:
    parcel_id = uuid.uuid4().hex[:12]
    output.preview.save(PARCELS_DIR / f"{parcel_id}-preview.png", format="PNG", optimize=True)
    output.roll.save(PARCELS_DIR / f"{parcel_id}-roll.png", format="PNG", optimize=True)
    session = ParcelSession(
        id=parcel_id,
        carrier=output.carrier[:80],
        confidence=round(output.confidence, 3),
        document_side=output.document_side,
        notes=output.notes[:240],
        label_width_mm=round(output.label_width_mm, 1),
        label_height_mm=round(output.label_height_mm, 1),
        band_heights_mm=tuple(round(value, 1) for value in output.band_heights_mm),
        roll_height=output.roll.height,
        model=output.model,
        created_at=now_iso(),
    )
    with parcel_lock:
        parcel_sessions[parcel_id] = session
        while len(parcel_sessions) > MAX_PARCELS:
            old_id, _ = parcel_sessions.popitem(last=False)
            (PARCELS_DIR / f"{old_id}-preview.png").unlink(missing_ok=True)
            (PARCELS_DIR / f"{old_id}-roll.png").unlink(missing_ok=True)
    return session


def save_job(
    image: Image.Image,
    *,
    label: str,
    source: str,
    density: int,
    threshold: int,
    dither: str | None = None,
) -> Job:
    job_id = uuid.uuid4().hex[:12]
    image.save(JOBS_DIR / f"{job_id}.png", format="PNG", optimize=True)
    job = Job(
        id=job_id,
        label=label[:80],
        source=source,
        density=density,
        threshold=threshold,
        dither=dither,
        created_at=now_iso(),
    )
    with jobs_lock:
        jobs[job_id] = job
        while len(jobs) > MAX_HISTORY:
            old_id, _ = jobs.popitem(last=False)
            (JOBS_DIR / f"{old_id}.png").unlink(missing_ok=True)
    print_queue.put(job_id)
    return job


def print_worker() -> None:
    while True:
        job_id = print_queue.get()
        try:
            with jobs_lock:
                job = jobs.get(job_id)
                if job is None:
                    continue
                job.status = "printing"
                job.started_at = now_iso()
            with Image.open(JOBS_DIR / f"{job_id}.png") as image:
                payload = encode_print_job(
                    image,
                    density=job.density,
                    threshold=job.threshold,
                )
            result = send_print_job(
                payload,
                mac=PRINTER_MAC,
                channel=PRINTER_CHANNEL,
                transport=PRINTER_TRANSPORT,
                port=PRINTER_PORT,
                baud=PRINTER_BAUD,
                chunk_size=(RFCOMM_CHUNK_SIZE if PRINTER_TRANSPORT == "macos_rfcomm" else SERIAL_CHUNK_SIZE),
                chunk_delay=(
                    RFCOMM_CHUNK_DELAY
                    if PRINTER_TRANSPORT == "macos_rfcomm"
                    else SERIAL_CHUNK_DELAY
                ),
            )
            with jobs_lock:
                job.status = "done"
                job.completed_at = now_iso()
                job.sent_bytes = result.sent_bytes
                job.reply_bytes = len(result.reply)
                job.elapsed_seconds = round(result.elapsed_seconds, 2)
        except Exception as exc:
            with jobs_lock:
                job = jobs.get(job_id)
                if job is not None:
                    job.status = "failed"
                    job.completed_at = now_iso()
                    job.error = f"{type(exc).__name__}: {exc}"
            app.logger.exception("S002 print job %s failed", job_id)
        finally:
            print_queue.task_done()


if PRINTING_ENABLED:
    threading.Thread(target=print_worker, name="s002-print-worker", daemon=True).start()


def _token_matches() -> bool:
    header = request.headers.get("Authorization", "")
    if header[:7].lower() == "bearer ":
        return hmac.compare_digest(header[7:].strip(), API_TOKEN)
    supplied = request.headers.get("X-API-Key", "")
    return bool(supplied) and hmac.compare_digest(supplied, API_TOKEN)


def _basic_matches() -> bool:
    auth = request.authorization
    return (
        auth is not None
        and auth.type == "basic"
        and hmac.compare_digest(auth.username or "", BASIC_USER)
        and hmac.compare_digest(auth.password or "", BASIC_PASSWORD)
    )


@app.before_request
def require_auth():
    """Optional defense-in-depth in addition to Cloudflare Access.

    Browsers authenticate with HTTP Basic; API clients such as Hermes send a
    bearer token. Either credential alone is sufficient, so the interface and
    the agent share one service and one print queue.
    """
    if not BASIC_PASSWORD and not API_TOKEN:
        return None
    if API_TOKEN and _token_matches():
        return None
    if BASIC_PASSWORD and _basic_matches():
        return None
    if not BASIC_PASSWORD:
        return jsonify({"error": "a valid bearer token is required"}), 401
    return ("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Ink 002"'})


@app.get("/")
def index():
    portable = PRINTER_TRANSPORT in {"macos_rfcomm", "macos_serial"}
    return render_template(
        "index.html",
        printer_mac=PRINTER_MAC,
        portable=portable,
        idle_label="S002 · Mac ready" if portable else "S002 · relay ready",
        transport_label="LOCAL MAC · RFCOMM 01" if portable else "HOME RELAY · RFCOMM 01",
    )


@app.get("/api/health")
def health():
    with jobs_lock:
        active = sum(job.status in {"queued", "printing"} for job in jobs.values())
    return jsonify(
        {
            "ok": True,
            "printer": "S002",
            "mac": PRINTER_MAC,
            "transport": PRINTER_TRANSPORT,
            "port": PRINTER_PORT if PRINTER_TRANSPORT == "macos_serial" else None,
            "active_jobs": active,
            "parcel_ai_configured": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
        }
    )


@app.post("/api/parcels/analyze")
def analyze_parcel_document():
    try:
        upload = request.files.get("file")
        if not upload or not upload.filename:
            raise ParcelError("Choisissez un bordereau PDF ou une image")
        content = upload.read()
        if not content:
            raise ParcelError("Le fichier envoyé est vide")
        output = analyze_parcel(upload.filename, content)
        return jsonify(save_parcel(output).public()), 201
    except (ParcelError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/parcels/prepare")
def prepare_parcel_document():
    try:
        upload = request.files.get("file")
        if not upload or not upload.filename:
            raise ParcelError("Choisissez un bordereau PDF ou une image")
        content = upload.read()
        if not content:
            raise ParcelError("Le fichier envoyé est vide")
        return jsonify(save_parcel_draft(upload.filename, content).public()), 201
    except (ParcelError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/parcels/drafts/<draft_id>/page")
def parcel_draft_page(draft_id: str):
    if get_parcel_draft(draft_id) is None:
        return jsonify({"error": "parcel draft not found"}), 404
    path = PARCELS_DIR / f"{draft_id}-page.png"
    if not path.is_file():
        return jsonify({"error": "parcel page not found"}), 404
    response = send_file(path, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.post("/api/parcels/drafts/<draft_id>/auto")
def auto_parcel_layout(draft_id: str):
    try:
        draft = get_parcel_draft(draft_id)
        if draft is None:
            return jsonify({"error": "parcel draft not found"}), 404
        result = automatic_layout(draft.filename, read_parcel_draft(draft))
        return jsonify(result)
    except (ParcelError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/parcels/drafts/<draft_id>/compose")
def compose_parcel_layout(draft_id: str):
    try:
        draft = get_parcel_draft(draft_id)
        if draft is None:
            return jsonify({"error": "parcel draft not found"}), 404
        payload = request.get_json(silent=True) or {}
        crop = payload.get("crop")
        cuts = payload.get("cuts")
        if not isinstance(crop, dict) or not isinstance(cuts, list):
            raise ParcelError("Cadre ou lignes de coupe invalides")
        output = compose_manual_parcel(
            draft.filename,
            read_parcel_draft(draft),
            crop,
            cuts,
        )
        return jsonify(save_parcel(output).public()), 201
    except (ParcelError, RuntimeError, ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400


def get_parcel_session(parcel_id: str) -> ParcelSession | None:
    with parcel_lock:
        return parcel_sessions.get(parcel_id)


@app.get("/api/parcels/<parcel_id>/preview")
def parcel_preview(parcel_id: str):
    if get_parcel_session(parcel_id) is None:
        return jsonify({"error": "parcel analysis not found"}), 404
    path = PARCELS_DIR / f"{parcel_id}-preview.png"
    if not path.is_file():
        return jsonify({"error": "parcel preview not found"}), 404
    response = send_file(path, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/api/parcels/<parcel_id>/roll")
def parcel_roll(parcel_id: str):
    if get_parcel_session(parcel_id) is None:
        return jsonify({"error": "parcel analysis not found"}), 404
    path = PARCELS_DIR / f"{parcel_id}-roll.png"
    if not path.is_file():
        return jsonify({"error": "parcel roll not found"}), 404
    response = send_file(path, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.post("/api/parcels/<parcel_id>/print")
def print_parcel(parcel_id: str):
    try:
        session = get_parcel_session(parcel_id)
        if session is None:
            return jsonify({"error": "parcel analysis not found"}), 404
        density = int(request.form.get("density", "12"))
        if density not in {7, 12, 15}:
            raise ValueError("density must be light, medium, or dark")
        with Image.open(PARCELS_DIR / f"{parcel_id}-roll.png") as source:
            image = source.convert("L")
        job = save_job(
            image,
            label=f"{session.carrier} · {len(session.band_heights_mm)} bandes",
            source="resize · mosaïque",
            density=density,
            threshold=128,
        )
        return jsonify(job.public()), 202
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/mtg/deck")
def create_mtg_deck():
    try:
        payload = request.get_json(silent=True) or {}
        deck_text = (payload.get("deck_text") or "").strip()
        lang = (payload.get("lang") or DEFAULT_LANG).strip().lower()
        if not deck_text:
            raise MtgError("Collez la decklist pour commencer")
        if lang not in SUPPORTED_LANGS:
            raise MtgError("langue non prise en charge")
        lines = parse_decklist(deck_text)
        if not lines:
            raise MtgError("aucune carte détectée dans la liste")
        cards, missing = resolve_deck(lines, lang)
        if not cards:
            raise MtgError("aucune carte résolue via Scryfall")
        deck_id = uuid.uuid4().hex[:12]
        deck = MtgDeck(
            id=deck_id,
            lang=lang,
            title="Deck MTG",
            cards=cards,
            missing=missing,
            created_at=now_iso(),
        )
        with mtg_lock:
            mtg_decks[deck_id] = deck
        prune_mtg_decks()
        return jsonify(deck.public()), 201
    except (MtgError, RuntimeError, ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/mtg/deck/<deck_id>/render")
def render_mtg_deck(deck_id):
    try:
        deck = get_mtg_deck(deck_id)
        if deck is None:
            return jsonify({"error": "deck not found"}), 404
        payload = request.get_json(silent=True) or {}
        dither = payload.get("dither", "floyd")
        if dither not in DITHER_PRESETS:
            raise MtgError("trame d'impression inconnue")
        contrast = int(payload.get("contrast", "100"))
        brightness = int(payload.get("brightness", "100"))
        sharpness = int(payload.get("sharpness", "100"))
        render_mode = str(payload.get("render_mode", "optimized"))
        show_artwork = bool(payload.get("show_artwork", True))
        if render_mode not in MTG_RENDER_MODES:
            raise MtgError("format de carte inconnu")
        if not 40 <= contrast <= 200:
            raise MtgError("le contraste doit rester entre 40 et 200")
        if not 40 <= brightness <= 160:
            raise MtgError("la clarté doit rester entre 40 et 160")
        if not 0 <= sharpness <= 250:
            raise MtgError("la netteté doit rester entre 0 et 250")

        clear_mtg_files(deck_id)
        include = payload.get("include")
        if include is not None:
            include = {int(index) for index in include}
        else:
            include = set(range(len(deck.cards)))
        selected = [card for index, card in enumerate(deck.cards) if index in include]
        if not selected:
            raise MtgError("aucune carte sélectionnée pour le rouleau")
        rendered: list[tuple[object, object]] = []
        gallery: list[RenderedCard] = []
        for index, card in enumerate(deck.cards):
            if index not in include:
                continue
            image = render_card_image(
                card,
                cache_dir=MTG_CACHE_DIR,
                dither=dither,
                contrast=contrast,
                brightness=brightness,
                sharpness=sharpness,
                render_mode=render_mode,
                show_artwork=show_artwork,
            )
            image.save(MTG_DIR / f"{deck_id}-card-{index}.png", format="PNG", optimize=True)
            rendered.append((card, image))
            gallery.append(RenderedCard(index=index, card=card, height=image.height, width=image.width))
        batches = build_batches(rendered)
        batch_infos = [
            BatchInfo(index=index, height=batch.height, estimated_bytes=_estimated_bytes(batch.height))
            for index, batch in enumerate(batches)
        ]
        for index, batch in enumerate(batches):
            batch.save(MTG_DIR / f"{deck_id}-roll-{index}.png", format="PNG", optimize=True)
        with mtg_lock:
            deck.batches = batch_infos
            deck.gallery = gallery
            deck.render_mode = render_mode
            deck.show_artwork = show_artwork
        deck_info = deck.public()
        deck_info["roll_height"] = sum(batch.height for batch in batches)
        deck_info["max_batch_height"] = MAX_BATCH_HEIGHT
        deck_info["max_batch_bytes"] = MAX_BATCH_BYTES
        return jsonify(deck_info)
    except (MtgError, RuntimeError, ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/mtg/deck/<deck_id>/cards/<int:index>/live-preview")
def mtg_card_live_preview(deck_id, index):
    """Return an exact renderer preview before the final roll is prepared."""
    try:
        deck = get_mtg_deck(deck_id)
        if deck is None:
            return jsonify({"error": "deck not found"}), 404
        if not 0 <= index < len(deck.cards):
            return jsonify({"error": "card not found"}), 404
        render_mode = request.args.get("render_mode", "optimized")
        if render_mode not in MTG_RENDER_MODES:
            raise MtgError("format de carte inconnu")
        dither = request.args.get("dither", "floyd")
        if dither not in DITHER_PRESETS:
            raise MtgError("trame d'impression inconnue")
        contrast = int(request.args.get("contrast", "100"))
        brightness = int(request.args.get("brightness", "100"))
        sharpness = int(request.args.get("sharpness", "100"))
        show_artwork = request.args.get("show_artwork", "1") not in {"0", "false"}
        if not 40 <= contrast <= 200 or not 40 <= brightness <= 160 or not 0 <= sharpness <= 250:
            raise MtgError("réglage d'image hors limites")
        image = render_card_image(
            deck.cards[index],
            cache_dir=MTG_CACHE_DIR,
            dither=dither,
            contrast=contrast,
            brightness=brightness,
            sharpness=sharpness,
            render_mode=render_mode,
            show_artwork=show_artwork,
        )
        stream = BytesIO()
        image.save(stream, format="PNG", optimize=True)
        stream.seek(0)
        response = send_file(stream, mimetype="image/png", max_age=0)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except (MtgError, RuntimeError, ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/mtg/deck/<deck_id>/cards/<int:index>/preview")
def mtg_card_preview(deck_id, index):
    if get_mtg_deck(deck_id) is None:
        return jsonify({"error": "deck not found"}), 404
    path = MTG_DIR / f"{deck_id}-card-{index}.png"
    if not path.is_file():
        return jsonify({"error": "card preview not found"}), 404
    response = send_file(path, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.get("/api/mtg/deck/<deck_id>/batches/<int:index>/preview")
def mtg_batch_preview(deck_id, index):
    if get_mtg_deck(deck_id) is None:
        return jsonify({"error": "deck not found"}), 404
    path = MTG_DIR / f"{deck_id}-roll-{index}.png"
    if not path.is_file():
        return jsonify({"error": "batch preview not found"}), 404
    response = send_file(path, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.post("/api/mtg/deck/<deck_id>/print")
def print_mtg_deck(deck_id):
    try:
        deck = get_mtg_deck(deck_id)
        if deck is None:
            return jsonify({"error": "deck not found"}), 404
        if not deck.batches:
            raise MtgError("préparer le rouleau avant d'imprimer")
        density = int(request.form.get("density", "12"))
        if density not in {7, 12, 15}:
            raise ValueError("density must be light, medium, or dark")
        job_ids: list[str] = []
        for batch in deck.batches:
            path = MTG_DIR / f"{deck_id}-roll-{batch.index}.png"
            with Image.open(path) as source:
                image = source.convert("L")
            job = save_job(
                image,
                label=f"MTG · lot {batch.index + 1}/{len(deck.batches)}",
                source="mtg · mosaïque",
                density=density,
                threshold=128,
            )
            job_ids.append(job.id)
        return jsonify({"jobs": job_ids, "count": len(job_ids)}), 202
    except (MtgError, RuntimeError, ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/status")
def status():
    """Queue summary for agents deciding whether to send another job."""
    with jobs_lock:
        history = list(jobs.values())
        queued = sum(job.status == "queued" for job in history)
        printing = sum(job.status == "printing" for job in history)
        last = history[-1].public() if history else None
    return jsonify(
        {
            "ok": True,
            "printer": "S002",
            "mac": PRINTER_MAC,
            "transport": PRINTER_TRANSPORT,
            "printing_enabled": PRINTING_ENABLED,
            "queued": queued,
            "printing": printing,
            "busy": bool(queued or printing),
            "history_size": len(history),
            "last_job": last,
        }
    )


@app.get("/api/spec")
def spec():
    """Machine-readable endpoint list so an agent can discover the print API."""
    allowed = ", ".join(str(path) for path in ALLOWED_PATHS) or "(none configured)"
    return jsonify(
        {
            "name": "ink-002",
            "description": "Print to the Snap & Tag S002 thermal printer (554-dot roll).",
            "auth": "Authorization: Bearer <S002_API_TOKEN>",
            "shared_options": {
                "density": "light | medium | dark (default medium)",
                "threshold": "1-254 black/white cutoff (default 85)",
                "font_size": "14-72 (default 32)",
                "label": "optional name shown in the job history",
            },
            "endpoints": [
                {
                    "method": "POST",
                    "path": "/api/print/text",
                    "body": {"text": "required", "align": "left | center | right"},
                },
                {
                    "method": "POST",
                    "path": "/api/print/markdown",
                    "body": {"markdown": "required"},
                },
                {
                    "method": "POST",
                    "path": "/api/print/image",
                    "body": {
                        "image_base64": "base64 PNG/JPEG/WebP/BMP/GIF/TIFF/PDF/TXT/MD",
                        "path": f"or a file under: {allowed}",
                        "filename": "names the format when sending base64",
                        "dither": f"one of {sorted(DITHER_PRESETS)} (default floyd)",
                        "contrast": "40-200 (default 100)",
                        "brightness": "40-160 (default 100)",
                        "sharpness": "0-250 (default 100)",
                    },
                },
                {"method": "GET", "path": "/api/status", "body": None},
                {"method": "GET", "path": "/api/jobs", "body": None},
                {"method": "GET", "path": "/api/jobs/<id>", "body": None},
            ],
        }
    )


def read_params() -> dict:
    """Accept a JSON body from API clients or a form post from the web UI."""
    if request.is_json:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("the JSON body must be an object")
        return payload
    return request.form.to_dict()


def parse_int(params: dict, name: str, default: int, low: int, high: int) -> int:
    raw = params.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def parse_density(params: dict) -> int:
    raw = params.get("density", "medium")
    if isinstance(raw, str):
        named = DENSITY_NAMES.get(raw.strip().lower())
        if named is not None:
            return named
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("density must be light, medium, or dark") from None
    if value not in {7, 12, 15}:
        raise ValueError("density must be light, medium, or dark")
    return value


def parse_common(params: dict) -> dict:
    return {
        "density": parse_density(params),
        "threshold": parse_int(params, "threshold", 85, 1, 254),
    }


def parse_image_options(params: dict) -> dict:
    dither = str(params.get("dither", "floyd"))
    if dither not in DITHER_PRESETS:
        raise ValueError(f"dither must be one of {sorted(DITHER_PRESETS)}")
    return {
        "dither": dither,
        "contrast": parse_int(params, "contrast", 100, 40, 200),
        "brightness": parse_int(params, "brightness", 100, 40, 160),
        "sharpness": parse_int(params, "sharpness", 100, 0, 250),
    }


def decode_base64(value: str) -> bytes:
    """Decode a base64 payload, tolerating a data: URI wrapper."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("the base64 payload is empty")
    cleaned = value.strip()
    if cleaned.startswith("data:"):
        _, _, cleaned = cleaned.partition(",")
    try:
        blob = base64.b64decode(cleaned, validate=False)
    except (binascii.Error, ValueError):
        raise ValueError("the base64 payload could not be decoded") from None
    if not blob:
        raise ValueError("the base64 payload is empty")
    if len(blob) > MAX_UPLOAD_BYTES:
        raise ValueError("decoded payloads are limited to 16 MB")
    return blob


def resolve_allowed_path(raw: str) -> Path:
    candidate = Path(raw).expanduser().resolve()
    if not any(candidate == root or root in candidate.parents for root in ALLOWED_PATHS):
        allowed = ", ".join(str(root) for root in ALLOWED_PATHS) or "(none configured)"
        raise ValueError(f"path is outside the allowed print directories: {allowed}")
    if not candidate.is_file():
        raise ValueError(f"no such file: {candidate}")
    if candidate.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("files are limited to 16 MB")
    return candidate


@app.post("/api/print/text")
def print_text():
    """Queue plain Unicode text. Body: {"text": "...", "align": "center"}."""
    try:
        params = read_params()
        text = params.get("text", "")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        image = render_text(
            text,
            font_size=parse_int(params, "font_size", 32, 14, 72),
            align=str(params.get("align", "left")),
        )
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "Text")
        job = save_job(
            image,
            label=str(params.get("label") or first_line),
            source="text",
            **parse_common(params),
        )
        return jsonify(job.public()), 202
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/print/markdown")
def print_markdown():
    """Queue markdown rendered with headings, lists, quotes, and code blocks."""
    try:
        params = read_params()
        markdown = params.get("markdown", params.get("text", ""))
        if not isinstance(markdown, str):
            raise ValueError("markdown must be a string")
        image = render_markdown(markdown, font_size=parse_int(params, "font_size", 32, 14, 72))
        heading = next(
            (line.strip().lstrip("#").strip() for line in markdown.splitlines() if line.strip()),
            "Markdown",
        )
        job = save_job(
            image,
            label=str(params.get("label") or heading),
            source="markdown",
            **parse_common(params),
        )
        return jsonify(job.public()), 202
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/print/image")
def print_image():
    """Queue an image, PDF, TXT, or MD payload.

    Accepts a multipart ``file`` upload, ``{"image_base64": ...}``, or
    ``{"path": ...}`` pointing inside an allowed directory.
    """
    try:
        params = read_params()
        upload = request.files.get("file")
        if upload and upload.filename:
            filename = upload.filename
            stream = upload.stream
        elif params.get("path"):
            resolved = resolve_allowed_path(str(params["path"]))
            filename = str(params.get("filename") or resolved.name)
            stream = BytesIO(resolved.read_bytes())
        else:
            encoded = params.get("image_base64") or params.get("content_base64")
            if not encoded:
                raise ValueError("provide a file upload, image_base64, or path")
            filename = str(params.get("filename") or "upload.png")
            stream = BytesIO(decode_base64(str(encoded)))
        image_options = parse_image_options(params)
        image = render_upload(
            filename,
            stream,
            font_size=parse_int(params, "font_size", 32, 14, 72),
            **image_options,
        )
        job = save_job(
            image,
            label=str(params.get("label") or filename),
            source=f"image · {image_options['dither']}",
            dither=image_options["dither"],
            **parse_common(params),
        )
        return jsonify(job.public()), 202
    except (RuntimeError, ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/jobs")
def list_jobs():
    with jobs_lock:
        recent = [job.public() for job in reversed(jobs.values())]
    return jsonify({"jobs": recent})


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "job not found"}), 404
        return jsonify(job.public())


@app.get("/api/jobs/<job_id>/preview")
def preview(job_id: str):
    with jobs_lock:
        if job_id not in jobs:
            return jsonify({"error": "job not found"}), 404
    path = JOBS_DIR / f"{job_id}.png"
    if not path.is_file():
        return jsonify({"error": "preview not found"}), 404
    return send_file(path, mimetype="image/png", max_age=60)


@app.post("/api/jobs")
def create_job():
    try:
        density = int(request.form.get("density", "12"))
        threshold = int(request.form.get("threshold", "85"))
        font_size = int(request.form.get("font_size", "32"))
        align = request.form.get("align", "left")
        if density not in {7, 12, 15}:
            raise ValueError("density must be light, medium, or dark")
        if not 1 <= threshold <= 254:
            raise ValueError("threshold must be between 1 and 254")

        upload = request.files.get("file")
        text = request.form.get("text", "")
        dither = request.form.get("dither", "floyd")
        contrast = int(request.form.get("contrast", "100"))
        brightness = int(request.form.get("brightness", "100"))
        sharpness = int(request.form.get("sharpness", "100"))
        if upload and upload.filename:
            if dither not in DITHER_PRESETS:
                raise ValueError("unknown dithering preset")
            if not 40 <= contrast <= 200:
                raise ValueError("contrast must be between 40 and 200")
            if not 40 <= brightness <= 160:
                raise ValueError("brightness must be between 40 and 160")
            if not 0 <= sharpness <= 250:
                raise ValueError("sharpness must be between 0 and 250")
            image = render_upload(
                upload.filename,
                upload.stream,
                font_size=font_size,
                dither=dither,
                contrast=contrast,
                brightness=brightness,
                sharpness=sharpness,
            )
            label = upload.filename
            source = f"image · {dither}"
        else:
            image = render_text(text, font_size=font_size, align=align)
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "Text")
            label = first_line[:60]
            source = "text"

        job = save_job(
            image,
            label=label,
            source=source,
            density=density,
            threshold=threshold,
            dither=dither if upload and upload.filename else None,
        )
        return jsonify(job.public()), 202
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "uploads are limited to 16 MB"}), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8092")), debug=False)
