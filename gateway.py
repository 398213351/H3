"""SiftQ MiniMax-H3 trial -> OpenAI-compatible API gateway.

Wraps the anonymous trial endpoint of siftq.com (reverse-engineered from the
/minimax-h3/try SPA bundle) behind an OpenAI-style API:

  POST /v1/videos              create an image-to-video task (Sora-style)
  GET  /v1/videos/{id}         poll task status
  GET  /v1/videos/{id}/content download the resulting MP4
  POST /v1/chat/completions    compatibility shim: image in -> video URL out
  GET  /v1/models              model list
  GET  /v1/trial/usage         rotator/quota debug info

Quota bypass: the upstream trial quota is keyed on the X-Forwarded-For header
(first hop) plus a client-minted mmtrial_<uuid> client id. Both are fully
attacker-controlled, so every request mints a fresh identity (2 free
generations per forged IP per day) and retries with a new identity on 429.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no python-dotenv dependency)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv(Path(__file__).with_name(".env"))

UPSTREAM = "https://siftq.com"
TRIAL_BASE = f"{UPSTREAM}/api/minimax-trial"
# The plain /video-generation endpoint ignores `prompt` and always applies a
# fixed showcase choreography (that's where the canned dance clip came from).
# /showcase/video-generation honors the caller's prompt; showcase_id is just a
# required reference — the explicit prompt overrides its choreography.
SHOWCASE_ID = os.environ.get("GATEWAY_SHOWCASE_ID", "case-mtqzygu8")  # "Effortless Motion"
DEFAULT_PROMPT = os.environ.get(
    "GATEWAY_DEFAULT_PROMPT",
    "Animate the scene in the image with natural, faithful motion. "
    "Keep the subject and setting consistent with the input image.",
)
REFERER = "https://siftq.com/minimax-h3/try/zh"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DB_PATH = Path(os.environ.get("GATEWAY_DB") or (Path(__file__).parent / "gateway.db"))
# Upstream reaps trial tasks after ~1-2 days ("Trial task was not found or has
# expired"), so finished MP4s are cached locally at success time and /content
# serves the cached copy first — a succeeded task's video can't evaporate.
VIDEOS_DIR = Path(os.environ.get("GATEWAY_VIDEOS_DIR") or (Path(__file__).parent / "videos"))
# Input images are kept so a task killed upstream ("The video could not be
# started" — the fleet flaps) can be resubmitted transparently.
UPLOADS_DIR = Path(os.environ.get("GATEWAY_UPLOADS_DIR") or (Path(__file__).parent / "uploads"))
MAX_TASK_RESUBMITS = int(os.environ.get("GATEWAY_TASK_RESUBMITS", "8"))
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "").strip()
MAX_CONCURRENT = int(os.environ.get("GATEWAY_MAX_CONCURRENT", "4"))  # upstream hard cap is 5
# Quota is keyed on the forged XFF IP (2 uses/day per IP) and identities are
# minted freely, so "quota exhausted" just means "mint another identity".
# Bound the retry loop by wall-clock time instead of a fixed attempt count.
SUBMIT_TIMEOUT = float(os.environ.get("GATEWAY_SUBMIT_TIMEOUT", "900"))
POLL_INTERVAL = float(os.environ.get("GATEWAY_POLL_INTERVAL", "3"))
IMAGE_MAX_BYTES = int(os.environ.get("GATEWAY_IMAGE_MAX_BYTES", str(20 * 1024 * 1024)))
PROXY_LIST = [p.strip() for p in os.environ.get("PROXY_LIST", "").split(",") if p.strip()]

RATIO_ALIASES = {
    "9:16": "9:16", "16:9": "16:9", "1:1": "1:1", "4:3": "4:3", "3:4": "3:4",
    "21:9": "21:9", "9:21": "9:16",
    "720x1280": "9:16", "1080x1920": "9:16", "768x1024": "3:4",
    "1280x720": "16:9", "1920x1080": "16:9", "1024x576": "16:9",
    "1024x1024": "1:1", "512x512": "1:1", "1024x768": "4:3",
}
DURATION_ALIASES = {4: 6, 5: 6, 6: 6, 7: 6, 8: 10, 9: 10, 10: 10, 11: 10, 12: 10,
                    13: 15, 14: 15, 15: 15, 16: 15, 20: 15}


# --------------------------------------------------------------------------
# identity rotation
# --------------------------------------------------------------------------

def random_public_ip() -> str:
    """Random routable IPv4, skipping reserved/private space."""
    while True:
        a = random.randint(1, 223)
        if a in (10, 127):
            continue
        b = random.randint(0, 255)
        if a == 172 and 16 <= b <= 31:
            continue
        if a == 192 and b in (0, 168):
            continue
        if a == 169 and b == 254:
            continue
        if a == 100 and 64 <= b <= 127:
            continue
        if a == 198 and b in (18, 19, 51):
            continue
        if a == 203 and b == 0:
            continue
        return f"{a}.{b}.{random.randint(0, 255)}.{random.randint(1, 254)}"


@dataclass
class Identity:
    client_id: str      # X-MiniMax-Trial-Client header + client_id form field
    visitor_id: str     # visitorId form field
    forged_ip: str      # X-Forwarded-For header (quota key on the server)
    uses_left: int = 2  # anonymous_daily_limit per forged IP

    def headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "X-MiniMax-Trial-Client": self.client_id,
        }
        if not PROXY_LIST:  # with a real proxy the egress IP is the quota key
            h["X-Forwarded-For"] = self.forged_ip
        return h


@dataclass
class Rotator:
    """Mints fresh trial identities; each forged IP is worth 2 generations/day."""

    proxies: list[str] = field(default_factory=list)
    _pool: list[Identity] = field(default_factory=list)
    minted: int = 0
    exhausted: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def mint(self) -> Identity:
        tag = str(uuid.uuid4())
        ident = Identity(
            client_id=f"mmtrial_{tag}",
            visitor_id=f"mmguest_{str(uuid.uuid4())}",
            forged_ip=random_public_ip(),
        )
        self.minted += 1
        return ident

    async def acquire(self) -> Identity:
        async with self._lock:
            while self._pool:
                ident = self._pool.pop()
                if ident.uses_left > 0:
                    return ident
                self.exhausted += 1
            return self.mint()

    async def report(self, ident: Identity, consumed: bool) -> None:
        async with self._lock:
            if consumed:
                ident.uses_left -= 1
            if ident.uses_left > 0:
                self._pool.append(ident)
            else:
                self.exhausted += 1

    def pick_proxy(self) -> str | None:
        return random.choice(self.proxies) if self.proxies else None

    def stats(self) -> dict[str, Any]:
        return {
            "identities_minted": self.minted,
            "identities_exhausted": self.exhausted,
            "pool_ready": len(self._pool),
            "proxy_mode": bool(self.proxies),
        }


ROTATOR = Rotator(proxies=PROXY_LIST)
SUBMIT_GATE = asyncio.Semaphore(MAX_CONCURRENT)


# --------------------------------------------------------------------------
# upstream client
# --------------------------------------------------------------------------

class UpstreamError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def _client(proxy: str | None) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(60, connect=15), "follow_redirects": False}
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


def sniff_image_mime(data: bytes) -> str:
    """Actual bytes decide the part content-type — upstream chokes on a
    mislabeled file (e.g. PNG bytes sent as image/jpeg -> origin 502)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF":
        return "image/webp"
    return "application/octet-stream"


async def upstream_submit(
    image_bytes: bytes, filename: str, ratio: str, duration: int,
    prompt: str | None = None,
) -> dict[str, Any]:
    """POST /api/minimax-trial/showcase/video-generation.

    Retry policy: 429 -> burn the spent identity and mint another (unlimited
    rotation, bounded by SUBMIT_TIMEOUT); 5xx -> backoff (upstream queue
    saturation is transient); other 4xx -> fail fast with the upstream error.
    """
    mime = sniff_image_mime(image_bytes)
    last: UpstreamError | None = None
    deadline = time.monotonic() + SUBMIT_TIMEOUT
    attempt = 0
    auth_fails = 0
    while time.monotonic() < deadline:
        attempt += 1
        ident = await ROTATOR.acquire()
        proxy = ROTATOR.pick_proxy()
        form = {
            "visitorId": ident.visitor_id,
            "channelCode": "direct",
            "sourceHost": "siftq.com",
            "showcase_id": SHOWCASE_ID,
            "ratio": ratio,
            "duration": str(duration),
            "client_id": ident.client_id,
            "prompt": prompt or DEFAULT_PROMPT,
        }
        try:
            async with _client(proxy) as client:
                resp = await client.post(
                    f"{TRIAL_BASE}/showcase/video-generation",
                    headers={**ident.headers(), "Idempotency-Key": f"mmtrial_{uuid.uuid4()}"},
                    data=form,
                    files={"image": (filename, image_bytes, mime)},
                )
            payload = resp.json() if resp.content and "json" in resp.headers.get("content-type", "") else {}
            err_type = payload.get("error", {}).get("type", "")
            if resp.status_code == 429 or err_type == "rate_limit_error":
                # forged IP is spent upstream — burn it and mint another
                ident.uses_left = 0
                await ROTATOR.report(ident, consumed=False)
                last = UpstreamError(429, "rate_limit_error", payload.get("error", {}).get("message", ""))
                print(f"[submit] attempt {attempt}: {last} — rotating", flush=True)
                await asyncio.sleep(random.uniform(0.4, 1.2))
                continue
            if resp.status_code in (401, 403) or err_type == "login_required":
                # upstream occasionally gates an identity/IP with login_required —
                # it's scoped, not global: rotating the identity usually passes.
                auth_fails += 1
                ident.uses_left = 0
                await ROTATOR.report(ident, consumed=False)
                last = UpstreamError(resp.status_code, err_type or "auth_error", payload.get("error", {}).get("message", ""))
                print(f"[submit] attempt {attempt}: {last} — rotating", flush=True)
                if auth_fails >= 6:
                    break  # every fresh identity rejected — policy change, not luck
                await asyncio.sleep(random.uniform(0.4, 1.2))
                continue
            auth_fails = 0
            if resp.status_code == 200:
                remaining = payload.get("remaining", payload.get("remaining_count"))
                if isinstance(remaining, int) and not isinstance(remaining, bool):
                    ident.uses_left = max(0, remaining)  # trust the upstream counter
                    await ROTATOR.report(ident, consumed=False)
                else:
                    await ROTATOR.report(ident, consumed=True)
                return {"identity": ident, "task": payload}
            last = UpstreamError(resp.status_code, "upstream_error", resp.text[:300])
            await ROTATOR.report(ident, consumed=False)
            print(f"[submit] attempt {attempt}: {last}", flush=True)
            if 400 <= resp.status_code < 500:
                break  # not retryable — upstream rejected the request itself
            await asyncio.sleep(min(2.0 * attempt, 8))  # 5xx: back off
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last = UpstreamError(0, "network_error", str(exc))
            await ROTATOR.report(ident, consumed=False)
            await asyncio.sleep(min(1.5 * attempt, 6))
        print(f"[submit] attempt {attempt} failed: {last}", flush=True)
    raise HTTPException(status_code=502, detail=f"upstream submit failed: {last}")


async def upstream_poll(task_id: str, access_token: str, ident: Identity) -> dict[str, Any]:
    async with _client(ROTATOR.pick_proxy()) as client:
        resp = await client.get(
            f"{TRIAL_BASE}/video-generation/{task_id}",
            headers=ident.headers(),
            params={"access_token": access_token},
        )
    if resp.status_code != 200:
        raise UpstreamError(resp.status_code, "poll_error", resp.text[:300])
    return resp.json()


async def upstream_content(task_id: str, access_token: str, ident: Identity) -> tuple[bytes, str]:
    """Fetch the MP4. Returns (bytes, media_type)."""
    async with _client(ROTATOR.pick_proxy()) as client:
        resp = await client.get(
            f"{TRIAL_BASE}/video-generation/{task_id}/content",
            headers=ident.headers(),
            params={"client_id": ident.client_id, "access_token": access_token},
        )
    if resp.status_code != 200:
        raise UpstreamError(resp.status_code, "content_error", resp.text[:300])
    media = resp.headers.get("content-type", "video/mp4").split(";")[0]
    return resp.content, media


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    status: str
    model: str
    ratio: str
    duration: int
    prompt: str | None
    upstream_task_id: str | None = None
    access_token: str | None = None
    client_id: str | None = None
    forged_ip: str | None = None
    video_url: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    attempts: int = 0  # upstream resubmit count


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS tasks(
                 id TEXT PRIMARY KEY, status TEXT, model TEXT, ratio TEXT,
                 duration INTEGER, prompt TEXT, upstream_task_id TEXT,
                 access_token TEXT, client_id TEXT, forged_ip TEXT,
                 video_url TEXT, error TEXT, created_at REAL, updated_at REAL,
                 attempts INTEGER DEFAULT 0)"""
        )
        try:
            self._db.execute("ALTER TABLE tasks ADD COLUMN attempts INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        self._db.commit()

    def save(self, t: Task) -> None:
        self._db.execute(
            """INSERT OR REPLACE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (t.id, t.status, t.model, t.ratio, t.duration, t.prompt, t.upstream_task_id,
             t.access_token, t.client_id, t.forged_ip, t.video_url, t.error,
             t.created_at, t.updated_at, t.attempts),
        )
        self._db.commit()

    def get(self, task_id: str) -> Task | None:
        row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        return Task(*row)

    def all(self) -> list[Task]:
        return [Task(*r) for r in self._db.execute("SELECT * FROM tasks ORDER BY created_at DESC")]


STORE = Store(DB_PATH)


# --------------------------------------------------------------------------
# background poller
# --------------------------------------------------------------------------

def video_cache_path(task_id: str) -> Path:
    return VIDEOS_DIR / f"{task_id}.mp4"


async def cache_video(task: Task) -> None:
    """Persist the finished MP4 locally — upstream deletes trial tasks after
    a day or two, so the canonical copy must live on disk."""
    if not task.upstream_task_id or video_cache_path(task.id).exists():
        return
    ident = Identity(client_id=task.client_id, visitor_id="", forged_ip=task.forged_ip or "")
    try:
        data, _ = await upstream_content(task.upstream_task_id, task.access_token or "", ident)
    except UpstreamError:
        return  # transient miss — /content will retry upstream lazily
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    video_cache_path(task.id).write_bytes(data)


_RETRYABLE_TASK_ERR = re.compile(
    r"could not be started|try again|retryable|overload|timeout|timed out|busy|unavailable",
    re.I,
)


def _retryable_task_error(data: dict[str, Any]) -> bool:
    msg = str(data.get("error") or data.get("error_message") or data.get("message") or data)
    return bool(_RETRYABLE_TASK_ERR.search(msg))


async def _resubmit_task(task: Task) -> bool:
    """Upstream killed the task before/at generation — resubmit the cached
    input image under a fresh identity and keep polling the new task."""
    src = UPLOADS_DIR / task.id
    if not src.exists() or not task.upstream_task_id:
        return False
    try:
        result = await upstream_submit(src.read_bytes(), "upload.jpg", task.ratio, task.duration, task.prompt)
    except Exception as exc:
        task.error = f"resubmit failed: {exc}"
        return False
    ident: Identity = result["identity"]
    td = result["task"]
    task.upstream_task_id = str(td.get("task_id", "")) or task.upstream_task_id
    task.access_token = td.get("access_token") or task.access_token
    task.client_id, task.forged_ip = ident.client_id, ident.forged_ip
    task.attempts += 1
    task.status, task.error = "queued", None
    task.updated_at = time.time()
    STORE.save(task)
    print(f"[poll] {task.id}: upstream task died, resubmitted as {task.upstream_task_id} (#{task.attempts})",
          flush=True)
    return True


async def poll_task(task: Task) -> None:
    while True:
        ident = Identity(client_id=task.client_id, visitor_id="", forged_ip=task.forged_ip or "")
        await asyncio.sleep(POLL_INTERVAL + random.uniform(0, 1))
        try:
            data = await upstream_poll(task.upstream_task_id, task.access_token, ident)
        except UpstreamError as exc:
            task.status = "failed"
            task.error = "upstream task expired" if exc.status == 404 else f"{exc.code}: {exc.message}"
            task.updated_at = time.time()
            STORE.save(task)
            return
        status = data.get("status", task.status)
        task.updated_at = time.time()
        if status == "failed" and _retryable_task_error(data) and task.attempts < MAX_TASK_RESUBMITS:
            # upstream workers flap in minutes-long windows — back off before
            # each resubmit instead of burning all attempts in the same minute
            await asyncio.sleep(min(30 * (task.attempts + 1), 150) + random.uniform(0, 10))
            if await _resubmit_task(task):
                continue  # ride on with the fresh upstream task
        if status in ("succeeded", "failed", "canceled"):
            task.status = "succeeded" if status == "succeeded" else "failed"
            if task.status == "succeeded":
                await cache_video(task)
            else:
                task.error = json.dumps(data)[:500]
            STORE.save(task)
            (UPLOADS_DIR / task.id).unlink(missing_ok=True)  # terminal — drop the input copy
            return
        task.status = "queued" if status == "queued" else "running"
        STORE.save(task)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def normalize_ratio(value: str | None) -> str:
    """The showcase channel (the only one honoring `prompt`) is 9:16-only
    upstream — reject anything else instead of silently producing vertical."""
    if not value:
        return "9:16"
    key = value.strip().lower().replace(" ", "")
    if RATIO_ALIASES.get(key) != "9:16":
        raise HTTPException(400, "only 9:16 vertical output is supported by the trial channel")
    return "9:16"


def normalize_duration(value: Any, model: str | None) -> int:
    if model:
        if model.endswith("-10s"):
            return 10
        if model.endswith("-15s"):
            return 15
    if value is None:
        return 6
    try:
        seconds = int(float(str(value)))
    except ValueError:
        raise HTTPException(400, f"invalid duration {value!r}")
    if seconds not in DURATION_ALIASES:
        raise HTTPException(400, f"unsupported duration {seconds}s; trial supports 6s, 10s and 15s")
    return DURATION_ALIASES[seconds]


def openai_error(message: str, code: str = "bad_request", status: int = 400) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error", "code": code}}, status_code=status)


async def fetch_image(source: str) -> tuple[bytes, str]:
    """image may be an http(s) URL or a data: URL."""
    if source.startswith("data:"):
        m = re.match(r"data:(?P<mime>[\w/+.-]+);base64,(?P<b64>.+)", source, re.S)
        if not m:
            raise HTTPException(400, "malformed data URL")
        try:
            data = base64.b64decode(m.group("b64"), validate=False)
        except binascii.Error:
            raise HTTPException(400, "invalid base64 image payload")
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(m.group("mime"), "jpg")
        return data, f"upload.{ext}"
    if source.startswith(("http://", "https://")):
        async with _client(None) as client:
            resp = await client.get(source)
        if resp.status_code != 200:
            raise HTTPException(400, f"cannot fetch image_url: HTTP {resp.status_code}")
        return resp.content, Path(source.split("?")[0]).name or "upload.jpg"
    raise HTTPException(400, "image must be an http(s) URL or base64 data URL")


def check_image(data: bytes) -> None:
    if len(data) > IMAGE_MAX_BYTES:
        raise HTTPException(413, f"image too large ({len(data)} bytes > {IMAGE_MAX_BYTES})")
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"RIFF"):
        raise HTTPException(400, "image must be JPEG, PNG or WebP")


# Browsers that load the bundled UI get this cookie; /v1/* accepts either the
# Bearer key (programmatic access) or the session cookie (same-site web UI).
SESSION_COOKIE = "h3_sess"
# Derived from the API key so restarts don't invalidate cookies already held by
# open browser tabs (a random token would 401 every session after each restart).
SESSION_TOKEN = (
    "h3sess-" + hashlib.sha256(f"h3sess:{GATEWAY_API_KEY}".encode()).hexdigest()
    if GATEWAY_API_KEY else secrets.token_urlsafe(24)
)


async def require_key(request: Request, authorization: str = Header(default="")) -> None:
    if not GATEWAY_API_KEY:
        return
    if authorization == f"Bearer {GATEWAY_API_KEY}":
        return
    if request.cookies.get(SESSION_COOKIE) == SESSION_TOKEN:
        return
    raise HTTPException(401, "invalid gateway API key")


def task_to_video_obj(t: Task, base_url: str) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": t.id,
        "object": "video",
        "model": t.model,
        "status": t.status,
        "progress": 100 if t.status == "succeeded" else 0,
        "created": int(t.created_at),
        "ratio": t.ratio,
        "seconds": str(t.duration),
        "failure_reason": t.error,
    }
    if t.status == "succeeded":
        obj["video_url"] = f"{base_url}/v1/videos/{t.id}/content"
    return obj


def base_url_of(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_: FastAPI):
    for t in STORE.all():
        if t.status in ("queued", "running") and t.upstream_task_id and t.access_token:
            asyncio.create_task(poll_task(t))
    yield


app = FastAPI(title="SiftQ MiniMax-H3 OpenAI Gateway", version="1.0.0", lifespan=lifespan)

# The bundled H3 Studio web UI is served same-origin; CORS stays permissive so
# the frontend can also be opened from a separate dev server if needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = Path(__file__).parent / "web"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models", dependencies=[Depends(require_key)])
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": "minimax-h3", "object": "model", "created": 1, "owned_by": "siftq-trial",
             "description": "MiniMax H3 image-to-video, 6s (anonymous trial)"},
            {"id": "minimax-h3-10s", "object": "model", "created": 1, "owned_by": "siftq-trial",
             "description": "MiniMax H3 image-to-video, 10s (anonymous trial)"},
            {"id": "minimax-h3-15s", "object": "model", "created": 1, "owned_by": "siftq-trial",
             "description": "MiniMax H3 image-to-video, 15s (anonymous trial)"},
        ],
    }


@app.get("/v1/trial/usage", dependencies=[Depends(require_key)])
async def trial_usage() -> dict[str, Any]:
    stats = ROTATOR.stats()
    stats["tasks"] = len(STORE.all())
    return stats


@app.post("/v1/videos", dependencies=[Depends(require_key)])
async def create_video(request: Request) -> JSONResponse:
    """OpenAI Sora-style video creation. JSON body or multipart form."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        image_file = form.get("image")
        if not isinstance(image_file, UploadFile):
            return openai_error("multipart request needs an 'image' file field")
        image_bytes = await image_file.read()
        filename = image_file.filename or "upload.jpg"
        model = form.get("model") or "minimax-h3"
        ratio = normalize_ratio(form.get("size") or form.get("ratio"))
        duration = normalize_duration(form.get("seconds") or form.get("duration"), model)
        prompt = form.get("prompt") or None
    else:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return openai_error("body must be JSON or multipart/form-data")
        model = body.get("model") or "minimax-h3"
        ratio = normalize_ratio(body.get("size") or body.get("ratio"))
        duration = normalize_duration(body.get("seconds") or body.get("duration"), model)
        prompt = body.get("prompt")
        image_ref = body.get("image_url") or body.get("image")
        if isinstance(image_ref, dict):  # OpenAI style {"url": "..."}
            image_ref = image_ref.get("url")
        if not image_ref:
            return openai_error("image (URL/base64) is required: trial is image-to-video only")
        image_bytes, filename = await fetch_image(image_ref)

    check_image(image_bytes)

    async with SUBMIT_GATE:
        result = await upstream_submit(image_bytes, filename, ratio, duration, prompt)
    ident: Identity = result["identity"]
    task_data: dict[str, Any] = result["task"]

    task = Task(
        id=f"video_{uuid.uuid4().hex}",
        status="queued",
        model=model,
        ratio=ratio,
        duration=duration,
        prompt=prompt,
        upstream_task_id=str(task_data.get("task_id", "")),
        access_token=task_data.get("access_token"),
        client_id=ident.client_id,
        forged_ip=ident.forged_ip,
    )
    STORE.save(task)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / task.id).write_bytes(image_bytes)  # enables transparent resubmit
    asyncio.create_task(poll_task(task))

    return JSONResponse(task_to_video_obj(task, base_url_of(request)), status_code=202)


@app.get("/v1/videos/{video_id}", dependencies=[Depends(require_key)])
async def get_video(video_id: str, request: Request) -> dict[str, Any]:
    task = STORE.get(video_id)
    if not task:
        raise HTTPException(404, "video task not found")
    return task_to_video_obj(task, base_url_of(request))


@app.get("/v1/videos/{video_id}/content", dependencies=[Depends(require_key)])
async def get_video_content(video_id: str):
    task = STORE.get(video_id)
    if not task:
        raise HTTPException(404, "video task not found")
    if task.status != "succeeded":
        raise HTTPException(409, f"task is {task.status}, not ready")
    cached = video_cache_path(task.id)
    if cached.exists():
        return FileResponse(cached, media_type="video/mp4", filename=f"{video_id}.mp4")
    ident = Identity(client_id=task.client_id, visitor_id="", forged_ip=task.forged_ip)
    try:
        data, media = await upstream_content(task.upstream_task_id, task.access_token, ident)
    except UpstreamError as exc:
        if exc.status == 404:
            raise HTTPException(404, "upstream expired this video; please regenerate")
        raise HTTPException(502, f"upstream content fetch failed: {exc.code} {exc.message}")
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)  # stash for later — upstream will reap the task
    return StreamingResponse(iter([data]), media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{video_id}.mp4"',
        "Content-Length": str(len(data)),
    })


@app.post("/v1/chat/completions", dependencies=[Depends(require_key)])
async def chat_completions(request: Request):
    """Compatibility shim: image in the last user message -> generated video URL."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return openai_error("body must be JSON")

    messages = body.get("messages") or []
    if not messages:
        return openai_error("messages[] is required")
    last = messages[-1]
    content = last.get("content")
    text_parts: list[str] = []
    image_ref: str | None = None
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
            elif part.get("type") == "image_url":
                url = part.get("image_url")
                image_ref = url.get("url") if isinstance(url, dict) else url
    if not image_ref:
        return openai_error(
            "no image found in the last user message; the MiniMax-H3 trial is "
            "image-to-video only (send an image_url part)"
        )
    prompt = " ".join(p for p in text_parts if p).strip() or None

    video_cfg = body.get("video") or {}
    model = body.get("model") or video_cfg.get("model") or "minimax-h3"
    ratio = normalize_ratio(video_cfg.get("size") or video_cfg.get("ratio") or body.get("size"))
    duration = normalize_duration(
        video_cfg.get("seconds") or video_cfg.get("duration") or body.get("seconds"), model
    )
    wait_seconds = float(body.get("wait_seconds", video_cfg.get("wait_seconds", 0)) or 0)

    image_bytes, filename = await fetch_image(image_ref)
    check_image(image_bytes)
    async with SUBMIT_GATE:
        result = await upstream_submit(image_bytes, filename, ratio, duration, prompt)
    ident: Identity = result["identity"]
    task = Task(
        id=f"video_{uuid.uuid4().hex}",
        status="queued",
        model=model,
        ratio=ratio,
        duration=duration,
        prompt=prompt,
        upstream_task_id=str(result["task"].get("task_id", "")),
        access_token=result["task"].get("access_token"),
        client_id=ident.client_id,
        forged_ip=ident.forged_ip,
    )
    STORE.save(task)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / task.id).write_bytes(image_bytes)
    asyncio.create_task(poll_task(task))

    base = base_url_of(request)
    content_url = f"{base}/v1/videos/{task.id}/content"
    note = ""
    message = (
        f"Video generation submitted.\n\n"
        f"- task: `{task.id}` (status: queued, {duration}s, {ratio})\n"
        f"- download: {content_url}\n"
        f"- poll: GET {base}/v1/videos/{task.id}{note}"
    )

    if wait_seconds > 0:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            task = STORE.get(task.id) or task
            if task.status in ("succeeded", "failed"):
                break
        if task.status == "succeeded":
            message = f"Video ready.\n\n- download: {content_url}\n- task: `{task.id}`{note}"
        elif task.status == "failed":
            message = f"Video generation failed: {task.error}"

    if body.get("stream"):
        async def event_stream():
            chunk = lambda delta, finish=None: (
                "data: " + json.dumps({
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }) + "\n\n"
            )
            yield chunk({"role": "assistant"})
            for line in message.split("\n"):
                yield chunk({"content": line + "\n"})
            yield chunk({}, "stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": message},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "video": task_to_video_obj(task, base),
    }


# H3 Studio frontend — explicit index route mints the web session cookie;
# everything else falls through to the static mount (registered last so
# /v1/* routes always win).
@app.get("/", include_in_schema=False)
async def index_page():
    resp = FileResponse(WEB_DIR / "index.html")
    resp.set_cookie(SESSION_COOKIE, SESSION_TOKEN, httponly=True, samesite="lax",
                    max_age=30 * 24 * 3600)
    return resp


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="h3-studio")


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
                port=int(os.environ.get("GATEWAY_PORT", "8787")))
