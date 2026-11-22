"""
Knowledge Base (agent_data_sources) detail + create handlers.

Kept in a separate module so `admin/router.py` stays slim. Imports the
shared `router`, `templates`, `_safe_list`, `current_env` helpers from
the router module so we don't fork the dependency wiring.

Routes added:
  GET  /admin/kb/{source_id}   — editable detail view
  POST /admin/kb/{source_id}   — persist edits
  POST /admin/kb               — create new data source from inline form

The list view (GET /admin/kb) is still owned by router.py.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from admin.audit import record_change
from admin.auth import require_superuser
from admin.router import (
    _safe_list,
    current_env,
    router,
    templates,
)
from core.auth import get_service_client, admin_service_client

logger = logging.getLogger(__name__)


# Mirrors the agent_data_source_kind enum defined in
# supabase/migrations/20260514120000_admin_portal_agent_config.sql.
# Keep these in sync if the enum ever grows.
KB_KINDS = ("pgvector", "ms_drive", "gitbook", "supabase_table", "http")

# ai_agent_kind values surfaced in the agent filter pill bar — kept here
# rather than hard-coded in two templates.
# Kept as the base for back-compat imports; the picker itself now uses the
# shared live list (``_kb_agent_kinds``).
from admin.agent_kinds import catalog_tuple as _catalog_tuple  # noqa: E402

AGENT_KIND_OPTIONS = _catalog_tuple("kb_kinds")


def _kb_agent_kinds() -> tuple:
    """Agent kinds offered in the KB doc's primary-kind picker. Collapses to
    the active client's owned kinds on any scoped build 
    so the picker never advertises WAJ's catalog; the WAJ list otherwise
    (unchanged). Mirrors the /admin/kb list chip bar in router.py."""
    # Partner build: its own agents only (never the reserved title /
    # suggestions kinds, which have no profile); WAJ build: the shared live
    # list (the old fixed tuple omitted client and demo kinds).
    from admin.agent_kinds import build_agent_kinds
    return build_agent_kinds()


def _parse_form_payload(form: Any) -> Dict[str, Any]:
    """Pull a normalized payload out of the detail / create form.

    Only fields actually present in the form are returned, so update
    handlers can safely splat into a `update(...)` call without nulling
    columns the form didn't surface.
    """
    payload: Dict[str, Any] = {}

    for f in ("name", "kind", "agent_kind", "refresh_cadence"):
        v = form.get(f)
        if v is not None and v != "":
            payload[f] = v

    sla = form.get("freshness_sla_hours")
    if sla not in (None, ""):
        try:
            payload["freshness_sla_hours"] = int(sla)
        except ValueError:
            raise HTTPException(400, "freshness_sla_hours must be an integer")

    # additional_agent_kinds is a newline-separated multiline text input.
    # additional_agent_kinds: the detail page now posts one checkbox per agent
    # (repeated field); the old newline-separated textarea shape is still
    # accepted. The `_additional_kinds_form` marker lets "none ticked" clear it.
    if "additional_agent_kinds" in form or form.get("_additional_kinds_form") == "1":
        getlist = getattr(form, "getlist", None)
        values = getlist("additional_agent_kinds") if getlist else [form.get("additional_agent_kinds") or ""]
        kinds: List[str] = []
        for raw in values:
            for line in (raw or "").splitlines():
                line = line.strip()
                if line and line not in kinds:
                    kinds.append(line)
        payload["additional_agent_kinds"] = kinds

    if "config" in form:
        raw_cfg = form.get("config") or "{}"
        try:
            payload["config"] = json.loads(raw_cfg)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"config must be valid JSON: {exc}")

    # is_active checkbox — only meaningful if the form opts in via marker.
    if form.get("_active_form") == "1":
        payload["is_active"] = form.get("is_active") == "on"

    return payload


def _inspect_pgvector_table(client: Any, table_name: str) -> List[Dict[str, str]]:
    """Best-effort schema introspection for a pgvector-backed source.

    Reads `information_schema.columns` for `public.<table_name>` via the
    service-role client. Returns [] if the table doesn't exist or the
    introspection RPC isn't reachable — never raises, so the detail page
    keeps rendering even if the table was renamed/dropped.

    NOTE: `table_name` comes from the row's `config.table` (admin-owned),
    not from any user-supplied query string, but we still defensively
    reject anything that isn't a plain identifier.
    """
    if not table_name or not isinstance(table_name, str):
        return []
    # Allow only the postgres identifier charset — letters, digits, underscore.
    if not table_name.replace("_", "").isalnum():
        return []
    try:
        result = (
            client.table("columns")
            .select("column_name,data_type")
            .eq("table_schema", "public")
            .eq("table_name", table_name)
            .execute()
        )
        rows = result.data or []
        return [
            {"column_name": r.get("column_name"), "data_type": r.get("data_type")}
            for r in rows
        ]
    except Exception as exc:  # pragma: no cover - depends on remote schema cache
        logger.info("kb: information_schema lookup failed for %s (%s)", table_name, exc)
        return []


# ---------------------------------------------------------------------------
# GET /admin/kb/{source_id}
# ---------------------------------------------------------------------------


def _load_pages_summary(
    client: Any,
    source_id: str,
) -> tuple[Optional[List[Dict[str, Any]]], int, int]:
    """Fetch pages + chunk-count summary for a shared pgvector KB.

    Returns (pages_list, pages_total, chunks_total).
    Each item in pages_list has: id, title, type, language, source,
    created_at, chunk_count.

    Caps the pages list at 200 rows but reports the true total count
    (len of the full un-capped fetch, since the Supabase client doesn't
    expose COUNT easily without an RPC). For the demo scale (≤1000 pages)
    fetching all and counting in Python is fine.
    """
    PAGE_CAP = 200
    try:
        pages_result = (
            client.table("ai_kb_pages")
            .select("id,title,type,language,source,created_at")
            .eq("source_id", source_id)
            .order("created_at", desc=False)
            .execute()
        )
        all_pages = pages_result.data or []
    except Exception as exc:
        logger.info("kb_detail: ai_kb_pages fetch failed for %s (%s)", source_id, exc)
        return None, 0, 0

    pages_total = len(all_pages)
    pages_display = all_pages[:PAGE_CAP]

    # Fetch all embedding rows for this source and tally by page_id.
    try:
        emb_result = (
            client.table("ai_kb_embeddings")
            .select("page_id")
            .eq("source_id", source_id)
            .execute()
        )
        emb_rows = emb_result.data or []
    except Exception as exc:
        logger.info("kb_detail: ai_kb_embeddings fetch failed for %s (%s)", source_id, exc)
        emb_rows = []

    chunks_total = len(emb_rows)

    # Build page_id → chunk_count map.
    chunk_counts: Dict[Any, int] = {}
    for emb in emb_rows:
        pid = emb.get("page_id")
        if pid is not None:
            chunk_counts[pid] = chunk_counts.get(pid, 0) + 1

    # Attach chunk_count to each displayed page.
    pages_with_counts = [
        {**p, "chunk_count": chunk_counts.get(p.get("id"), 0)}
        for p in pages_display
    ]

    return pages_with_counts, pages_total, chunks_total


@router.get("/kb/{source_id}", response_class=HTMLResponse, name="kb_detail")
async def kb_detail(
    source_id: str,
    request: Request,
    env: str = Depends(current_env),
    ctx: dict = Depends(require_superuser),
) -> HTMLResponse:
    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_data_sources")
        .select("*")
        .eq("source_id", source_id)
        .limit(1)
        .execute()
    )
    if not rows:
        raise HTTPException(404, "data source not found")
    row = rows[0]

    schema_rows: List[Dict[str, str]] = []
    if row.get("kind") == "pgvector":
        cfg = row.get("config") or {}
        table_name = cfg.get("table") if isinstance(cfg, dict) else None
        if table_name:
            schema_rows = _inspect_pgvector_table(client, table_name)

    # Pages/chunks summary — only for shared pgvector KBs backed by ai_kb_embeddings.
    pages: Optional[List[Dict[str, Any]]] = None
    pages_total = 0
    chunks_total = 0
    if row.get("kind") == "pgvector":
        cfg = row.get("config") or {}
        if _is_shared_kb(cfg):
            pages, pages_total, chunks_total = _load_pages_summary(client, source_id)

    return templates.TemplateResponse(
        request,
        "kb_detail.html",
        {
            "ctx": ctx,
            "active": "kb",
            "current_env": env,
            "row": row,
            "kinds": KB_KINDS,
            "agent_kinds": _kb_agent_kinds(),
            "schema_rows": schema_rows,
            "pages": pages,
            "pages_total": pages_total,
            "chunks_total": chunks_total,
            "embedding": embedding_status(),
            "embedding_mismatch": embedding_mismatch(row.get("config")) if chunks_total else None,
            "reembed_command": REEMBED_COMMAND,
        },
    )


# ---------------------------------------------------------------------------
# POST /admin/kb/{source_id}
# ---------------------------------------------------------------------------


@router.post("/kb/{source_id}", name="kb_update")
async def kb_update(
    source_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    payload = _parse_form_payload(form)
    if not payload:
        # Nothing to write; bounce back to detail rather than 400 the user.
        return RedirectResponse(url=f"/admin/kb/{source_id}", status_code=302)

    client = admin_service_client()
    before = _safe_list(
        client.table("agent_data_sources")
        .select("*")
        .eq("source_id", source_id)
        .limit(1)
        .execute()
    )
    if not before:
        raise HTTPException(404, "data source not found")

    client.table("agent_data_sources").update(payload).eq("source_id", source_id).execute()
    record_change(
        entity_type="agent_data_sources",
        entity_id=source_id,
        action="update",
        actor_user_id=ctx["user_id"],
        before=before[0],
        after={**before[0], **payload},
    )
    return RedirectResponse(url=f"/admin/kb/{source_id}", status_code=302)


# ---------------------------------------------------------------------------
# POST /admin/kb  (inline create form on the list page)
# ---------------------------------------------------------------------------


@router.post("/kb", name="kb_create")
async def kb_create(
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    form = await request.form()
    payload = _parse_form_payload(form)
    if not payload.get("name") or not payload.get("kind"):
        raise HTTPException(400, "name and kind are required")
    if "config" not in payload:
        payload["config"] = {}

    # Default pgvector KBs to the shared tables when no explicit table is set.
    # This means operators can create a pgvector KB with no config and start
    # uploading immediately — no per-KB migration required.
    if payload.get("kind") == "pgvector" and not payload["config"].get("table"):
        payload["config"] = {
            **payload["config"],
            "table": "ai_kb_embeddings",
            "pages_table": "ai_kb_pages",
        }

    client = admin_service_client()
    inserted = client.table("agent_data_sources").insert(payload).execute()
    new_row = (inserted.data or [{}])[0]
    record_change(
        entity_type="agent_data_sources",
        entity_id=new_row.get("source_id", ""),
        action="create",
        actor_user_id=ctx["user_id"],
        after=new_row,
    )
    new_id = new_row.get("source_id")
    target = f"/admin/kb/{new_id}" if new_id else "/admin/kb"
    return RedirectResponse(url=target, status_code=302)


# ---------------------------------------------------------------------------
# POST /admin/kb/{source_id}/upload — file ingestion into pgvector tables
# ---------------------------------------------------------------------------


# Hard cap per file to keep the request-thread ingestion path snappy. Larger
# files want a background queue (see TODO below).
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
REQUIRED_PGVECTOR_COLUMNS = ("id", "content", "embedding")
# Most pgvector data sources have a sibling "pages" table. When the source
# config doesn't name one we only auto-derive for the shared KB table.
_DEFAULT_PAGES_TABLE_FOR = {
    "ai_kb_embeddings": "ai_kb_pages",
}


def _is_shared_kb(config: dict) -> bool:
    """A pgvector source whose embeddings live in the shared, source-scoped table."""
    return (config or {}).get("table") == "ai_kb_embeddings"


# Where a shared knowledge base records which embedder wrote its vectors
# (`provider:model`, core.embedding_provider.embedding_identity). The table
# itself has no such column, and vectors from two models are not comparable,
# so this is the only way to tell "embedded by something else" from "fine".
# Sources created before this existed carry no stamp and are not flagged.
EMBEDDED_WITH_KEY = "embedded_with"

REEMBED_COMMAND = "python -m scripts.reembed --target kb --apply"


def embedding_status() -> Dict[str, Any]:
    """The embedder in force, for the Knowledge screens (no network call)."""
    from core.embedding_provider import embedding_status as _status

    return _status()


def embedding_mismatch(config: Any) -> Optional[Dict[str, str]]:
    """{stored, current} when this source's vectors came from another embedder."""
    if not isinstance(config, dict) or not _is_shared_kb(config):
        return None
    stored = config.get(EMBEDDED_WITH_KEY)
    if not stored or not isinstance(stored, str):
        return None
    from core.embedding_provider import embedding_identity

    current = embedding_identity()
    return None if stored == current else {"stored": stored, "current": current}


def upload_blocker(client: Any, source_id: str, config: Any) -> Optional[str]:
    """Why a document cannot be added right now, in plain words, or None.

    Two reasons, both known before any work is done: no embedding model is
    set up, or this knowledge base already holds passages written by a
    different one (adding more would mix two spaces in one search).
    """
    status = embedding_status()
    if not status["ready"]:
        return f"No embedding model is set up, so documents cannot be made searchable. {status['problem']}"
    mismatch = embedding_mismatch(config)
    if mismatch and _has_passages(client, source_id):
        return (
            f"This knowledge base was built with {mismatch['stored']}, and the engine "
            f"now uses {mismatch['current']}. Re-embed it first ({REEMBED_COMMAND}), "
            "or switch the embedding settings back."
        )
    return None


def _has_passages(client: Any, source_id: str) -> bool:
    try:
        found = (
            client.table("ai_kb_embeddings").select("id")
            .eq("source_id", source_id).limit(1).execute().data
        )
    except Exception as exc:  # noqa: BLE001 - cannot tell: do not block on a guess
        logger.info("kb: passage lookup failed for %s (%s)", source_id, exc)
        return False
    return bool(found)


def stamp_embedded_with(client: Any, source_id: str, identity: Optional[str] = None) -> None:
    """Record on the source which embedder wrote its vectors. Never raises."""
    try:
        if identity is None:
            from core.embedding_provider import embedding_identity

            identity = embedding_identity()
        found = (
            client.table("agent_data_sources").select("config")
            .eq("source_id", source_id).limit(1).execute().data
        )
        if not found:
            return
        config = found[0].get("config")
        config = dict(config) if isinstance(config, dict) else {}
        if config.get(EMBEDDED_WITH_KEY) == identity:
            return
        config[EMBEDDED_WITH_KEY] = identity
        client.table("agent_data_sources").update({"config": config}).eq("source_id", source_id).execute()
    except Exception as exc:  # noqa: BLE001 - a missing stamp only loses a warning
        logger.warning("kb: could not record the embedding model on %s (%s)", source_id, exc)


def _vector_literal(vec: List[float]) -> str:
    """Render a float list as a pgvector text literal: ``[0.1,0.2,...]``."""
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


def _extract_text(filename: str, raw: bytes) -> str:
    """Decode an uploaded file to plain text.

    Supports .md / .txt / .html as utf-8 decode (lossy on errors) and .pdf
    only when pypdf is installed. Returns "" when the file format is
    unsupported so the caller can surface a clear error.
    """
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception:
            raise HTTPException(400, "PDF upload requires pypdf to be installed")
        import io
        try:
            reader = PdfReader(io.BytesIO(raw))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            raise HTTPException(400, f"PDF parse failed for {filename}: {exc}")
    if name.endswith(".docx"):
        try:
            import docx  # python-docx
        except Exception:
            raise HTTPException(400, "DOCX upload requires python-docx to be installed")
        import io
        try:
            document = docx.Document(io.BytesIO(raw))
            return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())
        except Exception as exc:
            raise HTTPException(400, f"DOCX parse failed for {filename}: {exc}")
    # Plain text / markdown / html: utf-8 with replacement to survive odd
    # encodings without 500ing.
    return raw.decode("utf-8", errors="replace")


def _pages_table_for(config: Dict[str, Any]) -> Optional[str]:
    """Resolve the parent pages table from the source config, with a fallback
    for the canonical docs source so the common case works without operators
    having to backfill ``config.pages_table``."""
    pages = config.get("pages_table") if isinstance(config, dict) else None
    if pages:
        return pages
    table = (config or {}).get("table") if isinstance(config, dict) else None
    return _DEFAULT_PAGES_TABLE_FOR.get(table)


@router.post("/kb/{source_id}/upload", name="kb_upload")
async def kb_upload(
    source_id: str,
    request: Request,
    files: List[UploadFile] = File(...),
    title: str = Form(""),
    page_type: str = Form("user_guide"),
    language: str = Form("en"),
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Stage uploaded files, create page rows, enqueue background ingestion.

    Pipeline:
      1. (request thread) read + validate each file's bytes, decode text,
         insert the ``pages_table`` parent row so the embeddings have an
         id to FK against.
      2. (request thread) assemble a job payload and call
         ``start_tracked_job(_kb_ingest_worker, kind='kb_upload', ...)``
         which spawns a daemon thread and returns immediately.
      3. (background) ``_kb_ingest_worker`` chunks → embeds → inserts
         per file with cooperative cancellation between files.

    Returns a 302 to ``/admin/kb/{source_id}?job_id=<id>`` so the operator
    can watch progress in the live panel on the detail page (which polls
    ``GET /admin/jobs/<id>.json`` every few seconds).

    File buffering strategy: bytes are held in memory between the page
    insert and the worker handoff. The hard ``MAX_UPLOAD_BYTES`` cap
    bounds total RSS at ``len(files) * 5MB`` which is fine for the
    operator-driven workload this endpoint targets. Larger payloads
    should land on object storage and be referenced by URL — out of
    scope for this iteration.
    """
    if not files:
        return _redirect_with_error(source_id, "no files uploaded")

    client = admin_service_client()
    rows = _safe_list(
        client.table("agent_data_sources")
        .select("*")
        .eq("source_id", source_id)
        .limit(1)
        .execute()
    )
    if not rows:
        raise HTTPException(404, "data source not found")
    row = rows[0]
    if row.get("kind") != "pgvector":
        raise HTTPException(400, "uploads only supported for pgvector data sources")

    config = row.get("config") or {}
    table_name = config.get("table") if isinstance(config, dict) else None
    if not table_name:
        return _redirect_with_error(source_id, "config.table is not set on this source")

    # Say what is wrong before staging anything: a page row with no passages
    # and a job that "succeeded" with every file failed helps nobody.
    blocker = upload_blocker(client, source_id, config)
    if blocker:
        return _redirect_with_error(source_id, blocker)

    # Validate the embeddings table actually has the columns we'll be writing.
    schema_rows = _inspect_pgvector_table(client, table_name)
    col_names = {c["column_name"] for c in schema_rows}
    if schema_rows:  # only enforce when introspection succeeded
        missing = [c for c in REQUIRED_PGVECTOR_COLUMNS if c not in col_names]
        if missing:
            return _redirect_with_error(
                source_id,
                f"table public.{table_name} is missing required columns: " + ", ".join(missing),
            )

    pages_table = _pages_table_for(config)
    if not pages_table:
        return _redirect_with_error(
            source_id,
            "config.pages_table is not set and no fallback exists for this table",
        )

    # Decode + validate every file BEFORE we touch the DB so a bad PDF
    # in slot N doesn't leave orphan pages from slots 0..N-1.
    staged: List[Dict[str, Any]] = []  # [{filename, text, title}, ...]
    total_bytes = 0
    for upload in files:
        raw = await upload.read()
        if not raw:
            continue
        if len(raw) > MAX_UPLOAD_BYTES:
            return _redirect_with_error(
                source_id,
                f"{upload.filename} exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB cap",
            )
        try:
            text = _extract_text(upload.filename or "", raw)
        except HTTPException as exc:
            return _redirect_with_error(source_id, exc.detail)
        if not text.strip():
            continue
        page_title = (title or "").strip() or (upload.filename or "untitled")
        staged.append(
            {
                "filename": upload.filename or "untitled",
                "text": text,
                "title": page_title,
            }
        )
        total_bytes += len(raw)

    if not staged:
        return _redirect_with_error(source_id, "no decodable content in uploaded files")

    # Insert page rows up-front so the worker has stable FK ids to write
    # embeddings against. A worker crash leaves the page row in place
    # with zero embeddings — the operator can re-run upload, and the
    # next ingestion will create a fresh page (cheap, idempotent).
    file_contents_by_filename: Dict[str, str] = {}
    page_ids_by_filename: Dict[str, Any] = {}
    page_titles_by_filename: Dict[str, str] = {}
    shared = _is_shared_kb(config)
    for item in staged:
        page_payload = {
            "title": item["title"],
            "type": page_type,
            "language": language,
            "content": item["text"],
            "source": "admin_upload",
        }
        if shared:
            page_payload["source_id"] = source_id
        try:
            page_result = client.table(pages_table).insert(page_payload).execute()
        except Exception as exc:
            logger.exception("kb_upload: page insert failed")
            return _redirect_with_error(source_id, f"page insert failed: {exc}")
        page_rows = page_result.data or []
        page_id = (page_rows[0] if page_rows else {}).get("id")
        if not page_id:
            return _redirect_with_error(source_id, "page insert returned no id")
        # The same filename uploaded twice in one request gets disambiguated
        # by suffix so neither page is silently dropped from the payload.
        key = item["filename"]
        if key in page_ids_by_filename:
            key = f"{key}#{len(page_ids_by_filename)}"
        page_ids_by_filename[key] = page_id
        file_contents_by_filename[key] = item["text"]
        page_titles_by_filename[key] = item["title"]

    # Hand off to the background tracker. The worker is launched on a
    # daemon thread by `start_tracked_job` and we return immediately —
    # operators see progress at /admin/kb/{source_id}?job_id=... via the
    # live panel that polls /admin/jobs/{job_id}.json.
    from admin._jobs import start_tracked_job

    label = f"kb:{(row.get('name') or source_id)[:32]}"
    job_id = start_tracked_job(
        _kb_ingest_worker,
        kind="kb_upload",
        label=label,
        actor_user_id=ctx["user_id"],
        related_entity_type="agent_data_sources",
        related_entity_id=source_id,
        metadata={
            "file_count": len(staged),
            "total_bytes": total_bytes,
            "embeddings_table": table_name,
            "pages_table": pages_table,
        },
        source_id=source_id,
        table_name=table_name,
        page_ids_by_filename=page_ids_by_filename,
        file_contents_by_filename=file_contents_by_filename,
        page_titles_by_filename=page_titles_by_filename,
        page_type=page_type,
        language=language,
        max_tokens=int(config.get("chunk_max_tokens") or 512),
        overlap_tokens=int(config.get("chunk_overlap_tokens") or 64),
        embed_model=config.get("embed_model") or "gemini-embedding-001",
    )

    record_change(
        entity_type="agent_data_sources",
        entity_id=source_id,
        action="upload",
        actor_user_id=ctx["user_id"],
        after={"file_count": len(staged), "job_id": job_id},
    )
    return RedirectResponse(
        url=f"/admin/kb/{source_id}?job_id={job_id}",
        status_code=302,
    )


def _kb_ingest_worker(
    source_id: str,
    table_name: str,
    page_ids_by_filename: Dict[str, Any],
    file_contents_by_filename: Dict[str, str],
    page_titles_by_filename: Dict[str, str],
    page_type: str,
    language: str,
    max_tokens: int = 512,
    overlap_tokens: int = 64,
    embed_model: str = "gemini-embedding-001",
    _job_id: Optional[str] = None,
) -> None:
    """Background worker for KB ingestion.

    Called by ``start_tracked_job`` on a daemon thread. For each
    pre-staged file:

      * ``chunk_text`` the buffered content.
      * ``embed_texts`` the chunks (batched inside the embeddings
        client).
      * Bulk insert the embedding rows into ``table_name``.

    Cancellation is checked *between files* — once a file's embed call
    is in flight we let it complete (the Gemini API call isn't
    interruptible from Python anyway) so we don't burn the request and
    end up with half-written embeddings.

    Per-file errors are caught and logged so a single bad file doesn't
    poison the whole job; the worker keeps going and the final metadata
    update reflects what actually committed. The job's terminal status
    stays ``succeeded`` even with file-level failures (mirrors the
    eval-runner convention where partial progress is still progress);
    operators inspect ``metadata.file_results`` to see what landed.
    """
    from admin._jobs import is_cancelled, merge_job_metadata
    from runtime.embeddings import chunk_text, embed_texts

    client = admin_service_client()
    total_chunks = 0
    embed_ms_total = 0
    file_results: List[Dict[str, Any]] = []

    for filename, text in file_contents_by_filename.items():
        # Cooperative cancellation checkpoint — only between files. See
        # docstring for why mid-file isn't supported.
        if _job_id and is_cancelled(_job_id):
            logger.info(
                "kb_ingest[%s]: cancelled before file=%s; %d/%d files done",
                _job_id, filename, len(file_results),
                len(file_contents_by_filename),
            )
            break

        page_id = page_ids_by_filename.get(filename)
        page_title = page_titles_by_filename.get(filename) or filename
        file_result: Dict[str, Any] = {
            "filename": filename,
            "page_id": page_id,
            "chunk_count": 0,
            "status": "ok",
        }

        try:
            chunks = chunk_text(
                text,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                heading=page_title,
            )
            if not chunks:
                file_result["status"] = "empty"
                file_results.append(file_result)
                continue

            t0 = time.perf_counter()
            vectors = embed_texts(
                [c["content"] for c in chunks], model=embed_model
            )
            embed_ms = int((time.perf_counter() - t0) * 1000)
            embed_ms_total += embed_ms

            _shared = table_name == "ai_kb_embeddings"
            rows_to_insert = [
                {
                    "page_id": page_id,
                    "content": c["content"],
                    "heading": c["heading"],
                    "slug": c["slug"],
                    "token_count": c["token_count"],
                    "embedding": _vector_literal(vec),
                    **({"source_id": source_id} if _shared else {}),
                }
                for c, vec in zip(chunks, vectors)
            ]
            client.table(table_name).insert(rows_to_insert).execute()
            if _shared and not total_chunks:
                stamp_embedded_with(client, source_id)

            file_result["chunk_count"] = len(rows_to_insert)
            file_result["embed_ms"] = embed_ms
            total_chunks += len(rows_to_insert)
        except Exception as exc:
            logger.exception(
                "kb_ingest[%s]: file=%s failed; continuing", _job_id, filename
            )
            file_result["status"] = "failed"
            file_result["error"] = f"{type(exc).__name__}: {exc}"

        file_results.append(file_result)

        # Publish progress after each file so the UI poll sees a live
        # counter rather than waiting for the terminal update.
        if _job_id:
            merge_job_metadata(
                _job_id,
                {
                    "chunk_count": total_chunks,
                    "embed_ms_total": embed_ms_total,
                    "files_done": len(file_results),
                    "file_results": file_results,
                },
            )

    # Final metadata stamp — covers the case where we exited the loop
    # without entering it (no files) so the operator still sees a row.
    if _job_id:
        merge_job_metadata(
            _job_id,
            {
                "chunk_count": total_chunks,
                "embed_ms_total": embed_ms_total,
                "files_done": len(file_results),
                "file_results": file_results,
            },
        )


# ---------------------------------------------------------------------------
# POST /admin/kb/{source_id}/delete
# ---------------------------------------------------------------------------


@router.post("/kb/{source_id}/delete", name="kb_delete")
async def kb_delete(
    source_id: str,
    request: Request,
    ctx: dict = Depends(require_superuser),
) -> Response:
    """Delete a knowledge base and cascade-remove its embeddings.

    Steps:
      1. Load the source row for the audit "before" snapshot.
      2. Detach: remove ``source_id`` from every ``agent_profiles.data_source_ids``
         array that contains it, so no agent references a dead KB.
      3. Delete the ``agent_data_sources`` row (FK cascade auto-removes its
         ``ai_kb_pages`` + ``ai_kb_embeddings``).
      4. Write an audit ``record_change``.
      5. Redirect to ``/admin/kb``.

    If the source row is already missing, skip straight to the redirect so
    idempotent re-POSTs from the browser don't 404 the operator.
    """
    client = admin_service_client()

    # Step 1: load the before snapshot (best-effort for audit; soft-missing ok).
    before_rows = _safe_list(
        client.table("agent_data_sources")
        .select("*")
        .eq("source_id", source_id)
        .limit(1)
        .execute()
    )
    if not before_rows:
        # Already gone — redirect cleanly.
        return RedirectResponse(url="/admin/kb", status_code=302)
    before = before_rows[0]

    # Step 2: detach source_id from all referencing agent_profiles.
    all_profiles = _safe_list(
        client.table("agent_profiles").select("*").execute()
    )
    for profile in all_profiles:
        ids: list = list(profile.get("data_source_ids") or [])
        if source_id in ids:
            updated_ids = [i for i in ids if i != source_id]
            client.table("agent_profiles").update(
                {"data_source_ids": updated_ids}
            ).eq("profile_id", profile["profile_id"]).execute()

    # Step 3: delete the source row (cascade removes pages + embeddings).
    client.table("agent_data_sources").delete().eq("source_id", source_id).execute()

    # Step 4: audit.
    record_change(
        entity_type="agent_data_sources",
        entity_id=source_id,
        action="delete",
        actor_user_id=ctx["user_id"],
        before=before,
        after=None,
    )

    return RedirectResponse(url="/admin/kb", status_code=302)


def _redirect_with_error(source_id: str, message: str) -> RedirectResponse:
    """Bounce back to the detail page with a URL-safe error message."""
    return RedirectResponse(
        url=f"/admin/kb/{source_id}?error={quote(message, safe='')}",
        status_code=302,
    )
