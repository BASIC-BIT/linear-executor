import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

# Load env in two layers BEFORE importing any `app.*` module so module-level
# os.environ reads (e.g. PROXY_PROJECT_ID in app.filter, TEAM_ID in
# app.orchestrator) see the .env values. Subprocess CLIs (claude/opencode/
# codex/gemini) inherit the same env so their MCP servers see all provider
# keys + service tokens.
#  1. shared dev-workspace env (~/cc-dev/.env): all AI provider + MCP keys
#  2. executor-local .env: LINEAR_WEBHOOK_SECRET / LINEAR_API_KEY (overrides)
SHARED_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if SHARED_ENV_PATH.exists():
    load_dotenv(dotenv_path=SHARED_ENV_PATH, override=False)
load_dotenv(dotenv_path=ENV_PATH, override=True)

from app import job_registry  # noqa: E402
from app import linear_api  # noqa: E402
from app import queue as q  # noqa: E402
from app.filter import (  # noqa: E402
    is_proxy_ticket,
    should_cancel_run,
    should_complete_review,
    should_start_batch_run,
    should_start_execution,
)
from app.signature import verify_signature  # noqa: E402
from app.worker import Worker  # noqa: E402


def _db_path() -> Path:
    override = os.getenv("LINEAR_EXECUTOR_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "state" / "jobs.db"


def _post_status_comment(
    db_path: Path,
    issue_id: str | None,
    job_id: int,
    stage: str,
    identifier: str,
    logger: logging.Logger,
) -> None:
    """Post the initial 'queued' lifecycle comment and store its id on the job.

    Best-effort: failure to post or persist must not break the webhook.
    The worker will fall back to a fresh comment if status_comment_id is null.
    """
    if not issue_id:
        return
    body = (
        f"⏳ **Linear-Executor** — Queued\n\n"
        f"_stage: `{stage}` • job: {job_id} • ticket: {identifier}_\n"
        f"Waiting for worker..."
    )
    try:
        comment_id = linear_api.post_comment(issue_id, body)
        if comment_id:
            q.set_status_comment_id(db_path, job_id, comment_id)
    except Exception as exc:
        logger.warning("status-comment queued post failed for %s: %s", identifier, exc)


def _configure_logging() -> logging.Logger:
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    logger = logging.getLogger("linear-executor")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

        fh = logging.FileHandler(log_dir / "webhook.log")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger


def create_app() -> FastAPI:
    logger = _configure_logging()
    db_path = _db_path()
    q.init_db(db_path)
    reset = q.reset_stale_running(db_path)
    if reset:
        logger.info("startup — reset %d stale running job(s) to pending", reset)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # General lane: picks up anything (start / proxy / complete).
        # Express lane: proxy-only, so mobile-style Q&A tickets don't wait
        # behind a long-running code task.
        general = Worker(db_path, lane="general")
        express = Worker(db_path, kinds=["proxy"], lane="express")
        general.start()
        express.start()
        logger.info("lifespan — workers started (general + express)")
        try:
            yield
        finally:
            general.stop()
            express.stop()
            logger.info("lifespan — workers stopped")

    app = FastAPI(title="linear-executor", version="0.2.0", lifespan=lifespan)
    app.state.db_path = db_path

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request):
        secret = os.getenv("LINEAR_WEBHOOK_SECRET", "")
        if not secret:
            logger.error("LINEAR_WEBHOOK_SECRET not set — refusing request")
            raise HTTPException(status_code=500, detail="secret not configured")

        raw_body = await request.body()
        header_sig = request.headers.get("linear-signature")

        if not verify_signature(header_sig, raw_body, secret):
            logger.warning(
                "signature verification failed — delivery=%s event=%s",
                request.headers.get("linear-delivery"),
                request.headers.get("linear-event"),
            )
            raise HTTPException(status_code=401, detail="invalid signature")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            logger.error("body is not valid JSON: %s", exc)
            raise HTTPException(status_code=400, detail="body is not valid JSON")

        data = payload.get("data") or {}
        identifier = data.get("identifier") or data.get("id", "?")
        state_name = (data.get("state") or {}).get("name")

        delivery_id = request.headers.get("linear-delivery")
        stage = "ignored"
        job_id: int | None = None

        if should_start_execution(payload):
            kind = "proxy" if is_proxy_ticket(payload) else "start"
            stage = kind if kind == "proxy" else "stage1"
            job_id = q.enqueue(db_path, kind=kind, payload=payload, delivery_id=delivery_id)
            logger.info(
                "ENQUEUED %s — id=%s state=%s delivery=%s job=%d",
                stage.upper(), identifier, state_name, delivery_id, job_id,
            )
            _post_status_comment(db_path, data.get("id"), job_id, stage, identifier, logger)
        elif should_start_batch_run(payload):
            stage = "batch"
            job_id = q.enqueue(db_path, kind="batch", payload=payload, delivery_id=delivery_id)
            logger.info(
                "ENQUEUED BATCH — id=%s state=%s delivery=%s job=%d",
                identifier, state_name, delivery_id, job_id,
            )
            _post_status_comment(db_path, data.get("id"), job_id, stage, identifier, logger)
        elif should_cancel_run(payload):
            from app import cancel as cancel_mod
            stage = "cancel"
            issue_id = data.get("id")
            result = cancel_mod.cancel_ticket(db_path, identifier, issue_id)
            logger.info(
                "CANCELLED — id=%s cancelled_jobs=%d had_running=%s delivery=%s",
                identifier, result["cancelled_jobs"], result["had_running_process"], delivery_id,
            )
        elif should_complete_review(payload):
            stage = "stage2"
            job_id = q.enqueue(db_path, kind="complete", payload=payload, delivery_id=delivery_id)
            logger.info(
                "ENQUEUED STAGE2 — id=%s state=%s delivery=%s job=%d",
                identifier, state_name, delivery_id, job_id,
            )
            _post_status_comment(db_path, data.get("id"), job_id, stage, identifier, logger)
        else:
            logger.info(
                "ignored — id=%s state=%s action=%s delivery=%s",
                identifier, state_name, payload.get("action"), delivery_id,
            )

        return {"received": True, "stage": stage, "id": identifier, "job_id": job_id}

    return app


app = create_app()
