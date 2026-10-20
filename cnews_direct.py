#!/usr/bin/env python3
import collections
import gzip
import http.client
import http.cookiejar
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tkinter as tk

VIDEO_ID = "x3b68jn"
METADATA_URL = f"https://www.dailymotion.com/player/metadata/video/{VIDEO_ID}"
PLAYER_REFERER = "https://www.dailymotion.com/"
PLAYER_ORIGIN = "https://www.dailymotion.com"
EUROPE1_STREAM_URL = "https://europe1.lmn.fm/europe1.mp3"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
DEFAULT_MAX_HEIGHT = int(os.environ.get("CNEWS_MAX_HEIGHT", "720"))
TOKEN_REFRESH_SECONDS = 25 * 60
STALL_SECONDS = 45
CONTROLS_HIDE_DELAY_MS = 3000
PLAYLIST_WINDOW_SEGMENTS = 24
PREFETCH_SEGMENTS = 5
MAX_CACHED_SEGMENTS = 28
HTTP_TIMEOUT = 4.0

ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')

CHANNELS = {
    "cnews_tv": {
        "title": "CNEWS Direct — TV",
        "label_full": "📺 CNEWS TV",
        "label_compact": "📺 TV",
        "accent": "#c92a2a",
        "is_audio_only": False,
    },
    "cnews_audio": {
        "title": "CNEWS Direct — Audio",
        "label_full": "🎧 CNEWS Audio",
        "label_compact": "🎧 CNEWS",
        "accent": "#b91c1c",
        "is_audio_only": True,
        "card_icon": "🎧",
        "card_title": "CNEWS — Audio Direct",
        "card_subtitle": "Flux audio seul (économie de bande passante et batterie)",
    },
    "europe1": {
        "title": "Europe 1 — Direct",
        "label_full": "📻 Europe 1",
        "label_compact": "📻 Europe 1",
        "accent": "#1d4ed8",
        "is_audio_only": True,
        "card_icon": "📻",
        "card_title": "Europe 1 — En Direct",
        "card_subtitle": "Flux radio en direct",
    },
}

QUALITY_OPTIONS = [
    (720, "720p"),
    (480, "480p"),
    (380, "380p"),
]


class StreamError(Exception):
    pass


def parse_attrs(text):
    return {m.group(1): m.group(2).strip('"') for m in ATTR_RE.finditer(text)}


class PersistentFetcher:
    """Thread-safe HTTPS client with Keep-Alive, gzip support, short timeouts, and multi-IP retry."""

    def __init__(self, timeout=HTTP_TIMEOUT):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._conns = {}
        self._ssl_ctx = ssl.create_default_context()
        try:
            # Avoid oversized multi-packet Post-Quantum (ML-KEM) TLS 1.3 ClientHello on Wi-Fi
            self._ssl_ctx.set_ecdh_curve("X25519")
        except Exception:
            pass

    def _acquire(self, host, port, is_https):
        key = (host, port, is_https)
        with self._lock:
            pool = self._conns.get(key)
            if pool:
                return pool.pop()
        if is_https:
            return http.client.HTTPSConnection(
                host, port, timeout=self.timeout, context=self._ssl_ctx
            )
        return http.client.HTTPConnection(host, port, timeout=self.timeout)

    def _release(self, host, port, is_https, conn):
        key = (host, port, is_https)
        with self._lock:
            pool = self._conns.setdefault(key, [])
            if len(pool) < 6:
                pool.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass

    def fetch_With_headers(self, url, cookies="", attempts=4):
        parsed = urllib.parse.urlsplit(url.split("#", 1)[0])
        is_https = parsed.scheme == "https"
        host = parsed.hostname
        port = parsed.port or (443 if is_https else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "Referer": PLAYER_REFERER,
            "Origin": PLAYER_ORIGIN,
            "Connection": "keep-alive",
        }
        if cookies:
            headers["Cookie"] = cookies

        last_exc = None
        for attempt in range(attempts):
            conn = self._acquire(host, port, is_https)
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                status = resp.status
                resp_headers = resp.getheaders()
                encoding = resp.getheader("Content-Encoding", "")
                if status == 200:
                    self._release(host, port, is_https, conn)
                    if encoding == "gzip":
                        data = gzip.decompress(data)
                    return data, resp_headers
                try:
                    conn.close()
                except Exception:
                    pass
                last_exc = StreamError(f"HTTP {status} sur {path}")
                if status == 403:
                    getattr(last_exc, "__dict__", {})["code"] = 403
            except Exception as exc:
                last_exc = exc
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt + 1 < attempts:
                time.sleep(0.1 * (attempt + 1))
        raise last_exc or StreamError("Échec de téléchargement")

    def fetch_bytes(self, url, cookies="", attempts=4):
        data, _ = self.fetch_With_headers(url, cookies=cookies, attempts=attempts)
        return data

    def fetch_text(self, url, cookies="", attempts=4):
        return self.fetch_bytes(url, cookies=cookies, attempts=attempts).decode(
            "utf-8", "replace"
        )


GLOBAL_FETCHER = PersistentFetcher()


def http_get(url, cookies="", referer=PLAYER_REFERER, origin=PLAYER_ORIGIN):
    return GLOBAL_FETCHER.fetch_text(url, cookies=cookies, attempts=4)


def fetch_master():
    last_error = None
    for delay in (0, 0.5, 1.5, 3):
        if delay:
            time.sleep(delay)
        try:
            raw_meta, meta_headers = GLOBAL_FETCHER.fetch_With_headers(
                METADATA_URL, attempts=3
            )
            data = json.loads(raw_meta.decode("utf-8", "replace"))
            master_url = data["qualities"]["auto"][0]["url"]
            cookies_list = []
            for k, v in meta_headers:
                if k.lower() == "set-cookie":
                    cookie_pair = v.split(";", 1)[0].strip()
                    if cookie_pair:
                        cookies_list.append(cookie_pair)
            cookies = "; ".join(cookies_list)
            return http_get(master_url, cookies)
        except Exception as exc:
            last_error = exc
    if getattr(last_error, "code", None) == 403:
        raise StreamError(
            "Le serveur limite les requêtes. Réessayez dans une minute."
        )
    raise StreamError(f"Impossible de récupérer le flux ({last_error})")


def extract_cnews_audio_url(master_text):
    lines = [line.strip() for line in master_text.splitlines()]
    audio = {}
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            attrs = parse_attrs(line.split(":", 1)[1])
            audio.setdefault(attrs.get("GROUP-ID", ""), []).append(
                attrs.get("URI", "")
            )
    audio_urls = [
        url
        for urls in audio.values()
        for url in urls
        if url.startswith("http") and "backup" not in url
    ]
    if not audio_urls:
        audio_urls = [
            url
            for urls in audio.values()
            for url in urls
            if url.startswith("http")
        ]
    if not audio_urls:
        raise StreamError("Aucune piste audio disponible.")
    preferred = [url for url in audio_urls if "live-aac-128" in url]
    return (preferred or audio_urls)[0]


def select_streams(master_text, max_height=DEFAULT_MAX_HEIGHT):
    lines = [line.strip() for line in master_text.splitlines()]
    audio = {}
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line:
            attrs = parse_attrs(line.split(":", 1)[1])
            audio.setdefault(attrs.get("GROUP-ID", ""), []).append(
                attrs.get("URI", "")
            )

    variants = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = parse_attrs(line.split(":", 1)[1])
        if index + 1 >= len(lines):
            continue
        uri = lines[index + 1]
        if not uri.startswith("http"):
            continue
        if "backup" in attrs.get("AUDIO", ""):
            continue
        resolution = attrs.get("RESOLUTION", "")
        height = int(resolution.split("x")[1]) if "x" in resolution else 0
        if height > max_height:
            continue
        bandwidth = int(attrs.get("BANDWIDTH", "0"))
        variants.append(((height, bandwidth), attrs, uri))

    if not variants:
        raise StreamError("Aucune piste vidéo disponible.")

    _, attrs, video_url = max(variants, key=lambda item: item[0])
    group = attrs.get("AUDIO", "")
    audio_urls = [url for url in audio.get(group, []) if url.startswith("http")]
    if not audio_urls:
        audio_urls = [
            url
            for urls in audio.values()
            for url in urls
            if url.startswith("http") and "backup" not in url
        ]
    if not audio_urls:
        raise StreamError("Aucune piste audio disponible.")

    preferred = [url for url in audio_urls if "live-aac-128" in url]
    audio_url = (preferred or audio_urls)[0]
    return attrs, video_url, audio_url


def build_playlist(master_text, max_height=DEFAULT_MAX_HEIGHT):
    attrs, video_url, audio_url = select_streams(master_text, max_height=max_height)
    playlist = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        (
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="fre",'
            'DEFAULT=YES,CHANNELS="2",URI="%s"' % audio_url
        ),
        (
            '#EXT-X-STREAM-INF:BANDWIDTH=%s,RESOLUTION=%s,CODECS="%s",AUDIO="aud"'
            % (
                attrs.get("BANDWIDTH", "0"),
                attrs.get("RESOLUTION", ""),
                attrs.get("CODECS", ""),
            )
        ),
        video_url,
        "",
    ]
    return "\n".join(playlist)


class HLSProxyState:
    """Proxies and trims Dailymotion's huge 4-hour M3U8 playlists, caches init MP4s in RAM,
    and prefetches audio/video segments in parallel over persistent connections."""

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.lock = threading.Lock()
        self.base_local_url = ""
        self.custom_master_bytes = None
        self.stream_attrs = {}
        self.upstream_urls = {"video": "", "audio": ""}
        self.base_urls = {"video": "", "audio": ""}
        self.playlists = {"video": b"", "audio": b""}
        self.playlist_updated_at = {"video": 0.0, "audio": 0.0}
        self.init_cache = {}
        self.seg_cache = collections.OrderedDict()
        self.inflight = {}
        self.active = False
        self.audio_only = False
        self.generation = 0

    def configure_custom(self, master_text):
        with self.lock:
            self.generation += 1
            self.active = False
            self.custom_master_bytes = master_text.encode("utf-8")

    def configure_streams(self, attrs, video_url, audio_url, audio_only=False):
        v_clean = video_url.split("#", 1)[0]
        a_clean = audio_url.split("#", 1)[0]
        v_base = v_clean.rsplit("/", 1)[0] + "/"
        a_base = a_clean.rsplit("/", 1)[0] + "/"
        with self.lock:
            self.generation += 1
            gen = self.generation
            self.custom_master_bytes = None
            self.stream_attrs = dict(attrs or {})
            self.upstream_urls["video"] = v_clean
            self.upstream_urls["audio"] = a_clean
            self.base_urls["video"] = v_base
            self.base_urls["audio"] = a_base
            self.playlist_updated_at["video"] = 0.0
            self.playlist_updated_at["audio"] = 0.0
            self.active = True
            self.audio_only = audio_only

        # Warm up initial playlists + latest segments before mpv connects
        threads = [
            threading.Thread(
                target=self.refresh_kind, args=("audio", gen, True), daemon=True
            )
        ]
        if not audio_only and v_clean:
            threads.append(
                threading.Thread(
                    target=self.refresh_kind, args=("video", gen, True), daemon=True
                )
            )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=4.5)

    def update_upstream_urls(self, video_url, audio_url):
        """Seamlessly updates CDN token URLs without interrupting mpv."""
        with self.lock:
            if not self.active:
                return
            if video_url:
                v_clean = video_url.split("#", 1)[0]
                self.upstream_urls["video"] = v_clean
                self.base_urls["video"] = v_clean.rsplit("/", 1)[0] + "/"
            if audio_url:
                a_clean = audio_url.split("#", 1)[0]
                self.upstream_urls["audio"] = a_clean
                self.base_urls["audio"] = a_clean.rsplit("/", 1)[0] + "/"

    def stop(self):
        with self.lock:
            self.generation += 1
            self.active = False
            self.seg_cache.clear()
            self.init_cache.clear()

    def get_master_playlist(self):
        with self.lock:
            if self.custom_master_bytes is not None:
                return self.custom_master_bytes
            attrs = self.stream_attrs
            base = self.base_local_url
        playlist = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            (
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="fre",'
                f'DEFAULT=YES,CHANNELS="2",URI="{base}/audio.m3u8"'
            ),
            (
                '#EXT-X-STREAM-INF:BANDWIDTH=%s,RESOLUTION=%s,CODECS="%s",AUDIO="aud"'
                % (
                    attrs.get("BANDWIDTH", "2691072"),
                    attrs.get("RESOLUTION", "1280x720"),
                    attrs.get("CODECS", "avc1.640020,mp4a.40.2"),
                )
            ),
            f"{base}/video.m3u8",
            "",
        ]
        return "\n".join(playlist).encode("utf-8")

    def get_media_playlist(self, kind):
        with self.lock:
            gen = self.generation
            age = time.time() - self.playlist_updated_at.get(kind, 0.0)
            cached = self.playlists.get(kind, b"")
        if not cached or age > 1.2:
            self.refresh_kind(kind, gen, sync_prefetch=not bool(cached))
            with self.lock:
                cached = self.playlists.get(kind, b"")
        return cached

    def refresh_kind(self, kind, gen=None, sync_prefetch=False):
        with self.lock:
            if gen is not None and gen != self.generation:
                return
            upstream = self.upstream_urls.get(kind, "")
            local_base = self.base_local_url
        if not upstream:
            return
        try:
            raw_text = self.fetcher.fetch_text(upstream, attempts=2)
        except Exception:
            return

        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        target_dur = "3"
        version = "6"
        media_seq = 0
        init_uri = None
        segments = []
        cur_extinf = None

        for line in lines:
            if line.startswith("#EXT-X-VERSION:"):
                version = line.split(":", 1)[1].strip()
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                target_dur = line.split(":", 1)[1].strip()
            elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    media_seq = int(line.split(":", 1)[1].strip())
                except ValueError:
                    media_seq = 0
            elif line.startswith("#EXT-X-MAP:"):
                attrs = parse_attrs(line.split(":", 1)[1])
                init_uri = attrs.get("URI", "")
            elif line.startswith("#EXTINF:"):
                cur_extinf = line
            elif not line.startswith("#"):
                if cur_extinf is not None:
                    segments.append((cur_extinf, line))
                    cur_extinf = None

        if not segments:
            return

        total = len(segments)
        keep = min(total, PLAYLIST_WINDOW_SEGMENTS)
        trimmed = segments[-keep:]
        new_seq = media_seq + (total - keep)

        out = [
            "#EXTM3U",
            f"#EXT-X-VERSION:{version}",
            f"#EXT-X-TARGETDURATION:{target_dur}",
            f"#EXT-X-MEDIA-SEQUENCE:{new_seq}",
        ]
        if init_uri:
            init_name = init_uri.split("/")[-1].split("?")[0]
            out.append(f'#EXT-X-MAP:URI="{local_base}/init/{kind}/{init_name}"')

        for extinf, seg_uri in trimmed:
            seg_name = seg_uri.split("/")[-1].split("?")[0]
            out.append(extinf)
            out.append(f"{local_base}/seg/{kind}/{seg_name}")
        out.append("")
        body = "\n".join(out).encode("utf-8")

        with self.lock:
            if gen is not None and gen != self.generation:
                return
            self.playlists[kind] = body
            self.playlist_updated_at[kind] = time.time()

        # Prefetch init section and latest segments
        to_prefetch = [
            seg_uri.split("/")[-1].split("?")[0]
            for _, seg_uri in trimmed[-PREFETCH_SEGMENTS:]
        ]
        if sync_prefetch:
            pre_threads = []
            if init_uri:
                init_name = init_uri.split("/")[-1].split("?")[0]
                pre_threads.append(
                    threading.Thread(
                        target=self.get_init_bytes, args=(kind, init_name), daemon=True
                    )
                )
            for sname in to_prefetch:
                pre_threads.append(
                    threading.Thread(
                        target=self.get_segment_bytes, args=(kind, sname), daemon=True
                    )
                )
            for pt in pre_threads:
                pt.start()
            for pt in pre_threads[:4]:
                pt.join(timeout=2.5)
        else:
            if init_uri:
                init_name = init_uri.split("/")[-1].split("?")[0]
                threading.Thread(
                    target=self.get_init_bytes, args=(kind, init_name), daemon=True
                ).start()
            for sname in to_prefetch[-2:]:
                threading.Thread(
                    target=self.get_segment_bytes, args=(kind, sname), daemon=True
                ).start()

    def get_init_bytes(self, kind, name):
        key = (kind, name)
        with self.lock:
            if key in self.init_cache:
                return self.init_cache[key]
            base = self.base_urls.get(kind, "")
        if not base:
            return None
        url = urllib.parse.urljoin(base, name)
        try:
            data = self.fetcher.fetch_bytes(url, attempts=3)
        except Exception:
            return None
        with self.lock:
            self.init_cache[key] = data
        return data

    def get_segment_bytes(self, kind, name):
        key = (kind, name)
        with self.lock:
            if key in self.seg_cache:
                self.seg_cache.move_to_end(key)
                return self.seg_cache[key]
            ev = self.inflight.get(key)
            if ev is None:
                ev = threading.Event()
                self.inflight[key] = ev
                is_leader = True
            else:
                is_leader = False
            base = self.base_urls.get(kind, "")

        if not is_leader:
            ev.wait(timeout=6.0)
            with self.lock:
                return self.seg_cache.get(key)

        data = None
        try:
            if base:
                url = urllib.parse.urljoin(base, name)
                data = self.fetcher.fetch_bytes(url, attempts=3)
                with self.lock:
                    self.seg_cache[key] = data
                    self.seg_cache.move_to_end(key)
                    while len(self.seg_cache) > MAX_CACHED_SEGMENTS:
                        self.seg_cache.popitem(last=False)
        except Exception:
            data = None
        finally:
            with self.lock:
                self.inflight.pop(key, None)
            ev.set()
        return data


class PlaylistHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_bytes(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        proxy = getattr(self.server, "proxy", None)
        try:
            if path == "/live.m3u8":
                if proxy is not None and (proxy.active or proxy.custom_master_bytes is not None):
                    body = proxy.get_master_playlist()
                else:
                    body = self.server.playlist_data
                self._send_bytes(body, "application/vnd.apple.mpegurl")
                return

            if proxy is not None and path in ("/video.m3u8", "/audio.m3u8"):
                kind = "video" if path.startswith("/video") else "audio"
                body = proxy.get_media_playlist(kind)
                if not body:
                    self.send_error(503)
                    return
                self._send_bytes(body, "application/vnd.apple.mpegurl")
                return

            if proxy is not None and path.startswith("/init/"):
                parts = path.split("/")
                if len(parts) >= 4:
                    kind, name = parts[2], parts[3]
                    data = proxy.get_init_bytes(kind, name)
                    if data is not None:
                        self._send_bytes(data, "video/mp4")
                        return
                self.send_error(404)
                return

            if proxy is not None and path.startswith("/seg/"):
                parts = path.split("/")
                if len(parts) >= 4:
                    kind, name = parts[2], parts[3]
                    data = proxy.get_segment_bytes(kind, name)
                    if data is not None:
                        self._send_bytes(data, "video/iso.segment")
                        return
                self.send_error(504)
                return

            self.send_error(404)
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except OSError:
            pass

    def log_message(self, *args):
        pass


class CNewsDirect:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.sock_path = os.path.join(
            tempfile.gettempdir(), f"cnews-direct-{os.getpid()}.sock"
        )
        self.channel = "cnews_tv"
        self.max_height = DEFAULT_MAX_HEIGHT
        self.resolve_token = 0
        self.fullscreen = False
        self.saved_geometry = None
        self.paused = False
        self.muted = False
        self.is_behind_live = False
        self.controls_mode = "full"
        self.controls_visible = True
        self.hovering_controls = False
        self.hide_controls_job = None
        self.last_pointer_xy = None
        self.last_position = None
        self.last_progress = time.time()
        self.resolving = False
        self.backoff = 0
        self._running = True

        self.proxy = HLSProxyState(GLOBAL_FETCHER)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), PlaylistHandler)
        self.httpd.daemon_threads = True
        self.httpd.playlist_data = b""
        self.httpd.proxy = self.proxy
        port = self.httpd.server_address[1]
        self.proxy.base_local_url = f"http://127.0.0.1:{port}"
        self.playlist_url = f"http://127.0.0.1:{port}/live.m3u8"
        self.audio_playlist_url = f"http://127.0.0.1:{port}/audio.m3u8"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._proxy_poller_loop, daemon=True).start()
        threading.Thread(target=self._ipc_monitor_loop, daemon=True).start()

        root.title(CHANNELS[self.channel]["title"])
        root.configure(bg="black")
        width, height = 1024, 660
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 2)
        self.saved_geometry = f"{width}x{height}+{x}+{y}"
        root.geometry(self.saved_geometry)
        root.minsize(420, 290)
        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "icon.png"
        )
        try:
            self.icon = tk.PhotoImage(file=icon_path)
            root.iconphoto(True, self.icon)
        except tk.TclError:
            self.icon = None

        self._build_top_bar()
        self._build_controls()

        self.video = tk.Frame(root, bg="#0d1015", highlightthickness=0, bd=0)
        self.video.pack(side="top", fill="both", expand=True)

        self._build_audio_card()

        self.status = tk.Label(
            self.video, text="", fg="#dddddd", bg="#0d1015", font=("Sans", 12)
        )
        self.retry = tk.Button(
            self.video,
            text="Réessayer",
            command=self.start,
            state="disabled",
            bg="#262b33",
            fg="#ffffff",
            activebackground="#3daee9",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=6,
        )

        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.bind("<space>", lambda e: self.toggle_pause())
        root.bind("<Key-k>", lambda e: self.toggle_pause())
        root.bind("<Key-K>", lambda e: self.toggle_pause())
        root.bind("<Key-m>", lambda e: self.toggle_mute())
        root.bind("<Key-M>", lambda e: self.toggle_mute())
        root.bind("<Key-f>", lambda e: self.toggle_fullscreen())
        root.bind("<Key-F>", lambda e: self.toggle_fullscreen())
        root.bind("<F11>", lambda e: self.toggle_fullscreen())
        root.bind("<Left>", lambda e: self.seek(-10))
        root.bind("<Right>", lambda e: self.seek(10))
        root.bind("<Key-d>", lambda e: self.go_live())
        root.bind("<Key-D>", lambda e: self.go_live())
        root.bind("<Key-r>", lambda e: self.reload_stream())
        root.bind("<Key-R>", lambda e: self.reload_stream())
        root.bind("<F5>", lambda e: self.reload_stream())
        root.bind("<Key-1>", lambda e: self.switch_channel("cnews_tv"))
        root.bind("<Key-2>", lambda e: self.switch_channel("cnews_audio"))
        root.bind("<Key-3>", lambda e: self.switch_channel("europe1"))
        root.bind("<Escape>", lambda e: self.escape())
        root.bind("<Key-q>", lambda e: self.quit())
        root.bind("<Key-Q>", lambda e: self.quit())

        for widget in (root, self.video):
            widget.bind("<Button-1>", self._on_video_click)
            widget.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
            widget.bind("<Motion>", lambda e: self.on_user_activity())

        self.update_control_buttons()
        root.after(100, self.start)
        root.after(1000, self.watch)
        root.after(200, self.poll_pointer)
        root.after(TOKEN_REFRESH_SECONDS * 1000, self.refresh)

    def _proxy_poller_loop(self):
        """Continuously refreshes M3U8 playlists and prefetches segments in the background."""
        while self._running:
            time.sleep(1.4)
            with self.proxy.lock:
                active = self.proxy.active
                audio_only = self.proxy.audio_only
                gen = self.proxy.generation
            if not active:
                continue
            threading.Thread(
                target=self.proxy.refresh_kind, args=("audio", gen, False), daemon=True
            ).start()
            if not audio_only:
                threading.Thread(
                    target=self.proxy.refresh_kind,
                    args=("video", gen, False),
                    daemon=True,
                ).start()

    def _ipc_monitor_loop(self):
        """Polls mpv state off the main Tkinter thread so the UI never freezes."""
        while self._running:
            time.sleep(3.0)
            proc = self.proc
            if proc is not None and proc.poll() is None:
                position = self.get_property("time-pos")
                paused = self.get_property("pause")
                muted = self.get_property("mute")
                if not self._running:
                    break
                self.root.after(
                    0, lambda p=position, pa=paused, mu=muted: self._apply_ipc_state(p, pa, mu)
                )
            else:
                self.last_progress = time.time()

    def _apply_ipc_state(self, position, paused, muted):
        if self.proc is None or self.proc.poll() is not None:
            return
        changed = False
        if isinstance(paused, bool) and paused != self.paused:
            self.paused = paused
            changed = True
        if isinstance(muted, bool) and muted != self.muted:
            self.muted = muted
            changed = True
        if changed:
            self.update_control_buttons()

        if isinstance(position, (int, float)) and not self.paused:
            if self.last_position is None or abs(position - self.last_position) > 0.05:
                self.last_position = position
                self.last_progress = time.time()
                self.backoff = 0
            elif time.time() - self.last_progress > STALL_SECONDS:
                self.show_status("Reprise du direct…")
                self.root.after(500, self.start)

    def _make_btn(self, parent, text, command, bg="#252a32", fg="#f0f4f8", padx=8):
        return tk.Button(
            parent,
            text=text,
            command=lambda: (command(), self.root.focus_set()),
            bg=bg,
            fg=fg,
            activebackground="#3daee9",
            activeforeground="#ffffff",
            font=("Sans", 9, "bold"),
            relief="flat",
            bd=0,
            padx=padx,
            pady=6,
            cursor="hand2",
            takefocus=0,
        )

    def _build_top_bar(self):
        self.top_bar = tk.Frame(
            self.root,
            bg="#16191e",
            height=42,
            highlightbackground="#2c323c",
            highlightthickness=1,
        )
        self.top_bar.pack(side="top", fill="x")
        self.top_bar.pack_propagate(False)

        self.top_bar.bind("<Enter>", self._on_controls_enter)
        self.top_bar.bind("<Leave>", self._on_controls_leave)

        self.quality_frame = tk.Frame(self.top_bar, bg="#16191e")
        self.quality_frame.pack(side="right", padx=6, pady=4)

        self.quality_buttons = {}
        for q_height, q_label in QUALITY_OPTIONS:
            btn = self._make_btn(
                self.quality_frame,
                q_label,
                lambda h=q_height: self.switch_quality(h),
                padx=6,
            )
            btn.pack(side="left", padx=2)
            self.quality_buttons[q_height] = btn

        center = tk.Frame(self.top_bar, bg="#16191e")
        center.pack(expand=True, pady=4)

        self.channel_buttons = {}
        for ch_id in ("cnews_tv", "cnews_audio", "europe1"):
            btn = self._make_btn(
                center,
                CHANNELS[ch_id]["label_full"],
                lambda cid=ch_id: self.switch_channel(cid),
            )
            btn.pack(side="left", padx=4)
            self.channel_buttons[ch_id] = btn

    def _build_audio_card(self):
        self.audio_card = tk.Frame(
            self.video,
            bg="#161b24",
            highlightbackground="#2c3545",
            highlightthickness=2,
            padx=36,
            pady=28,
        )
        self.audio_icon_lbl = tk.Label(
            self.audio_card,
            text="🎧",
            bg="#161b24",
            fg="#ffffff",
            font=("Sans", 40),
        )
        self.audio_icon_lbl.pack(pady=(0, 10))

        self.audio_title_lbl = tk.Label(
            self.audio_card,
            text="",
            bg="#161b24",
            fg="#f0f4f8",
            font=("Sans", 16, "bold"),
        )
        self.audio_title_lbl.pack(pady=(0, 6))

        self.audio_sub_lbl = tk.Label(
            self.audio_card,
            text="",
            bg="#161b24",
            fg="#9aa5b4",
            font=("Sans", 10),
        )
        self.audio_sub_lbl.pack(pady=(0, 12))

        self.audio_state_lbl = tk.Label(
            self.audio_card,
            text="● Lecture en direct",
            bg="#161b24",
            fg="#4ade80",
            font=("Sans", 11, "bold"),
        )
        self.audio_state_lbl.pack()

        for w in (
            self.audio_card,
            self.audio_icon_lbl,
            self.audio_title_lbl,
            self.audio_sub_lbl,
            self.audio_state_lbl,
        ):
            w.bind("<Button-1>", self._on_video_click)
            w.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
            w.bind("<Motion>", lambda e: self.on_user_activity())

    def _update_audio_card(self):
        info = CHANNELS[self.channel]
        if not info.get("is_audio_only"):
            self.audio_card.place_forget()
            return
        self.audio_icon_lbl.configure(text=info.get("card_icon", "🎧"))
        self.audio_title_lbl.configure(text=info.get("card_title", ""))
        self.audio_sub_lbl.configure(text=info.get("card_subtitle", ""))
        self.audio_card.configure(highlightbackground=info.get("accent", "#3daee9"))

        if self.resolving:
            self.audio_state_lbl.configure(text="Connexion au flux…", fg="#fbbf24")
        elif self.paused:
            self.audio_state_lbl.configure(text="⏸ En pause", fg="#fbbf24")
        elif self.muted:
            self.audio_state_lbl.configure(text="🔇 Son coupé (Muet)", fg="#f87171")
        else:
            self.audio_state_lbl.configure(text="● En direct", fg="#4ade80")

        self.audio_card.place(relx=0.5, rely=0.5, anchor="center")

    def _build_controls(self):
        self.controls = tk.Frame(
            self.root,
            bg="#16191e",
            height=46,
            highlightbackground="#2c323c",
            highlightthickness=1,
        )
        self.controls.pack(side="bottom", fill="x")
        self.controls.pack_propagate(False)

        self.controls.bind("<Enter>", self._on_controls_enter)
        self.controls.bind("<Leave>", self._on_controls_leave)
        self.controls.bind("<Configure>", self._on_controls_resize)

        self.right_group = tk.Frame(self.controls, bg="#16191e")
        self.right_group.pack(side="right", padx=6, pady=5)

        self.btn_mute = self._make_btn(
            self.right_group, "🔊 Son", self.toggle_mute
        )
        self.btn_mute.pack(side="left", padx=2)

        self.btn_fs = self._make_btn(
            self.right_group, "⛶ Plein écran", self.toggle_fullscreen
        )
        self.btn_fs.pack(side="left", padx=2)

        self.btn_quit = self._make_btn(
            self.right_group,
            "✕ Quitter",
            self.quit,
            bg="#3a2326",
            fg="#ffccd2",
        )
        self.btn_quit.pack(side="left", padx=(4, 2))

        self.left_group = tk.Frame(self.controls, bg="#16191e")
        self.left_group.pack(side="left", padx=6, pady=5)

        self.btn_pause = self._make_btn(
            self.left_group, "⏸ Pause", self.toggle_pause
        )
        self.btn_pause.pack(side="left", padx=2)

        self.btn_back = self._make_btn(
            self.left_group, "⏪ -10s", lambda: self.seek(-10)
        )
        self.btn_back.pack(side="left", padx=2)

        self.btn_fwd = self._make_btn(
            self.left_group, "⏩ +10s", lambda: self.seek(10)
        )
        self.btn_fwd.pack(side="left", padx=2)

        self.btn_live = self._make_btn(
            self.left_group,
            "● EN DIRECT",
            self.go_live,
            bg="#c92a2a",
            fg="#ffffff",
        )
        self.btn_live.pack(side="left", padx=4)

        self.btn_reload = self._make_btn(
            self.left_group, "⟳ Recharger", self.reload_stream
        )
        self.btn_reload.pack(side="left", padx=2)

    def _on_controls_resize(self, event):
        if event.width >= 950:
            mode = "full"
            padx = 8
        elif event.width >= 600:
            mode = "medium"
            padx = 7
        else:
            mode = "compact"
            padx = 6
        if mode != self.controls_mode:
            self.controls_mode = mode
            for btn in (
                self.btn_pause,
                self.btn_back,
                self.btn_fwd,
                self.btn_live,
                self.btn_reload,
                self.btn_mute,
                self.btn_fs,
                self.btn_quit,
            ):
                btn.configure(padx=padx)
            self.update_control_buttons()

    def _on_controls_enter(self, _event=None):
        self.hovering_controls = True
        if self.hide_controls_job is not None:
            self.root.after_cancel(self.hide_controls_job)
            self.hide_controls_job = None

    def _on_controls_leave(self, _event=None):
        self.hovering_controls = False
        if self.fullscreen:
            self._schedule_hide_controls()

    def _on_video_click(self, _event=None):
        self.root.focus_set()
        self.on_user_activity()

    def on_user_activity(self):
        self.show_controls()
        if self.fullscreen:
            self._schedule_hide_controls()

    def show_controls(self):
        if not self.controls_visible:
            self.top_bar.pack(side="top", fill="x", before=self.video)
            self.controls.pack(side="bottom", fill="x", before=self.video)
            self.controls_visible = True
        self.video.configure(cursor="")
        self.root.configure(cursor="")

    def hide_controls(self):
        if not self.fullscreen or self.hovering_controls:
            return
        if self.controls_visible:
            self.top_bar.pack_forget()
            self.controls.pack_forget()
            self.controls_visible = False
        self.video.configure(cursor="none")
        self.root.configure(cursor="none")

    def _schedule_hide_controls(self):
        if self.hide_controls_job is not None:
            self.root.after_cancel(self.hide_controls_job)
        if self.fullscreen and not self.hovering_controls:
            self.hide_controls_job = self.root.after(
                CONTROLS_HIDE_DELAY_MS, self.hide_controls
            )

    def poll_pointer(self):
        if self.fullscreen:
            try:
                xy = self.root.winfo_pointerxy()
                if self.last_pointer_xy is not None and xy != self.last_pointer_xy:
                    self.on_user_activity()
                self.last_pointer_xy = xy
            except tk.TclError:
                pass
        self.root.after(200, self.poll_pointer)

    def switch_quality(self, height):
        if height == self.max_height:
            return
        self.max_height = height
        self.update_control_buttons()
        if self.channel == "cnews_tv":
            self.reload_stream()

    def switch_channel(self, channel_id):
        if channel_id not in CHANNELS or channel_id == self.channel:
            return
        self.channel = channel_id
        self.root.title(CHANNELS[self.channel]["title"])
        self.paused = False
        self.is_behind_live = False
        self.backoff = 0
        self.resolving = False
        self.update_control_buttons()
        self.start()

    def update_control_buttons(self):
        m = self.controls_mode
        full = m == "full"
        compact = m == "compact"

        for ch_id, btn in self.channel_buttons.items():
            ch_info = CHANNELS[ch_id]
            lbl = ch_info["label_compact"] if compact else ch_info["label_full"]
            if ch_id == self.channel:
                btn.configure(text=lbl, bg=ch_info["accent"], fg="#ffffff")
            else:
                btn.configure(text=lbl, bg="#252a32", fg="#c8d0dc")

        for q_height, btn in self.quality_buttons.items():
            if q_height == self.max_height:
                btn.configure(bg="#2b5b84", fg="#ffffff")
            else:
                btn.configure(bg="#252a32", fg="#9aa5b4")

        self.btn_back.configure(text="⏪ -10s" if full else "-10s")
        self.btn_fwd.configure(text="⏩ +10s" if full else "+10s")
        self.btn_reload.configure(text="⟳" if compact else "⟳ Recharger")
        self.btn_quit.configure(text="✕ Quitter" if full else "✕")

        if self.paused:
            self.btn_pause.configure(
                text="▶" if compact else "▶ Lecture", bg="#2b5b84"
            )
        else:
            self.btn_pause.configure(
                text="⏸" if compact else "⏸ Pause", bg="#252a32"
            )

        if self.muted:
            self.btn_mute.configure(
                text="🔇 Muet" if full else "🔇", bg="#b33939"
            )
        else:
            self.btn_mute.configure(
                text="🔊 Son" if full else "🔊", bg="#252a32"
            )

        if self.is_behind_live or self.paused:
            self.btn_live.configure(
                text="↩ Retour au direct" if full else "↩ Direct", bg="#d97706"
            )
        else:
            self.btn_live.configure(
                text="● EN DIRECT" if full else "● Direct", bg="#c92a2a"
            )

        if self.fullscreen:
            self.btn_fs.configure(
                text="❐ Fenêtré" if full else "❐", bg="#2b5b84"
            )
        else:
            self.btn_fs.configure(
                text="⛶ Plein écran" if full else "⛶", bg="#252a32"
            )

        self._update_audio_card()

    def toggle_pause(self):
        self.paused = not self.paused
        self.send("set_property", "pause", self.paused)
        if self.paused:
            self.is_behind_live = True
        self.update_control_buttons()

    def toggle_mute(self):
        self.muted = not self.muted
        self.send("set_property", "mute", self.muted)
        self.update_control_buttons()

    def seek(self, seconds):
        self.send("seek", str(seconds), "relative")
        if seconds < 0:
            self.is_behind_live = True
        self.update_control_buttons()

    def go_live(self):
        self.paused = False
        self.is_behind_live = False
        self.send("set_property", "pause", False)
        self.send("seek", "100", "absolute-percent")
        self.update_control_buttons()

    def reload_stream(self):
        self.paused = False
        self.is_behind_live = False
        self.resolving = False
        self.update_control_buttons()
        self.show_status("Rafraîchissement du direct…")
        self.root.after(150, self.start)

    def start(self):
        self.stop_mpv()
        self.resolve_token += 1
        token = self.resolve_token

        if self.channel == "europe1":
            self.proxy.stop()
            self.resolving = False
            self._update_audio_card()
            self.launch_mpv(EUROPE1_STREAM_URL, audio_only=True)
            return

        override = os.environ.get("CNEWS_PLAYLIST")
        if override and self.channel == "cnews_tv":
            try:
                with open(override, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError as exc:
                self.show_error(str(exc))
                return
            self.set_playlist(text)
            self.launch_mpv(self.playlist_url, audio_only=False)
            return

        self.resolving = True
        self._update_audio_card()
        if not CHANNELS[self.channel]["is_audio_only"]:
            self.show_status("Chargement du direct…")
        else:
            self.hide_status()
        threading.Thread(
            target=self._resolve_worker,
            args=(token, self.channel, self.max_height),
            daemon=True,
        ).start()

    def _resolve_worker(self, token, channel, max_height):
        try:
            master_text = fetch_master()
            if channel == "cnews_audio":
                audio_url = extract_cnews_audio_url(master_text)
                self.proxy.configure_streams({}, "", audio_url, audio_only=True)
                target = self.audio_playlist_url
            else:
                attrs, video_url, audio_url = select_streams(
                    master_text, max_height=max_height
                )
                self.proxy.configure_streams(
                    attrs, video_url, audio_url, audio_only=False
                )
                target = self.playlist_url
        except Exception as exc:
            self.root.after(0, lambda: self._on_resolve_error(token, str(exc)))
            return
        self.root.after(0, lambda: self._on_resolve_success(token, channel, target))

    def _on_resolve_error(self, token, message):
        if token != self.resolve_token:
            return
        self.resolving = False
        self.audio_card.place_forget()
        self.show_error(message)

    def _on_resolve_success(self, token, channel, target):
        if token != self.resolve_token:
            return
        self.resolving = False
        if channel == "cnews_audio":
            self._update_audio_card()
            self.launch_mpv(target, audio_only=True)
        else:
            self.launch_mpv(target, audio_only=False)

    def set_playlist(self, text):
        self.httpd.playlist_data = text.encode("utf-8")
        self.proxy.configure_custom(text)

    def launch_mpv(self, stream_url, audio_only=False):
        self.stop_mpv(stop_proxy=False)
        self.hide_status()
        self._update_audio_card()
        self.root.update_idletasks()
        debug = bool(os.environ.get("CNEWS_DEBUG"))
        args = [
            "mpv",
            "--no-config",
            "--no-border",
            "--no-osc",
            "--no-osd-bar",
            "--no-window-dragging",
            "--input-vo-keyboard=no",
            "--input-cursor=no",
            "--input-cursor-passthrough=yes",
            "--ao=pipewire,pulse,alsa",
            "--audio-buffer=1.0",
            "--msg-level=all=warn" if debug else "--really-quiet",
            "--cache=yes",
            "--cache-secs=20",
            "--demuxer-readahead-secs=15",
            "--demuxer-max-bytes=40MiB",
            "--demuxer-max-back-bytes=30MiB",
            "--cache-pause=yes",
            "--cache-pause-initial=yes",
            "--cache-pause-wait=2",
            "--demuxer-lavf-o=live_start_index=-5,http_persistent=1,reconnect=1,reconnect_streamed=1,reconnect_delay_max=2",
            f"--title={CHANNELS[self.channel]['title']}",
            f"--input-ipc-server={self.sock_path}",
        ]
        if audio_only:
            args.extend(["--vid=no", "--force-window=no"])
        else:
            args.extend([
                "--profile=fast",
                "--force-window=yes",
                "--hwdec=auto-safe",
                "--framedrop=vo",
                "--vd-lavc-dr=yes",
                f"--wid={self.video.winfo_id()}",
            ])
        args.append(stream_url)

        try:
            if debug:
                self.mpv_log = open("/tmp/cnews-mpv.log", "w")
                out = self.mpv_log
            else:
                self.mpv_log = None
                out = subprocess.DEVNULL
            self.proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            self.show_error("mpv est introuvable sur ce système.")
            return
        self.paused = False
        self.is_behind_live = False
        self.update_control_buttons()
        self.last_position = None
        self.last_progress = time.time()
        self.root.after(500, self.root.focus_force)
        self.root.after(800, self._sync_initial_state)

    def _sync_initial_state(self):
        self.send("set_property", "pause", False)
        if self.muted:
            self.send("set_property", "mute", True)

    def watch(self):
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            delay = min(60, 3 * (2 ** self.backoff))
            self.backoff = min(self.backoff + 1, 5)
            self.show_status("Reconnexion au direct…")
            self.root.after(delay * 1000, self.start)
        self.root.after(1500, self.watch)

    def refresh(self):
        """Refreshes Dailymotion CDN token transparently in background without restarting mpv."""
        if self.proc is not None and self.channel != "europe1":
            threading.Thread(
                target=self._silent_token_refresh,
                args=(self.channel, self.max_height),
                daemon=True,
            ).start()
        self.root.after(TOKEN_REFRESH_SECONDS * 1000, self.refresh)

    def _silent_token_refresh(self, channel, max_height):
        try:
            master_text = fetch_master()
            if channel == "cnews_audio":
                audio_url = extract_cnews_audio_url(master_text)
                self.proxy.update_upstream_urls("", audio_url)
            else:
                _, video_url, audio_url = select_streams(
                    master_text, max_height=max_height
                )
                self.proxy.update_upstream_urls(video_url, audio_url)
        except Exception:
            pass

    def send(self, *command):
        def _worker():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.3)
                    sock.connect(self.sock_path)
                    sock.sendall(
                        json.dumps({"command": list(command)}).encode() + b"\n"
                    )
            except OSError:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def get_property(self, name):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.4)
                sock.connect(self.sock_path)
                sock.sendall(
                    json.dumps({"command": ["get_property", name]}).encode() + b"\n"
                )
                buffer = b""
                while b"\n" not in buffer:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
            data = json.loads(buffer.decode())
            return data.get("data")
        except (OSError, ValueError):
            return None

    def show_status(self, message):
        self.status.configure(text=message)
        self.status.place(relx=0.5, rely=0.5, anchor="center")
        self.retry.place_forget()

    def hide_status(self):
        self.status.place_forget()
        self.retry.place_forget()

    def show_error(self, message):
        self.status.configure(text=f"Impossible de lire le direct.\n{message}")
        self.status.place(relx=0.5, rely=0.5, anchor="center")
        self.retry.place(relx=0.5, rely=0.62, anchor="center")
        self.retry.configure(state="normal")

    def toggle_fullscreen(self):
        if not self.fullscreen:
            self.saved_geometry = self.root.geometry()
            self.fullscreen = True
            self.root.attributes("-fullscreen", True)
            self.last_pointer_xy = self.root.winfo_pointerxy()
            self._schedule_hide_controls()
        else:
            self.fullscreen = False
            if self.hide_controls_job is not None:
                self.root.after_cancel(self.hide_controls_job)
                self.hide_controls_job = None
            self.root.attributes("-fullscreen", False)
            if self.saved_geometry:
                self.root.geometry(self.saved_geometry)
            self.show_controls()
        self.update_control_buttons()
        self.root.focus_set()

    def escape(self):
        if self.fullscreen:
            self.toggle_fullscreen()
        else:
            self.quit()

    def stop_mpv(self, stop_proxy=True):
        if stop_proxy:
            self.proxy.stop()
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        if getattr(self, "mpv_log", None):
            self.mpv_log.close()
            self.mpv_log = None
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass

    def quit(self):
        self._running = False
        self.stop_mpv()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.root.destroy()


def ensure_desktop_shortcut():
    app_dir = os.path.dirname(os.path.abspath(__file__))
    exec_path = os.path.join(app_dir, "cnews-direct")
    icon_path = os.path.join(app_dir, "icon.png")
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=CNEWS Direct\n"
        "GenericName=Direct TV & Radio\n"
        "Comment=Regarder CNEWS TV et écouter CNEWS Audio / Europe 1\n"
        f'Exec="{exec_path}"\n'
        f"Icon={icon_path}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Video;TV;\n"
        "Keywords=CNEWS;Europe1;direct;tv;radio;info;actualités;\n"
        "StartupNotify=true\n"
    )
    home = os.path.expanduser("~")
    apps_dir = os.path.join(home, ".local", "share", "applications")
    targets = []
    try:
        os.makedirs(apps_dir, exist_ok=True)
        targets.append(os.path.join(apps_dir, "cnews-direct.desktop"))
    except OSError:
        pass

    for d_name in ("Bureau", "Desktop"):
        d_path = os.path.join(home, d_name)
        if os.path.isdir(d_path):
            targets.append(os.path.join(d_path, "cnews-direct.desktop"))
            break

    for target in targets:
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(target, 0o755)
        except OSError:
            pass


def main():
    ensure_desktop_shortcut()
    root = tk.Tk()
    CNewsDirect(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
