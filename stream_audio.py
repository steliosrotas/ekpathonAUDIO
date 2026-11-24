import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlparse

import vlc
import yt_dlp
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
M3U8_URL_RE = re.compile(r"""https?://[^\s"'<>\\]+\.m3u8[^\s"'<>\\]*""", re.IGNORECASE)


def clean_error(msg: str) -> str:
    return ANSI_ESCAPE_RE.sub("", msg).strip()


def is_stream_url(url: str) -> bool:
    lower = url.lower()
    if re.search(r"\.(ts|m4s|key)(\?|$)", lower):
        return False
    return any(ext in lower for ext in (".m3u8", ".mpd", ".aac", ".mp3", ".opus"))


class AudioStreamPlayer:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Livestream Audio Player")
        self.root.geometry("500x305")
        self.root.resizable(False, False)

        self.vlc_instance = vlc.Instance("--no-video", "--quiet")
        self.player = self.vlc_instance.media_player_new()
        self.player.audio_set_volume(80)

        self._request_id = 0
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Livestream URL:").pack(anchor=tk.W)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(frame, textvariable=self.url_var, width=60)
        self.url_entry.pack(fill=tk.X, pady=(4, 10))
        self.url_entry.bind("<Return>", lambda _e: self.start_stream())
        self.url_entry.focus()

        creds_frame = ttk.LabelFrame(
            frame, text="SSO Login (leave blank for public streams)", padding=8
        )
        creds_frame.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(creds_frame, text="Username:").grid(row=0, column=0, sticky=tk.W, padx=(0, 8))
        self.user_var = tk.StringVar()
        ttk.Entry(creds_frame, textvariable=self.user_var, width=38).grid(
            row=0, column=1, sticky=tk.EW, pady=(0, 6)
        )

        ttk.Label(creds_frame, text="Password:").grid(row=1, column=0, sticky=tk.W, padx=(0, 8))
        self.pass_var = tk.StringVar()
        pass_entry = ttk.Entry(creds_frame, textvariable=self.pass_var, show="*", width=38)
        pass_entry.grid(row=1, column=1, sticky=tk.EW)
        pass_entry.bind("<Return>", lambda _e: self.start_stream())

        creds_frame.columnconfigure(1, weight=1)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(0, 12))

        self.play_btn = ttk.Button(btn_frame, text="Play Audio", command=self.start_stream)
        self.play_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stop_btn = ttk.Button(
            btn_frame, text="Stop", command=self.stop_stream, state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT)

        ttk.Label(btn_frame, text="Volume:").pack(side=tk.LEFT, padx=(24, 6))
        self.volume_var = tk.IntVar(value=80)
        ttk.Scale(
            btn_frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.volume_var,
            command=self.set_volume,
            length=130,
        ).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Enter a livestream URL (and SSO login if required).")
        ttk.Label(
            frame, textvariable=self.status_var, wraplength=460, foreground="#555555"
        ).pack(anchor=tk.W, pady=(2, 0))

    def _set_status(self, text: str, req_id: int):
        def _update():
            if req_id == self._request_id:
                self.status_var.set(text)

        self.root.after(0, _update)

    def start_stream(self):
        url = self.url_var.get().strip()
        username = self.user_var.get().strip()
        password = self.pass_var.get()

        if not url:
            messagebox.showwarning("Missing URL", "Please enter a livestream URL.")
            return

        if not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")

        self.stop_stream()
        self._request_id += 1
        current_req = self._request_id

        self.play_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_var.set("Connecting...")

        thread = threading.Thread(
            target=self._extract_and_play,
            args=(url, username, password, current_req),
            daemon=True,
        )
        thread.start()

    def _extract_and_play(self, url: str, username: str, password: str, req_id: int):
        try:
            if username or password:
                if not (username and password):
                    raise RuntimeError("Please enter both a username and a password.")
                stream_url, title, headers = self._extract_via_browser(
                    url, username, password, req_id
                )
            else:
                self._set_status("Extracting public stream with yt-dlp...", req_id)
                stream_url, title, headers = self._extract_via_ytdlp(url)

            self.root.after(0, self._begin_playback, stream_url, title, headers, req_id)
        except Exception as exc:
            self.root.after(0, self._handle_error, clean_error(str(exc)), req_id)

    def _extract_via_ytdlp(self, url: str):
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
                raise RuntimeError("No playable audio stream was found at that URL.")
            title = info.get("title") or url
            headers = info.get("http_headers") or {}
            return stream_url, title, headers

    def _extract_via_browser(self, url: str, username: str, password: str, req_id: int):
        user_agent = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        captured = {"url": None, "referer": url}
        failed_requests = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=user_agent,
                ignore_https_errors=True,
            )

            # Upgrade any accidental http:// (or internal port) redirects on uoa.gr to https://
            def upgrade_uoa_http(route):
                req_url = route.request.url
                if req_url.startswith("http://"):
                    parsed = urlparse(req_url)
                    host = parsed.hostname or ""
                    if host.endswith(".uoa.gr") or host == "uoa.gr":
                        https_url = parsed._replace(scheme="https", netloc=host).geturl()
                        route.fulfill(status=307, headers={"Location": https_url})
                        return
                route.continue_()

            context.route("http://**/*", upgrade_uoa_http)
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

            def on_request_failed(request):
                failure = request.failure or "unknown error"
                failed_requests.append(f"{request.url} ({failure})")

            page.on("request", on_request)
            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)

            self._set_status("Opening livestream page...", req_id)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Handle Apereo CAS / SSO login redirect
            if "sso." in page.url.lower() or page.locator("input[type='password']").count() > 0:
                self._set_status("Logging into SSO...", req_id)
                user_input = page.locator(
                    "input[name='username'], #username, input[type='text']"
                ).first
                pass_input = page.locator(
                    "input[name='password'], #password, input[type='password']"
                ).first
                submit_btn = page.locator(
                    "button[type='submit'], input[type='submit'], button[name='submit']"
                ).first

                user_input.wait_for(state="visible", timeout=10000)
                user_input.fill(username)
                pass_input.fill(password)
                submit_btn.click()

                # Wait for redirect away from SSO without brittle expect_navigation
                login_deadline = time.time() + 20
                while time.time() < login_deadline:
                    if req_id != self._request_id:
                        browser.close()
                        raise RuntimeError("Cancelled.")
                    if "sso." not in page.url.lower():
                        break
                    page.wait_for_timeout(300)

                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except PlaywrightError:
                    pass

                # If an intermediate redirect failed after setting the cookie, retry the stream URL
                if page.url.startswith("chrome-error://"):
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    except PlaywrightError as exc:
                        details = "\n".join(failed_requests[-5:])
                        raise RuntimeError(
                            f"Connection failed after SSO login:\n{exc}\n\nFailed URLs:\n{details}"
                        ) from exc

                if page.url.startswith("chrome-error://"):
                    details = "\n".join(failed_requests[-5:]) or "No failed URL recorded."
                    raise RuntimeError(
                        f"Navigation failed after SSO login.\n\nFailed URLs:\n{details}"
                    )

                if "sso." in page.url.lower() and page.locator("input[type='password']").count() > 0:
                    extra = (
                        "\n\nFailed URLs:\n" + "\n".join(failed_requests[-5:])
                        if failed_requests
                        else ""
                    )
                    raise RuntimeError(
                        "SSO login failed. Double-check your username and password." + extra
                    )

                # Ensure we land on the exact livestream room (preserving ?id=...)
                parsed_target = urlparse(url)
                target_path = parsed_target.path.rstrip("/")
                target_query = parsed_target.query
                if not captured["url"] and (
                    (target_path and target_path not in page.url)
                    or (target_query and target_query not in page.url)
                ):
                    self._set_status("Navigating to livestream room...", req_id)
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)

            self._set_status("Waiting for livestream audio feed...", req_id)

            clicked_play = False
            deadline = time.time() + 15
            while not captured["url"] and time.time() < deadline:
                if req_id != self._request_id:
                    browser.close()
                    raise RuntimeError("Cancelled.")

                if not clicked_play and (deadline - time.time()) < 11:
                    clicked_play = True
                    for selector in (
                        ".vjs-big-play-button",
                        ".jw-icon-display",
                        "button[aria-label*='Play' i]",
                        "video",
                    ):
                        try:
                            el = page.locator(selector).first
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
            final_url = page.url
            browser.close()

        if not captured["url"]:
            extra = (
                "\n\nFailed URLs:\n" + "\n".join(failed_requests[-5:])
                if failed_requests
                else ""
            )
            raise RuntimeError(
                f"Logged in, but no live audio/video stream was found on:\n{final_url}{extra}"
            )

        headers = {
            "User-Agent": user_agent,
            "Referer": captured["referer"],
        }
        return captured["url"], title, headers

    def _begin_playback(self, stream_url: str, title: str, headers: dict, req_id: int):
        if req_id != self._request_id:
            return

        media = self.vlc_instance.media_new(stream_url)
        if "User-Agent" in headers:
            media.add_option(f":http-user-agent={headers['User-Agent']}")
        if "Referer" in headers:
            media.add_option(f":http-referrer={headers['Referer']}")

        self.player.set_media(media)
        self.player.play()
        self.player.audio_set_volume(int(self.volume_var.get()))

        self.play_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_var.set(f"Playing audio: {title}")

    def _handle_error(self, error_msg: str, req_id: int):
        if req_id != self._request_id:
            return

        self.play_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_var.set("Failed to load stream.")
        messagebox.showerror("Stream Error", f"Could not play stream:\n\n{error_msg}")

    def set_volume(self, value: str):
        self.player.audio_set_volume(int(float(value)))

    def stop_stream(self):
        self._request_id += 1
        self.player.stop()
        self.play_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.status_var.set("Stopped.")

    def on_close(self):
        self.player.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = AudioStreamPlayer(root)
    root.mainloop()
