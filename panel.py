"""Панель Яндекс Музыки в правом нижнем углу экрана.

Маленькое окно поверх всех окон: обложка, название трека, исполнитель,
прогресс и кнопки «назад / пауза-играть / вперёд». Читает воспроизведение
через системный медиа-интерфейс Windows (SMTC), поэтому работает и с
приложением «Яндекс Музыка», и с музыкой в браузере.

Запуск:  pythonw.exe panel.py            панель один раз
         pythonw.exe panel.py --watch    контролёр: панель вместе с приложением
         python panel.py --install       включить автозапуск контролёра
         python panel.py --version       версия

Версия: 1.1 (установочный файл YandexMusicPanel-1.1.exe)
"""

import asyncio
import base64
import ctypes
import ctypes.wintypes as wintypes
import datetime
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox as messagebox
import urllib.request

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SmtcManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as SmtcStatus,
)
from winrt.windows.storage.streams import DataReader

try:
    from PIL import Image, ImageTk
except Exception:  # обложка необязательна
    Image = ImageTk = None

if getattr(sys, "frozen", False):        # собрано PyInstaller: данные рядом с .exe
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "config.json")
PID_FILE = os.path.join(BASE, ".watch.pid")

# ---------- цвета: палитра приложения Яндекс Музыка ----------
# тёмный фон интерфейса + фирменный жёлтый #FFDB4F
BG = "#191919"
BORDER = "#2e2e33"
FG = "#ededf0"
FG_DIM = "#9b9ba3"
BAR_BG = "#3a3a40"
BAR_FG = "#ffdb4f"
ACCENT = "#ffdb4f"
ACCENT_HOVER = "#ffe57f"
BTN_BG = "#26262b"
BTN_HOVER = "#34343b"
GLYPH_ON_ACCENT = "#191919"
TRANSPARENT = "#000001"

PANEL_W, PANEL_H = 215, 54     # макет в логических px; половина прежних 430×108
CDP_PORT = 9229      # порт отладки приложения: нужен для кнопки «Мне нравится»
COVER = 32
MARGIN = 24          # отступ от угла экрана
ROUND = 9


# ---------- Win32 ----------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


user32 = ctypes.windll.user32
user32.keybd_event.argtypes = [ctypes.c_byte, ctypes.c_byte, ctypes.c_uint, ctypes.c_void_p]

kernel32 = ctypes.windll.kernel32
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]

VK_PREV, VK_PLAY_PAUSE, VK_NEXT = 0xB6, 0xB3, 0xB5

PANEL_MUTEX = "ymcorner_panel_single"
WATCH_MUTEX = "ymcorner_watch_single"


def media_key(vk):
    """Глобальная медиа-клавиша (запасной вариант, если приложение
    не принимает команду напрямую)."""
    user32.keybd_event(vk, 0, 0, None)
    user32.keybd_event(vk, 0, 2, None)


def work_area():
    r = RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom


def fmt_time(td):
    try:
        total = int(td.total_seconds())
    except Exception:
        total = 0
    if total < 0:
        total = 0
    return "%d:%02d" % (total // 60, total % 60)


def load_conf():
    try:
        # utf-8-sig: файл могли сохранить в Блокноте с BOM
        with open(CONF, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_conf(cfg):
    try:
        with open(CONF, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass


class Smtc(threading.Thread):
    """Фоновый поток: читает воспроизведение из SMTC Windows и выполняет
    команды кнопок, чтобы панель не зависела от откликов приложения."""

    daemon = True

    def __init__(self):
        threading.Thread.__init__(self, name="smtc")
        self.cmds = queue.Queue()
        self.latest = None          # последний снимок для отрисовки
        self.sess = None

    async def _fetch(self):
        mgr = await SmtcManager.request_async()
        sessions = list(mgr.get_sessions())
        if not sessions:
            self.sess = None
            return None
        target = None
        for s in sessions:
            try:
                app = (s.source_app_user_model_id or "").lower()
            except Exception:
                app = ""
            if "yandex" in app:
                target = s
                break
        if target is None:
            for s in sessions:
                if s.get_playback_info().playback_status == SmtcStatus.PLAYING:
                    target = s
                    break
        target = target or sessions[0]
        self.sess = target
        tl = target.get_timeline_properties()
        data = {"status": target.get_playback_info().playback_status,
                "position": tl.position, "end": tl.end_time,
                "yandex": "yandex" in (target.source_app_user_model_id or "").lower(),
                "title": "", "artist": "", "thumb": None}
        mp = await target.try_get_media_properties_async()
        data["title"], data["artist"] = mp.title, mp.artist
        if Image is not None and mp.thumbnail is not None:
            st = await mp.thumbnail.open_read_async()
            n = int(st.size)
            if 0 < n < 6000000:
                rdr = DataReader(st.get_input_stream_at(0))
                await rdr.load_async(n)
                buf = bytearray(n)
                rdr.read_bytes(buf)          # заполняет буфер на месте
                data["thumb"] = bytes(buf)
        return data

    async def _cmd(self, what, arg):
        s = self.sess
        if s is None:
            return False
        if what == "play":
            return await s.try_toggle_play_pause_async()
        if what == "next":
            return await s.try_skip_next_async()
        if what == "prev":
            return await s.try_skip_previous_async()
        if what == "seek":
            return await s.try_change_playback_position_async(arg)
        return False

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            cmds = []
            try:
                while True:
                    cmds.append(self.cmds.get_nowait())
            except queue.Empty:
                pass
            for what, arg in cmds:
                ok = False
                try:
                    ok = bool(loop.run_until_complete(self._cmd(what, arg)))
                except Exception:
                    ok = False
                if not ok and what in ("prev", "play", "next"):
                    media_key({"prev": VK_PREV, "play": VK_PLAY_PAUSE,
                               "next": VK_NEXT}[what])   # запасной вариант
            try:
                self.latest = loop.run_until_complete(self._fetch())
            except Exception:
                pass
            time.sleep(0.6)


class Like(threading.Thread):
    """Кнопка «Мне нравится». В SMTC такой команды нет, поэтому панель нажимает
    родную кнопку приложения внутри «Яндекс Музыки» через её порт отладки
    (127.0.0.1). Окно приложения при этом не показывается и фокус не уходит."""

    daemon = True

    GET_JS = ("(() => { const b = document.querySelector('[data-test-id=\"LIKE_BUTTON\"]');"
              " return b ? b.getAttribute('aria-pressed') === 'true' : null; })()")
    CLICK_JS = ("(() => { const b = document.querySelector('[data-test-id=\"LIKE_BUTTON\"]');"
                " if (!b) return null; b.click(); return true; })()")

    def __init__(self, port=CDP_PORT):
        threading.Thread.__init__(self, name="like")
        self.port = port
        self.cmds = queue.Queue()
        self.state = None          # None — неизвестно, True/False — лайкнут ли трек
        self.live = False          # доступен ли режим для лайков
        self.sock = None
        self.buf = b""
        self.mid = 0

    # ---- минимальный websocket + CDP (без внешних библиотек) ----
    def _connect(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % self.port,
                                    timeout=2) as fh:
            targets = json.loads(fh.read().decode("utf-8", "replace"))
        url = None
        for t in targets:
            if t.get("type") == "page" and "music-application" in (t.get("url") or ""):
                url = t.get("webSocketDebuggerUrl")
                break
        if url is None:
            for t in targets:
                if t.get("type") == "page":
                    url = t.get("webSocketDebuggerUrl")
                    break
        if url is None:
            raise OSError("нет страницы приложения")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, prt = hostport.partition(":")
        self.sock = socket.create_connection((host, int(prt or 80)), timeout=4)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET /%s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
                           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, prt, key)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("закрыто при рукопожатии")
            head += chunk
        if b"101" not in head.split(b"\r\n")[0]:
            raise OSError("рукопожатие не принято")
        self.buf = head.split(b"\r\n\r\n", 1)[1]
        self.mid = 0

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("соединение закрыто")
            self.buf += chunk

    def _send(self, text):
        payload = text.encode()
        n = len(payload)
        hdr = bytes([0x81])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv(self):
        parts = b""
        while True:
            self._need(2)
            first, second = self.buf[0] & 0x0F, self.buf[1] & 0x7F
            last, off = self.buf[0] & 0x80, 2
            if second == 126:
                self._need(4)
                second = struct.unpack(">H", self.buf[2:4])[0]
                off = 4
            elif second == 127:
                self._need(10)
                second = struct.unpack(">Q", self.buf[2:10])[0]
                off = 10
            self._need(off + second)
            body = self.buf[off:off + second]
            self.buf = self.buf[off + second:]
            if first == 0x9:
                self._send_pong(body)
                continue
            if first == 0x8:
                raise OSError("порт отладки закрыт")
            if first in (0x1, 0x2, 0x0):
                parts += body
                if last:
                    return parts.decode("utf-8", "replace")

    def _send_pong(self, body):
        n = len(body)
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x8A, 0x80 | n]) + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(body)))

    def _js(self, expr):
        self.mid += 1
        mid = self.mid
        self._send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                               "params": {"expression": expr, "returnByValue": True}}))
        while True:
            msg = json.loads(self._recv())
            if msg.get("id") == mid:
                return msg.get("result", {}).get("result", {}).get("value")

    def _close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def probe(self):
        """Быстрая проверка: доступен ли режим для лайков прямо сейчас."""
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % self.port,
                                        timeout=0.7) as fh:
                fh.read()
            self.live = True
        except Exception:
            self.live = False
        return self.live

    def run(self):
        while True:
            try:
                if self.sock is None:
                    self._connect()
                clicked = False
                try:
                    while True:
                        self.cmds.get_nowait()
                        clicked = True
                except queue.Empty:
                    pass
                if clicked:
                    self._js(self.CLICK_JS)
                    time.sleep(0.4)          # приложение должно успеть перерисовать кнопку
                self.state = self._js(self.GET_JS)
                self.live = True
            except Exception:
                self._close()
                self.live = False
                self.state = None
            time.sleep(1.0)


class ProcEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def ym_main_proc():
    """Главный процесс приложения «Яндекс Музыка»: (pid, путь к .exe)."""
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if snap in (None, -1, 0):
        return 0, None
    pe = ProcEntry()
    pe.dwSize = ctypes.sizeof(pe)
    found = kernel32.Process32FirstW(snap, ctypes.byref(pe))
    listed = []
    while found:
        listed.append((pe.th32ProcessID, pe.th32ParentProcessID))
        found = kernel32.Process32NextW(snap, ctypes.byref(pe))
    kernel32.CloseHandle(snap)
    mine = {}
    for pid, ppid in listed:
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        name = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(name))
        path = None
        if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            path = name.value
        kernel32.CloseHandle(handle)
        if path and "\\yandexmusic\\" in path.lower():
            mine[pid] = (ppid, path)
    if not mine:
        return 0, None
    top = [p for p, (ppid, _) in mine.items() if ppid not in mine]
    pid = top[0] if top else next(iter(mine))
    return pid, mine[pid][1]


class Panel:
    def __init__(self):
        self.cfg = load_conf()
        self.k = max(1.0, float(self.cfg.get("scale", 1.0)))
        self.root = tk.Tk(className="ymcorner")
        self.root.title("Панель Яндекс Музыки")
        self.root.overrideredirect(True)
        try:
            self.root.wm_attributes("-transparentcolor", TRANSPARENT)
        except Exception:
            pass
        self.root.wm_attributes("-topmost", True)
        self.smtc = Smtc()
        self.like = Like(int(self.cfg.get("cdp_port", CDP_PORT)))
        self.like_shown = None
        self._like_asked = False
        self.art_photo = None
        self.art_key = None
        self.pos_base = None       # (position, monotonic ts)
        self.duration = None
        self.playing = False
        self.snap = None
        self._drag = None
        self._build()
        self._place()
        self.smtc.start()
        self.like.start()
        self.poll()
        self.animate()

    # ---------- геометрия ----------
    def _place(self):
        k = self.k
        w, h = int(PANEL_W * k), int(PANEL_H * k)
        self.root.geometry("%dx%d+0+0" % (w, h))
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        l, t, r, b = work_area()
        if r <= 0 or b <= 0:          # запасной вариант, если Windows не отдала область
            r, b = sw, sh - int(48 * k)
        scale_y = sh / max(1, user32.GetSystemMetrics(1))
        if scale_y <= 0.1 or scale_y > 10:
            scale_y = 1.0
        x = self.cfg.get("x")
        y = self.cfg.get("y")
        bx, by = r - w - MARGIN, int(b * scale_y) - h - MARGIN   # правый нижний угол
        if (x is None or y is None
                or not 0 <= int(x) <= sw - w or not 0 <= int(y) <= sh - h):
            # позиции нет или она не влезает в этот экран (другой монитор) — в угол
            x, y = bx, by
        x = max(0, min(int(x), sw - w, max(0, bx)))
        y = max(0, min(int(y), sh - h, max(0, by)))
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.update_idletasks()
        if os.environ.get("YM_DEBUG"):
            print("place: screen=%sx%s work=%s size=%sx%s -> %d,%d real=%s"
                  % (sw, sh, (l, t, r, b), w, h, x, y, self.root.geometry()))

    # ---------- виджеты ----------
    def _build(self):
        k = self.k
        w, h = int(PANEL_W * k), int(PANEL_H * k)
        cv = tk.Canvas(self.root, width=w, height=h, highlightthickness=0,
                       bd=0, bg=TRANSPARENT)
        cv.pack(fill="both", expand=True)
        self.cv = cv
        self._round_rect(cv, 1, 1, w - 1, h - 1, int(ROUND * k),
                         fill=BG, outline=BORDER, width=1, tag="bg")

        cx, cy = int(8 * k), int((PANEL_H * k - COVER * k) / 2)
        self.cover_box = cv.create_rectangle(cx, cy, cx + COVER * k, cy + COVER * k,
                                             fill="#2a2a33", outline=BORDER, tag="cover")
        self.cover_img = cv.create_image(cx + COVER * k / 2, cy + COVER * k / 2,
                                         image="", tag="cover")

        tx = cx + COVER * k + int(7 * k)
        self.text_w = int(75 * k)         # ширина текста, чтобы не наползал на кнопки
        self.f_title = tkfont.Font(family="Segoe UI", size=int(7 * k), weight="bold")
        self.f_artist = tkfont.Font(family="Segoe UI", size=int(5 * k))
        self.title_id = cv.create_text(tx, int(13 * k), anchor="w", text="Яндекс Музыка",
                                       fill=FG, font=self.f_title,
                                       width=self.text_w, justify="left", tag="text")
        self.artist_id = cv.create_text(tx, int(24 * k), anchor="w", text="—",
                                        fill=FG_DIM, font=self.f_artist,
                                        width=self.text_w, justify="left", tag="text")

        # прогресс
        self.bar_y = int(37 * k)
        self.bar_x1, self.bar_x2 = tx, tx + self.text_w
        cv.create_line(self.bar_x1, self.bar_y, self.bar_x2, self.bar_y,
                       fill=BAR_BG, width=max(3, int(4 * k)), cap="round", tag="bar")
        self.bar_fill = cv.create_line(self.bar_x1, self.bar_y, self.bar_x1, self.bar_y,
                                       fill=BAR_FG, width=max(3, int(4 * k)), cap="round",
                                       tag="bar")
        self.time_id = cv.create_text(tx + self.text_w // 2, int(46 * k), anchor="center",
                                      text="", fill=FG_DIM,
                                      font=("Segoe UI", int(5 * k)), tag="text")

        # кнопки: лайк / назад / играть-пауза / вперёд (жёлтый круг — как в приложении)
        bx = w - int(59 * k)
        by = int(20 * k)
        r = int(8 * k)
        self.off = {"like": -int(21 * k), "prev": 0, "play": int(21 * k),
                    "next": int(42 * k)}
        self.buttons = {}
        for name in ("like", "prev", "play", "next"):
            bxp = bx + self.off[name]
            fill = ACCENT if name == "play" else BTN_BG
            circ = cv.create_oval(bxp - r, by - r, bxp + r, by + r,
                                  fill=fill, outline=fill, tag=("btn", name))
            glyphs = self._icon(cv, name, bxp, by, r)
            self.buttons[name] = (circ, glyphs)
            for it in [circ] + glyphs:
                cv.tag_bind(it, "<Button-1>", lambda e, n=name: self.act(n))
                cv.tag_bind(it, "<Enter>", lambda e, n=name: self._hover(n, True))
                cv.tag_bind(it, "<Leave>", lambda e, n=name: self._hover(n, False))
        self.btn_y2 = int(42 * k)

        self.hint = cv.create_text(w - int(7 * k), int(46 * k), anchor="e",
                                   text="", fill="#6b6b76",
                                   font=("Segoe UI", int(5 * k)), tag="text")

        # перетаскивание и меню
        cv.bind("<ButtonPress-1>", self._press)
        cv.bind("<B1-Motion>", self._motion)
        cv.bind("<ButtonRelease-1>", self._release)
        menu = tk.Menu(self.root, tearoff=0, bg=BG, fg=FG, activebackground=ACCENT,
                       activeforeground="#000")
        menu.add_command(label="Обновить сейчас", command=self.poll)
        menu.add_command(label="Вернуть в правый нижний угол",
                         command=lambda: (self.cfg.pop("x", None), self.cfg.pop("y", None),
                                          save_conf(self.cfg), self._place()))
        menu.add_command(label="Перезапустить плеер с режимом для лайков",
                         command=self._ask_restart)
        menu.add_command(label="Версия %s" % VERSION, state="disabled")
        menu.add_separator()
        menu.add_command(label="Выйти", command=self._quit)
        self.menu = menu
        cv.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

    def _round_rect(self, cv, x1, y1, x2, y2, rad, **kw):
        pts = [x1 + rad, y1, x2 - rad, y1, x2, y1, x2, y1 + rad, x2, y2 - rad, x2, y2,
               x2 - rad, y2, x1 + rad, y2, x1, y2, x1, y2 - rad, x1, y1 + rad, x1, y1]
        return cv.create_polygon(pts, smooth=True, **kw)

    def _icon(self, cv, name, cx, cy, r):
        s = r * 0.62
        col = GLYPH_ON_ACCENT if name == "play" else FG
        if name == "play":
            self._icon_shape = "play"
            return [cv.create_polygon(cx - s * 0.7, cy - s, cx - s * 0.7, cy + s,
                                      cx + s, cy, fill=col, outline="", tag=("btn", name))]
        if name == "like":
            # «Мне нравится»: пустое сердце — не лайкнуто, заполненное — лайкнуто
            return [cv.create_text(cx, cy, text="\u2661", fill=col,
                                   font=("Segoe UI Symbol", int(r * 0.9)),
                                   tag=("btn", name))]
        if name == "prev":
            return [cv.create_polygon(cx + s * 0.35, cy - s, cx + s * 0.35, cy + s,
                                      cx - s * 0.65, cy, fill=col, outline="",
                                      tag=("btn", name)),
                    cv.create_rectangle(cx - s * 1.0, cy - s, cx - s * 0.72, cy + s,
                                        fill=col, outline="", tag=("btn", name))]
        return [cv.create_polygon(cx - s * 0.35, cy - s, cx - s * 0.35, cy + s,
                                  cx + s * 0.65, cy, fill=col, outline="",
                                  tag=("btn", name)),
                cv.create_rectangle(cx + s * 0.72, cy - s, cx + s * 1.0, cy + s,
                                    fill=col, outline="", tag=("btn", name))]

    def _hover(self, name, on):
        circ, glyphs = self.buttons[name]
        cv = self.cv
        if name == "play":
            col = ACCENT_HOVER if on else ACCENT
            gcol = GLYPH_ON_ACCENT
        elif name == "like":
            col = BTN_HOVER if on else BTN_BG
            if self.like_shown is True:
                gcol = ACCENT_HOVER if on else ACCENT
            elif self.like_shown is False:
                gcol = ACCENT if on else FG
            else:                       # режим для лайков недоступен
                gcol = ACCENT if on else "#6b6b76"
        else:
            col = BTN_HOVER if on else BTN_BG
            gcol = ACCENT if on else FG
        cv.itemconfig(circ, fill=col, outline=col)
        for it in glyphs:
            cv.itemconfig(it, fill=gcol)

    def _set_play_icon(self, playing):
        cv = self.cv
        circ, glyphs = self.buttons["play"]
        want = "play" if playing else "pause"
        if self._icon_shape == want:
            return
        self._icon_shape = want
        cx, cy, r = self._btn_geom("play")
        for it in glyphs:
            cv.delete(it)
        s = r * 0.62
        col = GLYPH_ON_ACCENT
        if playing:
            new = [cv.create_rectangle(cx - s * 0.85, cy - s, cx - s * 0.2, cy + s,
                                       fill=col, outline="", tag=("btn", "play")),
                   cv.create_rectangle(cx + s * 0.2, cy - s, cx + s * 0.85, cy + s,
                                       fill=col, outline="", tag=("btn", "play"))]
        else:
            new = [cv.create_polygon(cx - s * 0.7, cy - s, cx - s * 0.7, cy + s,
                                     cx + s, cy, fill=col, outline="",
                                     tag=("btn", "play"))]
        self.buttons["play"] = (circ, new)
        for item in new:
            cv.tag_bind(item, "<Button-1>", lambda e: self.act("play"))
            cv.tag_bind(item, "<Enter>", lambda e: self._hover("play", True))
            cv.tag_bind(item, "<Leave>", lambda e: self._hover("play", False))

    def _set_like_icon(self, state):
        """Сердце: заполненное — трек в «Мне нравится», пустое — лайк можно
        поставить, серое — режим для лайков сейчас недоступен."""
        item = self.buttons["like"][1][0]
        if state is True:
            text, col = "\u2665", ACCENT
        elif state is False:
            text, col = "\u2661", FG
        else:
            text, col = "\u2661", "#6b6b76"
        self.cv.itemconfig(item, text=text, fill=col)

    def _ask_restart(self):
        """Режим для лайков включается ключом при запуске приложения:
        если приложения запущены без него, предлагаем перезапустить."""
        pid, path = ym_main_proc()
        if not path:
            messagebox.showinfo("Яндекс Музыка",
                                "Приложение «Яндекс Музыка» сейчас не запущено.")
            return
        if not messagebox.askyesno(
                "Лайк трека",
                "Чтобы лайк работал, Яндекс Музыку нужно перезапустить с режимом "
                "для лайков.\n\nПерезапустить сейчас? Музыка остановится на "
                "несколько секунд."):
            return
        threading.Thread(target=self._restart_worker, args=(pid, path),
                         daemon=True).start()

    def _restart_worker(self, pid, path):
        try:
            subprocess.Popen(["taskkill", "/PID", str(pid), "/T", "/F"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2.5)
            subprocess.Popen([path, "--remote-debugging-port=%d" % self.like.port])
        except Exception:
            pass

    def _btn_geom(self, name):
        k = self.k
        w = int(PANEL_W * k)
        bx = w - int(59 * k) + self.off[name]
        return bx, int(20 * k), int(8 * k)

    # ---------- перетаскивание ----------
    def _press(self, e):
        tags = self.cv.find_withtag("current")
        kinds = [self.cv.gettags(i) for i in tags]
        if any(("btn" in t or "bar" in t) for t in kinds):
            self._bar_seek = tags and any("bar" in t for t in kinds)
            if self._bar_seek:
                self._seek(e.x)
            return
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _motion(self, e):
        if self._drag:
            self.root.geometry("+%d+%d" % (e.x_root - self._drag[0],
                                           e.y_root - self._drag[1]))

    def _release(self, e):
        if self._drag:
            self._drag = None
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
            self.cfg.pop("stopped", None)
            save_conf(self.cfg)

    def _quit(self):
        """Выход по меню: контролёр не поднимет панель, пока приложение
        не запустят заново (ключ stopped в config.json)."""
        self.cfg["stopped"] = int(time.time())
        save_conf(self.cfg)
        self.root.destroy()

    def _seek(self, x):
        if not self.duration or self.smtc.sess is None:
            return
        frac = max(0.0, min(1.0, (x - self.bar_x1) / float(self.bar_x2 - self.bar_x1)))
        target = datetime.timedelta(seconds=int(frac * self.duration.total_seconds()))
        self.smtc.cmds.put(("seek", target))
        self.pos_base = (target, _now())

    # ---------- управление ----------
    def act(self, what):
        """Кнопки: команда уходит в фоновый поток, ответ не ждём."""
        if what == "like":
            if not self.like.live and not self.like.probe():
                if not self._like_asked:        # не приставаем к пользователю дважды
                    self._like_asked = True
                    self.root.after(30, self._ask_restart)
                return
            if self.like.state is not None:
                self.like_shown = not self.like.state
                self._set_like_icon(self.like_shown)
            self.like.cmds.put("click")
            return
        if what == "play":
            self.playing = not self.playing
            self._set_play_icon(self.playing)
        self.smtc.cmds.put((what, None))

    # ---------- опрос ----------
    def poll(self):
        """Забирает снимок из фонового потока и перерисовывает панель."""
        snap = self.smtc.latest
        if snap is not self.snap:
            self.snap = snap
            if snap:
                self.duration = snap.get("end")
                pos = snap.get("position")
                # Яндекс присылает позицию с задержкой: обновляем базу только
                # при заметном скачке, иначе идём вперёд сами
                if pos is not None and (self.pos_base is None or self.pos_base[0] is None
                                        or abs((pos - self.pos_base[0]).total_seconds()) > 0.8):
                    self.pos_base = (pos, _now())
                self._apply_art(snap.get("thumb"))
            self._render(snap)
        if self.like.live:
            self._like_asked = False
        st = self.like.state if (snap and self._is_yandex(snap)) else None
        if st is not self.like_shown:
            self.like_shown = st
            self._set_like_icon(st)
        self.root.attributes("-topmost", True)
        if os.environ.get("YM_DEBUG"):
            print("poll: %r / %r playing=%s" % ((snap or {}).get("title"),
                                                (snap or {}).get("artist"), self.playing))
        self.root.after(700, self.poll)

    def _apply_art(self, blob):
        if Image is None:
            return
        if not blob:
            self.art_key = None
            return
        if self.art_key == hash(blob):
            return
        self.art_key = hash(blob)
        try:
            import io
            k = self.k
            side = int(COVER * k)
            img = Image.open(io.BytesIO(blob)).convert("RGBA")
            img = img.resize((side, side))
            mask = Image.new("L", (side, side), 0)
            from PIL import ImageDraw
            d = ImageDraw.Draw(mask)
            rad = int(5 * k)
            d.rounded_rectangle([0, 0, side - 1, side - 1], rad, fill=255)
            img.putalpha(mask)
            self.art_photo = ImageTk.PhotoImage(img)
            self.cv.itemconfig(self.cover_img, image=self.art_photo)
            self.cv.tag_lower(self.cover_box, self.cover_img)
            self.cv.coords(self.cover_box, 0, 0, 0, 0)
            self.cv.itemconfig(self.cover_box, state="hidden")
        except Exception:
            pass

    def _render(self, data):
        if data is None:
            self._set_text("", "")
            self.cv.itemconfig(self.hint, text="нет воспроизведения")
            self._set_play_icon(False)
            self.cv.itemconfig(self.time_id, text="")
            self.cv.coords(self.bar_fill, self.bar_x1, self.bar_y, self.bar_x1, self.bar_y)
            return
        self._set_text(data.get("title") or "", data.get("artist") or "")
        playing = data.get("status") == SmtcStatus.PLAYING
        if self.smtc.cmds.empty():            # не перерисовывать иконку во время нажатия
            self.playing = playing
            self._set_play_icon(playing)
        self.cv.itemconfig(self.hint, text="Яндекс Музыка" if self._is_yandex(data)
                           else "плеер")
        self._draw_progress()

    def _is_yandex(self, data):
        return bool(data and data.get("yandex"))

    def _set_text(self, title, artist):
        self.cv.itemconfig(self.title_id, text=self._fit(title, self.f_title) or "—")
        self.cv.itemconfig(self.artist_id, text=self._fit(artist, self.f_artist))

    def _fit(self, text, font):
        """Обрезает строку по ширине, без переноса на вторую строку."""
        text = (text or "").strip()
        if font.measure(text) <= self.text_w:
            return text
        while text and font.measure(text + "…") > self.text_w:
            text = text[:-1].rstrip()
        return text + "…"

    def _draw_progress(self):
        if not self.pos_base or not self.duration:
            return
        pos, ts = self.pos_base
        try:
            cur = pos.total_seconds() + (_now() - ts) if self.playing else pos.total_seconds()
            total = self.duration.total_seconds()
        except Exception:
            return
        if total <= 0:
            return
        cur = max(0.0, min(cur, total))
        frac = cur / total
        x2 = self.bar_x1 + int((self.bar_x2 - self.bar_x1) * frac)
        self.cv.coords(self.bar_fill, self.bar_x1, self.bar_y, x2, self.bar_y)
        self.cv.itemconfig(self.time_id,
                           text="%s / %s" % (fmt_time(datetime.timedelta(seconds=int(cur))),
                                             fmt_time(self.duration)))

    def animate(self):
        self._draw_progress()
        self.root.after(200, self.animate)


def _now():
    import time
    return time.monotonic()


# ---------- контролёр: панель вместе с приложением «Яндекс Музыка» ----------
def _mutex_taken(name):
    """True, если мьютекс уже принадлежит другому процессу."""
    kern = ctypes.windll.kernel32
    handle = kern.CreateMutexW(None, False, name)
    taken = kern.GetLastError() == 183          # ERROR_ALREADY_EXISTS
    if handle:
        kern.CloseHandle(handle)
    return taken


def _hold_mutex(name):
    """Занять мьютекс на всё время работы процесса; False, если он занят."""
    kern = ctypes.windll.kernel32
    kern.CreateMutexW(None, False, name)
    return kern.GetLastError() != 183


def panel_cmd():
    """Чем запускать панель: собранный .exe или pythonw + panel.py."""
    if getattr(sys, "frozen", False):
        return [target_exe()]
    py = sys.executable
    if os.path.basename(py).lower() == "python.exe":
        gui = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.exists(gui):
            py = gui
    return [py, os.path.abspath(__file__)]


def _write_pids(panel_pid=0):
    """pid контролёра и панели — чтобы снять автозапуск точечно."""
    try:
        with open(PID_FILE, "w", encoding="ascii") as fh:
            fh.write("%d %d\n" % (os.getpid(), panel_pid))
    except OSError:
        pass


def kill_pids():
    """Заккрыть контролёр и панель по pid-файлу (для исходников)."""
    pids = []
    try:
        with open(PID_FILE, "r", encoding="ascii") as fh:
            pids = [int(a) for a in fh.read().split() if a.isdigit()]
    except OSError:
        pass
    for pid in pids:
        if pid and pid != os.getpid():
            subprocess.Popen(["taskkill", "/PID", str(pid), "/F"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def watch_ym(interval=3.0):
    """Пока работает «Яндекс Музыка», работает и панель в правом нижнем углу.

    Панель поднимается при запуске приложения и при падении панели; «Выйти»
    в её меню гасит панель до следующего запуска приложения (ключ stopped).
    Ключ require_ym=false в config.json — панель работает всегда.
    Ключ disabled=true — контролёр закрывает панель и сам выходит.
    """
    _write_pids()
    was = False
    proc = None
    while True:
        try:
            cfg = load_conf()
            if cfg.get("disabled"):
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                try:
                    os.remove(PID_FILE)
                except OSError:
                    pass
                return
            running = bool(ym_main_proc()[0])
            if running and not was:              # приложение запустили заново
                cfg.pop("stopped", None)
                cfg.pop("disabled", None)
                save_conf(cfg)
            need = running or not cfg.get("require_ym", True)
            if (need and not cfg.get("stopped")
                    and (proc is None or proc.poll() is not None)
                    and not _mutex_taken(PANEL_MUTEX)):
                proc = subprocess.Popen(panel_cmd(), cwd=BASE,
                                        creationflags=0x08000000)   # без окна
                _write_pids(proc.pid)
            was = running
        except Exception:
            pass
        time.sleep(interval)


# ---------- установка (для собранного одного .exe) ----------
APP_NAME = "YandexMusicPanel"
VERSION = "1.1"                                  # номер и установочного файла, и панели
EXE_NAME = "%s-%s.exe" % (APP_NAME, VERSION)     # YandexMusicPanel-1.1.exe
LEGACY_EXE = "YandexMusicPanel.exe"              # имя установочного файла до 1.1
STARTUP_VBS = "YandexMusicPanel.vbs"             # ярлык в «Автозагрузке», без номера


def app_dir():
    """Куда ставится панель: %LOCALAPPDATA%\\YandexMusicPanel."""
    return os.path.join(os.environ.get("LOCALAPPDATA") or BASE, APP_NAME)


def target_exe():
    return os.path.join(app_dir(), EXE_NAME)


def startup_file():
    d = os.path.join(os.environ.get("APPDATA") or BASE,
                     "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    return os.path.join(d, STARTUP_VBS)


def _same_path(a, b):
    try:
        return os.path.abspath(a).lower() == os.path.abspath(b).lower()
    except (OSError, ValueError):
        return False


def _silent():
    """Без диалогов: YandexMusicPanel-<версия>.exe --install --silent."""
    return "--silent" in [a.lower() for a in sys.argv[1:]] or bool(os.environ.get("YM_SILENT"))


def _vbs_run(argv):
    """Содержимое .vbs из «Автозагрузки»: запуск без окна консоли.
    Кавычки через Chr(34): иначе VBScript путается в вложенных кавычках."""
    parts = ' & " " & '.join('Chr(34) & "%s" & Chr(34)' % a.replace('"', "")
                             for a in argv)
    return ('Set sh = CreateObject("WScript.Shell")\n'
            'sh.Run %s, 0, False\n' % parts)


def _drop_old_builds(dst):
    """Закрыть и удалить ранее установленный .exe (другое имя версии)."""
    keep = os.path.basename(dst).lower()
    names = {LEGACY_EXE}
    try:
        for f in os.listdir(app_dir()):
            if f.lower().startswith(APP_NAME.lower()) and f.lower().endswith(".exe"):
                names.add(f)
    except OSError:
        pass
    for name in names:
        if name.lower() == keep:
            continue
        subprocess.Popen(["taskkill", "/IM", name, "/F"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.remove(os.path.join(app_dir(), name))
        except OSError:
            pass


def install_self():
    """Двойной клик: копирование .exe в LOCALAPPDATA, автозапуск, старт."""
    dst_hint = target_exe() if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    if getattr(sys, "frozen", False):
        dst = target_exe()
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _drop_old_builds(dst)               # версия N уступает версии N+1
        if not _same_path(sys.executable, dst):
            shutil.copy2(sys.executable, dst)
    vbs = startup_file()
    try:
        os.makedirs(os.path.dirname(vbs), exist_ok=True)
        with open(vbs, "w", encoding="mbcs") as fh:
            fh.write(_vbs_run(panel_cmd() + ["--watch"]))
        autostart = "включён" if os.path.exists(vbs) else "не удалось включить"
    except OSError:
        autostart = "не удалось включить (%s)" % vbs
    cfg = load_conf()
    cfg.pop("disabled", None)
    cfg.pop("stopped", None)
    save_conf(cfg)
    subprocess.Popen(panel_cmd() + ["--watch"], cwd=BASE,
                     creationflags=0x08000000)
    if not _silent():
        messagebox.showinfo(
            "Панель Яндекс Музыки %s" % VERSION,
            "Панель %s установлена: она появляется сама, как только запущена "
            "«Яндекс Музыка»,\nи держится в правом нижнем углу.\n\n"
            "Файл:  %s\nАвтозапуск контролёра при входе в Windows: %s\n\n"
            "Удаление:  %s --uninstall" % (VERSION, dst_hint, autostart, EXE_NAME))


def uninstall_self():
    """Снять автозапуск и закрыть панель; для собранного .exe — удалить папку."""
    if not _silent():
        if not messagebox.askyesno("Панель Яндекс Музыки",
                                   "Удалить панель: снять автозапуск и закрыть её?"):
            return
    cfg = load_conf()
    cfg["disabled"] = True          # контролёр погасит панель и выйдет
    save_conf(cfg)
    kill_pids()
    try:
        os.remove(startup_file())
    except OSError:
        pass
    if not getattr(sys, "frozen", False):
        if not _silent():
            messagebox.showinfo("Панель Яндекс Музыки",
                                "Готово: автозапуск снят, панель закрыта. "
                                "Исходники не тронуты.")
        return
    # удаляем папку установленной панели через отдельную команду:
    # процесс может удалять сам себя
    import tempfile
    folder = app_dir()
    bat = os.path.join(tempfile.gettempdir(), "ympanel_uninstall.cmd")
    kill = "".join('taskkill /IM "%s" /F >nul 2>&1\r\n' % n
                   for n in (EXE_NAME, LEGACY_EXE))
    with open(bat, "w", encoding="ascii") as fh:
        fh.write("@echo off\r\ntimeout /t 5 /nobreak >nul\r\n"
                 "%s"
                 'del /q /f "%s"\r\n'
                 'del /q /f "%s"\r\n'
                 'timeout /t 3 /nobreak >nul\r\n'
                 'rmdir /s /q "%s"\r\n'
                 'del "%%~f0"\r\n'
                 % (kill, startup_file(), os.path.join(folder, LEGACY_EXE), folder))
    subprocess.Popen(["cmd", "/c", bat],
                     creationflags=0x00000008 | 0x00000200)   # detached, new process group
    if not _silent():
        messagebox.showinfo("Панель Яндекс Музыки",
                            "Готово: автозапуск снят, файлы будут удалены через пару секунд.")


def main():
    try:  # чёткий текст на экранах с масштабированием
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in ("--version", "/version", "-v", "-version"):
        if not getattr(sys, "frozen", False):
            print("%s %s" % (APP_NAME, VERSION))
        elif not _silent():
            messagebox.showinfo("Панель Яндекс Музыки",
                                "Версия %s\nФайл: %s" % (VERSION, target_exe()))
        return
    if arg in ("--uninstall", "/uninstall", "-u"):
        uninstall_self()
        return
    if arg in ("--install", "-i"):
        install_self()                # и для .exe, и для исходников
        return
    if getattr(sys, "frozen", False) and arg not in (
            "--run", "-r", "--silent", "--watch", "-w") \
            and not _same_path(sys.executable, target_exe()):
        install_self()                # первый запуск скачанного файла = установка
        return
    if arg in ("--watch", "-w"):      # контролёр: панель вместе с приложением
        if not _hold_mutex(WATCH_MUTEX):
            return
        watch_ym()
        return
    if "scale" not in load_conf():
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except Exception:
            dpi = 96
        cfg = load_conf()
        cfg["scale"] = round(max(1.0, dpi / 96.0), 2)
        save_conf(cfg)
    if not _hold_mutex(PANEL_MUTEX):     # панель уже запущена
        return
    Panel().root.mainloop()


if __name__ == "__main__":
    main()
