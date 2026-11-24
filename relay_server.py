import asyncio
import collections
import os
import re
import secrets
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

import uvicorn
import yt_dlp
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from pydantic import BaseModel

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")

# MPEG-1 Layer III @ 48 kHz, 128 kbps => 384 bytes per frame (24 ms)
FRAME_DURATION = 1152 / 48000  # 0.024 s
BATCH_FRAMES = 4               # 0.096 s per broadcast tick
M3U8_URL_RE = re.compile(r"""https?://[^\s"'<>\\]+\.m3u8[^\s"'<>\\]*""", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def is_stream_url(url: str) -> bool:
    lower = url.lower()
    if re.search(r"\.(ts|m4s|key)(\?|$)", lower):
        return False
    return any(ext in lower for ext in (".m3u8", ".mpd", ".aac", ".mp3", ".opus"))


def extract_mp3_frames(buf: bytearray) -> list[bytes]:
    """Parse complete 48 kHz / 128 kbps MPEG-1 Layer III frames out of a byte buffer."""
    frames = []
    i = 0
    n = len(buf)
    while i + 4 <= n:
        if buf[i] == 0xFF and (buf[i + 1] & 0xFE) == 0xFA:
            bitrate_idx = (buf[i + 2] >> 4) & 0x0F
            sr_idx = (buf[i + 2] >> 2) & 0x03
            if bitrate_idx == 9 and sr_idx == 1:  # 128 kbps, 48 kHz
                padding = (buf[i + 2] >> 1) & 1
                frame_len = 384 + padding
                if i + frame_len <= n:
                    frames.append(bytes(buf[i : i + frame_len]))
                    i += frame_len
                    continue
                break
        i += 1
    del buf[:i]
    return frames


def generate_silent_frame() -> bytes:
    """Generate one valid 24 ms silent MP3 frame matching the live encoder settings."""
    out = subprocess.check_output(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "0.5",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "128k",
            "-c:a",
            "libmp3lame",
            "-write_xing",
            "0",
            "-id3v2_version",
            "0",
            "-f",
            "mp3",
            "pipe:1",
        ]
    )
    frames = extract_mp3_frames(bytearray(out))
    if not frames:
        raise RuntimeError("FFmpeg failed to produce silent MP3 frames.")
    return frames[-1]


def extract_via_ytdlp(url: str):
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]
        stream_url = info.get("url")
        if not stream_url:
            raise RuntimeError("No playable audio stream found.")
        return stream_url, (info.get("title") or url), (info.get("http_headers") or {})


def extract_via_browser(url: str, username: str, password: str, status_cb):
    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    captured = {"url": None, "referer": url}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=user_agent, ignore_https_errors=True)

        def upgrade_http(route):
            req_url = route.request.url
            if req_url.startswith("http://"):
                parsed = urlparse(req_url)
                host = parsed.hostname or ""
                if host.endswith(".uoa.gr") or host == "uoa.gr":
                    https_url = parsed._replace(scheme="https", netloc=host).geturl()
                    route.fulfill(status=307, headers={"Location": https_url})
                    return
            route.continue_()

        context.route("http://**/*", upgrade_http)
        page = context.new_page()

        def on_request(request):
            if not captured["url"] and is_stream_url(request.url):
                captured["url"] = request.url
                captured["referer"] = request.headers.get("referer", page.url)

        def on_response(response):
            if captured["url"]:
                return
            ctype = response.headers.get("content-type", "").lower()
            if any(t in ctype for t in ("mpegurl", "dash+xml", "audio/")):
                if not re.search(r"\.(ts|m4s)(\?|$)", response.url.lower()):
                    captured["url"] = response.url
                    captured["referer"] = page.url

        page.on("request", on_request)
        page.on("response", on_response)

        status_cb("Opening livestream page...")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if "sso." in page.url.lower() or page.locator("input[type='password']").count() > 0:
            status_cb("Logging into SSO...")
            page.locator("input[name='username'], #username, input[type='text']").first.fill(
                username
            )
            page.locator("input[name='password'], #password, input[type='password']").first.fill(
                password
            )
            page.locator(
                "button[type='submit'], input[type='submit'], button[name='submit']"
            ).first.click()

            deadline = time.time() + 20
            while time.time() < deadline and "sso." in page.url.lower():
                page.wait_for_timeout(300)

            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightError:
                pass

            if page.url.startswith("chrome-error://"):
                page.goto(url, wait_until="domcontentloaded", timeout=20000)

            if "sso." in page.url.lower() and page.locator("input[type='password']").count() > 0:
                raise RuntimeError("SSO login failed. Check username and password.")

            parsed_target = urlparse(url)
            if not captured["url"] and (
                (parsed_target.path.rstrip("/") not in page.url)
                or (parsed_target.query and parsed_target.query not in page.url)
            ):
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

        status_cb("Waiting for audio feed...")
        deadline = time.time() + 15
        clicked = False
        while not captured["url"] and time.time() < deadline:
            if not clicked and (deadline - time.time()) < 11:
                clicked = True
                for sel in (".vjs-big-play-button", "button[aria-label*='Play' i]", "video"):
                    try:
                        el = page.locator(sel).first
                        if el.is_visible(timeout=500):
                            el.click(timeout=1000)
                            break
                    except Exception:
                        continue
            page.wait_for_timeout(400)

        if not captured["url"]:
            match = M3U8_URL_RE.search(page.content())
            if match:
                captured["url"] = match.group(0)
                captured["referer"] = page.url

        title = page.title() or url
        browser.close()

    if not captured["url"]:
        raise RuntimeError("Logged in, but no live stream was found on that page.")

    return captured["url"], title, {"User-Agent": user_agent, "Referer": captured["referer"]}


class AudioRelay:
    def __init__(self):
        self.lock = threading.Lock()
        self.silent_chunk = generate_silent_frame() * BATCH_FRAMES
        self.live_frames: collections.deque[bytes] = collections.deque()
        self.clients: set[asyncio.Queue[bytes]] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        self.state = "idle"  # idle | connecting | buffering | live | error
        self.message = "No livestream linked — outputting silence."
        self.title = ""
        self.source_url = ""
        self.delay_seconds = 10
        self.target_frames = int(10 / FRAME_DURATION)

        self._job_id = 0
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._running = False

    def start_broadcaster(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._running = True
        threading.Thread(target=self._ticker_loop, daemon=True).start()

    def shutdown(self):
        self._running = False
        self.stop_stream()

    def _kill_ffmpeg_locked(self):
        if self._ffmpeg_proc is not None:
            try:
                self._ffmpeg_proc.kill()
            except Exception:
                pass
            self._ffmpeg_proc = None

    def stop_stream(self):
        with self.lock:
            self._job_id += 1
            self._kill_ffmpeg_locked()
            self.live_frames.clear()
            self.state = "idle"
            self.title = ""
            self.source_url = ""
            self.message = "No livestream linked — outputting silence."

    def start_stream(
        self, url: str, delay_seconds: int = 10, username: str = "", password: str = ""
    ):
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")

        delay_seconds = max(0, min(60, int(delay_seconds)))

        with self.lock:
            self._job_id += 1
            job_id = self._job_id
            self._kill_ffmpeg_locked()
            self.live_frames.clear()
            self.delay_seconds = delay_seconds
            self.target_frames = int(delay_seconds / FRAME_DURATION)
            self.source_url = url
            self.title = ""
            self.state = "connecting"
            self.message = "Extracting stream..."

        threading.Thread(
            target=self._worker,
            args=(job_id, url, username.strip(), password),
            daemon=True,
        ).start()

    def _set_status(self, job_id: int, state: str, message: str, title: Optional[str] = None):
        with self.lock:
            if job_id != self._job_id:
                return
            self.state = state
            self.message = message
            if title is not None:
                self.title = title

    def _worker(self, job_id: int, url: str, username: str, password: str):
        try:
            if username or password:
                if not (username and password):
                    raise RuntimeError("Enter both username and password for SSO login.")
                stream_url, title, headers = extract_via_browser(
                    url,
                    username,
                    password,
                    lambda msg: self._set_status(job_id, "connecting", msg),
                )
            else:
                self._set_status(job_id, "connecting", "Extracting public stream...")
                stream_url, title, headers = extract_via_ytdlp(url)

            with self.lock:
                if job_id != self._job_id:
                    return
                self.title = title
                self.state = "buffering"
                self.message = f"Buffering {self.delay_seconds}s cushion..."

            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
            if headers:
                header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                cmd += ["-headers", header_str]
            cmd += [
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-i",
                stream_url,
                "-vn",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-b:a",
                "128k",
                "-c:a",
                "libmp3lame",
                "-write_xing",
                "0",
                "-id3v2_version",
                "0",
                "-f",
                "mp3",
                "pipe:1",
            ]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with self.lock:
                if job_id != self._job_id:
                    proc.kill()
                    return
                self._ffmpeg_proc = proc

            buf = bytearray()
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                frames = extract_mp3_frames(buf)
                if frames:
                    with self.lock:
                        if job_id != self._job_id:
                            break
                        self.live_frames.extend(frames)

            proc.wait()
            with self.lock:
                if job_id == self._job_id:
                    self._ffmpeg_proc = None
                    self.live_frames.clear()
                    self.state = "error"
                    self.message = "Upstream livestream ended — outputting silence."
        except Exception as exc:
            err = ANSI_RE.sub("", str(exc)).strip()
            self._set_status(job_id, "error", f"Error: {err}")

    def _ticker_loop(self):
        tick_period = BATCH_FRAMES * FRAME_DURATION  # 0.096 s
        next_tick = time.monotonic()

        while self._running:
            now = time.monotonic()
            sleep_time = next_tick - now
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -1.0:
                next_tick = now
            next_tick += tick_period

            with self.lock:
                chunk = self.silent_chunk

                if self.state == "buffering":
                    buffered_s = len(self.live_frames) * FRAME_DURATION
                    if len(self.live_frames) >= self.target_frames:
                        self.state = "live"
                        self.message = f"Live ({self.delay_seconds}s behind)"
                    else:
                        self.message = (
                            f"Buffering ({buffered_s:.1f}s / {self.delay_seconds}s)..."
                        )

                if self.state == "live":
                    if len(self.live_frames) >= BATCH_FRAMES:
                        chunk = b"".join(self.live_frames.popleft() for _ in range(BATCH_FRAMES))
                        max_frames = self.target_frames + int(15 / FRAME_DURATION)
                        if len(self.live_frames) > max_frames:
                            trim_to = self.target_frames + int(5 / FRAME_DURATION)
                            while len(self.live_frames) > trim_to:
                                self.live_frames.popleft()
                    else:
                        self.state = "buffering"
                        self.message = "Re-buffering stream..."

                clients_snapshot = list(self.clients)

            if self.loop and clients_snapshot:
                for q in clients_snapshot:
                    self.loop.call_soon_threadsafe(self._push_to_client, q, chunk)

    @staticmethod
    def _push_to_client(q: asyncio.Queue[bytes], chunk: bytes):
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(chunk)
        except asyncio.QueueFull:
            pass

    def status_dict(self):
        with self.lock:
            return {
                "state": self.state,
                "message": self.message,
                "title": self.title,
                "delay_seconds": self.delay_seconds,
                "buffered_seconds": round(len(self.live_frames) * FRAME_DURATION, 1),
                "listeners": len(self.clients),
            }


relay = AudioRelay()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    relay.start_broadcaster(asyncio.get_running_loop())
    try:
        yield
    finally:
        relay.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartRequest(BaseModel):
    url: str
    delay_seconds: int = 10
    username: str = ""
    password: str = ""


def verify_admin(x_admin_password: Optional[str]):
    if not x_admin_password or not secrets.compare_digest(x_admin_password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Invalid admin password.")


@app.get("/api/status")
async def get_status():
    return relay.status_dict()


@app.post("/api/admin/start")
async def admin_start(
    req: StartRequest, x_admin_password: Optional[str] = Header(default=None)
):
    verify_admin(x_admin_password)
    relay.start_stream(req.url, req.delay_seconds, req.username, req.password)
    return relay.status_dict()


@app.post("/api/admin/stop")
async def admin_stop(x_admin_password: Optional[str] = Header(default=None)):
    verify_admin(x_admin_password)
    relay.stop_stream()
    return relay.status_dict()


@app.get("/audio")
async def audio_stream():
    q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
    with relay.lock:
        relay.clients.add(q)

    async def generator():
        try:
            while True:
                yield await q.get()
        finally:
            with relay.lock:
                relay.clients.discard(q)

    return StreamingResponse(
        generator(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )


docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
if os.path.isdir(docs_dir):
    app.mount("/", StaticFiles(directory=docs_dir, html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
