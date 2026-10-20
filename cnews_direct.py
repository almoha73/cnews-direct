#!/usr/bin/env python3
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
MAX_HEIGHT = int(os.environ.get("CNEWS_MAX_HEIGHT", "720"))
REFRESH_SECONDS = 60 * 60
STALL_SECONDS = 45
CONTROLS_HIDE_DELAY_MS = 3000

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


class PlaylistHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] != "/live.m3u8":
            self.send_error(404)
            return
        body = self.server.playlist_data
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class StreamError(Exception):
    pass


def parse_attrs(text):
    return {m.group(1): m.group(2).strip('"') for m in ATTR_RE.finditer(text)}


def http_get(url, cookies="", referer=PLAYER_REFERER, origin=PLAYER_ORIGIN):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": referer,
        "Origin": origin,
    }
    if cookies:
        headers["Cookie"] = cookies
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_master():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(METADATA_URL, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=20) as resp:
        data = json.load(resp)
    master_url = data["qualities"]["auto"][0]["url"]
    cookies = "; ".join(f"{c.name}={c.value}" for c in jar)

    last_error = None
    for delay in (0, 3, 8, 20):
        if delay:
            time.sleep(delay)
        try:
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


def build_playlist(master_text):
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
        if height > MAX_HEIGHT:
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


class CNewsDirect:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.sock_path = os.path.join(
            tempfile.gettempdir(), f"cnews-direct-{os.getpid()}.sock"
        )
        self.channel = "cnews_tv"
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

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), PlaylistHandler)
        self.httpd.daemon_threads = True
        self.httpd.playlist_data = b""
        self.playlist_url = (
            f"http://127.0.0.1:{self.httpd.server_address[1]}/live.m3u8"
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

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
        root.after(3000, self.check_progress)
        root.after(200, self.poll_pointer)
        root.after(REFRESH_SECONDS * 1000, self.refresh)

    def _make_btn(self, parent, text, command, bg="#252a32", fg="#f0f4f8"):
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
            padx=8,
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
            target=self._resolve_worker, args=(token, self.channel), daemon=True
        ).start()

    def _resolve_worker(self, token, channel):
        try:
            master_text = fetch_master()
            if channel == "cnews_audio":
                target = extract_cnews_audio_url(master_text)
            else:
                target = build_playlist(master_text)
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
            self.set_playlist(target)
            self.launch_mpv(self.playlist_url, audio_only=False)

    def set_playlist(self, text):
        self.httpd.playlist_data = text.encode("utf-8")

    def launch_mpv(self, stream_url, audio_only=False):
        self.stop_mpv()
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
            "--audio-samplerate=48000",
            "--audio-buffer=1.0",
            "--msg-level=all=warn" if debug else "--really-quiet",
            "--cache=yes",
            "--cache-secs=15",
            "--demuxer-readahead-secs=10",
            "--demuxer-max-back-bytes=50MiB",
            f"--title={CHANNELS[self.channel]['title']}",
            f"--input-ipc-server={self.sock_path}",
        ]
        if audio_only:
            args.extend(["--vid=no", "--force-window=no"])
        else:
            args.extend([
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

    def check_progress(self):
        if self.proc is not None and self.proc.poll() is None:
            position = self.get_property("time-pos")
            paused = self.get_property("pause")
            muted = self.get_property("mute")
            if isinstance(paused, bool) and paused != self.paused:
                self.paused = paused
                self.update_control_buttons()
            if isinstance(muted, bool) and muted != self.muted:
                self.muted = muted
                self.update_control_buttons()
            if isinstance(position, (int, float)) and not paused:
                if self.last_position is None or abs(position - self.last_position) > 0.05:
                    self.last_position = position
                    self.last_progress = time.time()
                    self.backoff = 0
                elif time.time() - self.last_progress > STALL_SECONDS:
                    self.show_status("Reprise du direct…")
                    self.root.after(500, self.start)
        else:
            self.last_progress = time.time()
        self.root.after(5000, self.check_progress)

    def refresh(self):
        if self.proc is not None and self.channel != "europe1":
            self.show_status("Rafraîchissement du direct…")
            self.root.after(400, self.start)
        self.root.after(REFRESH_SECONDS * 1000, self.refresh)

    def send(self, *command):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.3)
                sock.connect(self.sock_path)
                sock.sendall(json.dumps({"command": list(command)}).encode() + b"\n")
        except OSError:
            pass

    def get_property(self, name):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
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

    def stop_mpv(self):
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
