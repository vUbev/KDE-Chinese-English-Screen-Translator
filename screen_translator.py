#!/usr/bin/env python3
"""
Screen Translator  -  Chinese -> English screen reader for KDE Plasma.

    screen-translator              same as "trigger" (what the Alt+Q shortcut runs)
    screen-translator trigger      scan the screen now (starts the background daemon if needed)
    screen-translator daemon       run the background daemon in the foreground
    screen-translator setup        download the dictionary + offline translation model
    screen-translator lookup 词    print a dictionary card in the terminal (for testing)

In the overlay:
    click a highlighted word ... dictionary popup
    mouse wheel / arrow keys .... lengthen / shorten the selected word (Ctrl or Shift = left edge)
    "Save word" ................. appends  WORD<TAB>DEFINITION  to your saved-words file
    click anywhere else / Esc ... dismiss
"""
import os
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import st_core as core  # noqa: E402  (light: stdlib only)


# --------------------------------------------------------------------------
# tiny CLI commands that must NOT import Qt (so that the shortcut is instant)
# --------------------------------------------------------------------------
def _connect() -> socket.socket | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(core.SOCKET_PATH)
        return s
    except OSError:
        s.close()
        return None


def cmd_trigger() -> int:
    s = _connect()
    if s:
        s.sendall(b"trigger\n")
        s.close()
        return 0
    # no daemon yet: start one and let it scan as soon as it is ready
    subprocess.Popen([sys.executable, os.path.realpath(__file__), "daemon", "--trigger-on-start"],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def cmd_setup() -> int:
    cfg = core.load_config()
    ok = True
    dest = core.Path(cfg["cedict_path"]).expanduser()
    if "--force" in sys.argv or not dest.exists():
        ok &= core.install_cedict(dest)
    else:
        print(f"Dictionary already present: {dest}")
    if cfg["translator"] == "argos":
        try:
            core.Translator()
            print("Translation model already installed.")
        except Exception:
            if not core.install_translation_model():
                print("\nThe dictionary will still work, but the 'Context Translation' line will be empty.\n"
                      "Re-run  screen-translator setup  when you are online.")
                ok = False
    print("Setup finished." if ok else "Setup finished with problems (see above).")
    return 0 if ok else 1


def cmd_lookup(args) -> int:
    cfg = core.load_config()
    d = core.Dictionary(cfg["cedict_path"])
    for word in args:
        card = d.build_card(word)
        print(f"== {word}")
        for e in card["entries"]:
            print(f"{e['simp']} | {e['trad']}   {core.pinyin_plain(e['pinyin'])}")
            print("   " + "; ".join(e["defs"]))
        for c in card["components"]:
            print(f"  {c['simp']} ({c['trad']}) {core.pinyin_plain(c['pinyin'])}  {c['gloss']}")
        print("saved line:", repr(f"{word}\t{core.save_definition(card)}"))
    return 0


if __name__ == "__main__":
    _cmd = sys.argv[1] if len(sys.argv) > 1 else "trigger"
    if _cmd == "trigger":
        sys.exit(cmd_trigger())
    if _cmd == "setup":
        sys.exit(cmd_setup())
    if _cmd == "lookup":
        sys.exit(cmd_lookup(sys.argv[2:]))
    if _cmd != "daemon":
        print(__doc__)
        sys.exit(0 if _cmd in ("-h", "--help", "help") else 2)

# --------------------------------------------------------------------------
# GUI daemon (imports Qt)
# --------------------------------------------------------------------------
import fcntl  # noqa: E402
import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal, Slot  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen, QPixmap  # noqa: E402
from PySide6.QtNetwork import QLocalServer  # noqa: E402
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QVBoxLayout, QWidget  # noqa: E402

log = logging.getLogger("screen-translator")


def notify(msg: str):
    log.info("notify: %s", msg)
    try:
        subprocess.Popen(["notify-send", "-a", "Screen Translator", "-t", "7000", "Screen Translator", msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def capture_screen() -> tuple[str, bool]:
    """Screenshot of the monitor KDE considers current. Returns (png path, is_temporary)."""
    test = os.environ.get("ST_TEST_IMAGE")  # debugging hook: scan this image instead of the screen
    if test:
        return test, False
    path = str(core.RUNTIME_DIR / f"st-{os.getpid()}-{time.time_ns()}.png")
    r = subprocess.run(["spectacle", "--background", "--nonotify", "--current", "--output", path],
                       capture_output=True, text=True, timeout=20)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError("Spectacle did not produce a screenshot: " + (r.stderr or r.stdout or "no output").strip()[:300])
    return path, True


class Ocr:
    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()

    def _get(self):
        if self._engine is None:
            logging.getLogger("RapidOCR").setLevel(logging.ERROR)
            from rapidocr import RapidOCR
            try:
                self._engine = RapidOCR(params={"Global.log_level": "critical"})
            except Exception:
                self._engine = RapidOCR()
        return self._engine

    def warm(self):
        import numpy as np
        with self._lock:
            self._get()(np.full((96, 320, 3), 255, dtype=np.uint8))

    def run(self, path: str, cfg: dict):
        with self._lock:
            r = self._get()(path, return_word_box=True)
        return core.build_lines(r.boxes, r.txts, r.scores, getattr(r, "word_results", None),
                                cfg["min_confidence"], cfg["only_chinese"])


class Signals(QObject):
    captured = Signal(int, object)     # scan id, QImage
    scanned = Signal(int, object)      # scan id, list[OcrLine]
    failed = Signal(int, str)
    translated = Signal(str, str)      # sentence, translation


class Popup(QFrame):
    def __init__(self, parent, width):
        super().__init__(parent)
        self.setObjectName("popup")
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        self.setFixedWidth(width)
        self.setStyleSheet("""
            #popup { background-color: #1b1c23; border: 1px solid #3b3e4d; border-radius: 12px; }
            QLabel { color: #f0f0f5; background: transparent; }
            QPushButton { background: #2d3040; color: #f0f0f5; border: 1px solid #444860;
                          border-radius: 8px; padding: 6px 14px; }
            QPushButton:hover { background: #3a3e54; }
            QPushButton:disabled { color: #8b90a0; background: #24262f; }
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(8)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.save = QPushButton("★  Save word")
        self.save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay.addWidget(self.label)
        lay.addWidget(self.save, 0, Qt.AlignmentFlag.AlignRight)

    def set_content(self, html: str):
        self.label.setText(html)
        self.layout().activate()
        self.adjustSize()

    def set_save_state(self, enabled: bool, saved: bool):
        self.save.setEnabled(enabled and not saved)
        self.save.setText("✓  Saved" if saved else "★  Save word")

    def mousePressEvent(self, e):
        e.accept()

    def mouseReleaseEvent(self, e):
        e.accept()

    def wheelEvent(self, e):
        e.accept()


class Overlay(QWidget):
    closed = Signal()

    def __init__(self, app: "App", sid: int, pixmap: QPixmap, screen):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.app, self.sid, self.pm = app, sid, pixmap
        self.lines: list[core.OcrLine] = []
        self.scanning = True
        self.hover = -1
        self.ctx: core.Context | None = None
        self.sel = (0, 0)
        self.card = None
        self._wheel = 0
        self.setWindowTitle("Screen Translator")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if screen is not None:
            self.setScreen(screen)
            self.setGeometry(screen.geometry())
        self.popup = Popup(self, 420)
        self.popup.hide()
        self.popup.save.clicked.connect(self.save_current)
        app.sig.translated.connect(self.on_translated)

    # ---- coordinates --------------------------------------------------
    def kx(self):
        return self.width() / self.pm.width()

    def ky(self):
        return self.height() / self.pm.height()

    def rect_of(self, x0, y0, x1, y1) -> QRectF:
        return QRectF(x0 * self.kx(), y0 * self.ky(), (x1 - x0) * self.kx(), (y1 - y0) * self.ky())

    def selection_rects(self) -> list[QRectF]:
        if not self.ctx:
            return []
        a, b = self.sel
        per_line: dict[int, list[float]] = {}
        for li, ci in self.ctx.chars[a:b]:
            x0, x1 = self.lines[li].spans[ci]
            cur = per_line.setdefault(li, [x0, x1])
            cur[0], cur[1] = min(cur[0], x0), max(cur[1], x1)
        return [self.rect_of(v[0], self.lines[li].y0, v[1], self.lines[li].y1) for li, v in per_line.items()]

    # ---- state --------------------------------------------------------
    def set_lines(self, lines):
        self.lines = lines
        self.scanning = False
        self.update()

    def hit_line(self, pos) -> int:
        ix, iy = pos.x() / self.kx(), pos.y() / self.ky()
        best, best_d = -1, 1e18
        for i, l in enumerate(self.lines):
            slack_x, slack_y = 6, max(3, l.h * 0.15)
            if l.x0 - slack_x <= ix <= l.x1 + slack_x and l.y0 - slack_y <= iy <= l.y1 + slack_y:
                d = (ix - (l.x0 + l.x1) / 2) ** 2 + (iy - l.cy) ** 2
                if d < best_d:
                    best, best_d = i, d
        return best

    def char_at(self, li: int, ix: float) -> int:
        spans = self.lines[li].spans
        for k, (a, b) in enumerate(spans):
            if a <= ix <= b:
                return k
        return min(range(len(spans)), key=lambda k: min(abs(ix - spans[k][0]), abs(ix - spans[k][1])))

    # ---- painting -----------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(self.rect(), self.pm)
        p.fillRect(self.rect(), QColor(0, 0, 0, 70))
        for i, l in enumerate(self.lines):
            r = self.rect_of(l.x0 - 3, l.y0 - 2, l.x1 + 3, l.y1 + 2)
            p.setPen(QPen(QColor(255, 205, 0, 210), 1.5))
            p.setBrush(QColor(255, 205, 0, 105 if i == self.hover else 55))
            p.drawRoundedRect(r, 4, 4)
        for r in self.selection_rects():
            p.setPen(QPen(QColor(70, 160, 255), 2))
            p.setBrush(QColor(70, 160, 255, 110))
            p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 4, 4)
        if self.scanning:
            self._pill(p, "Scanning…")
        elif not self.lines:
            self._pill(p, "No Chinese text found  -  click anywhere to close")
        p.end()

    def _pill(self, p: QPainter, text: str):
        f = QFont()
        f.setPointSize(11)
        p.setFont(f)
        fm = p.fontMetrics()
        w, h = fm.horizontalAdvance(text) + 40, fm.height() + 18
        r = QRectF((self.width() - w) / 2, 28, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 22, 30, 235))
        p.drawRoundedRect(r, h / 2, h / 2)
        p.setPen(QColor(240, 240, 245))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

    # ---- input --------------------------------------------------------
    def mouseMoveEvent(self, e):
        i = self.hit_line(e.position())
        if i != self.hover:
            self.hover = i
            self.setCursor(Qt.CursorShape.PointingHandCursor if i >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            self.close()
            return
        li = self.hit_line(e.position())
        if li < 0:
            self.close()  # click anywhere else dismisses
            return
        ci = self.char_at(li, e.position().x() / self.kx())
        ctx = core.build_context(self.lines, li, ci, self.app.seg)
        if ctx is None:
            return  # punctuation / non-Chinese character: ignore
        self.ctx = ctx
        _, a, b = ctx.tokens[ctx.tok]
        self.sel = (a, b)
        self.app.request_translation(ctx.sentence.strip())
        self.refresh()

    def _resize_sel(self, right: int = 0, left: int = 0):
        if not self.ctx:
            return
        s, (a, b) = self.ctx.sentence, self.sel
        if right > 0 and b < len(s) and core.is_cjk(s[b]):
            b += 1
        elif right < 0 and b > a + 1:
            b -= 1
        if left > 0 and a > 0 and core.is_cjk(s[a - 1]):
            a -= 1
        elif left < 0 and a < b - 1:
            a += 1
        if (a, b) != self.sel:
            self.sel = (a, b)
            self.refresh()

    def wheelEvent(self, e):
        self._wheel += e.angleDelta().y()
        steps = int(self._wheel / 120)
        if steps:
            self._wheel -= steps * 120
            ctrl = bool(e.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
            for _ in range(abs(steps)):
                d = 1 if steps > 0 else -1
                self._resize_sel(left=d) if ctrl else self._resize_sel(right=d)

    def keyPressEvent(self, e):
        k, shift = e.key(), bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if k == Qt.Key.Key_Escape:
            self.close()
        elif k == Qt.Key.Key_Right:
            self._resize_sel(left=-1) if shift else self._resize_sel(right=1)
        elif k == Qt.Key.Key_Left:
            self._resize_sel(left=1) if shift else self._resize_sel(right=-1)

    # ---- popup --------------------------------------------------------
    def refresh(self):
        a, b = self.sel
        self.card = self.app.dict.build_card(self.ctx.sentence[a:b])
        self.update_popup(reposition=True)
        self.update()

    def update_popup(self, reposition=False):
        if not self.card:
            return
        sentence = self.ctx.sentence.strip()
        tr = self.app.tcache.get(sentence)
        if tr is None:
            tr = "Translating…" if self.app.cfg["translator"] == "argos" else None
        html = core.card_html(self.card, tr, self.app.cfg["tone_colors"])
        self.popup.set_content(html)
        word = self.saved_word()
        self.popup.set_save_state(self.card["found"], core.is_saved(self.app.saved_path(), word))
        if reposition or not self.popup.isVisible():
            self.place_popup()
        self.popup.show()
        self.popup.raise_()

    def place_popup(self):
        rects = self.selection_rects()
        if not rects:
            return
        anchor = rects[0]
        for r in rects[1:]:
            anchor = anchor.united(r)
        pw, ph = self.popup.width(), self.popup.height()
        x = min(max(anchor.left(), 10), max(10, self.width() - pw - 10))
        y = anchor.bottom() + 12
        if y + ph > self.height() - 10:
            y = anchor.top() - ph - 12
        if y < 10:
            y = max(10, self.height() - ph - 10)
        self.popup.move(int(x), int(y))

    def saved_word(self) -> str:
        c = self.card
        return c["entries"][0]["simp"] if c["found"] else c["text"]

    def save_current(self):
        if not self.card or not self.card["found"]:
            return
        res = core.save_word(self.app.saved_path(), self.saved_word(), core.save_definition(self.card))
        log.info("save %s -> %s", self.saved_word(), res)
        self.popup.set_save_state(True, True)

    @Slot(str, str)
    def on_translated(self, sentence: str, _text: str):
        if self.ctx and sentence == self.ctx.sentence.strip():
            self.update_popup()

    def closeEvent(self, e):
        try:
            self.app.sig.translated.disconnect(self.on_translated)
        except (RuntimeError, TypeError):
            pass
        self.closed.emit()
        super().closeEvent(e)


class App(QObject):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.sig = Signals()
        self.dict = core.Dictionary(cfg["cedict_path"])
        self.seg = core.Segmenter(self.dict)
        self.ocr = Ocr()
        self.translator = None
        self._tr_error = None
        self._tr_lock = threading.Lock()
        self.tcache: dict[str, str] = {}
        self.overlay: Overlay | None = None
        self.scan_id = 0
        self.sig.captured.connect(self.on_captured)
        self.sig.scanned.connect(self.on_scanned)
        self.sig.failed.connect(self.on_failed)

        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(core.SOCKET_PATH)
        if not self.server.listen(core.SOCKET_PATH):
            raise RuntimeError("cannot listen on " + core.SOCKET_PATH)
        self.server.newConnection.connect(self.on_connection)

    def saved_path(self) -> str:
        return os.path.expanduser(self.cfg["saved_words_file"])

    # ---- trigger ------------------------------------------------------
    @Slot()
    def on_connection(self):
        while self.server.hasPendingConnections():
            conn = self.server.nextPendingConnection()
            conn.waitForReadyRead(300)
            if b"trigger" in bytes(conn.readAll()):
                self.trigger()
            conn.disconnectFromServer()

    def trigger(self):
        if self.overlay is not None:  # pressing the shortcut again toggles
            self.overlay.close()
            return
        self.scan_id += 1
        threading.Thread(target=self._scan_job, args=(self.scan_id,), daemon=True).start()

    def _scan_job(self, sid: int):
        path, temp = None, False
        try:
            path, temp = capture_screen()
            img = QImage(path)
            if img.isNull():
                raise RuntimeError("could not read the screenshot")
            self.sig.captured.emit(sid, img)
            lines = self.ocr.run(path, self.cfg)
            log.info("scan %d: %d text lines", sid, len(lines))
            self.sig.scanned.emit(sid, lines)
        except Exception as ex:
            log.exception("scan failed")
            self.sig.failed.emit(sid, str(ex))
        finally:
            if temp and path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def pick_screen(self, pm: QPixmap):
        screens = QGuiApplication.screens()
        match = [s for s in screens
                 if abs(round(s.size().width() * s.devicePixelRatio()) - pm.width()) <= 2
                 and abs(round(s.size().height() * s.devicePixelRatio()) - pm.height()) <= 2]
        if len(match) == 1:
            return match[0]
        from PySide6.QtGui import QCursor
        under_cursor = QGuiApplication.screenAt(QCursor.pos())
        if under_cursor in match or not match:
            return under_cursor or QGuiApplication.primaryScreen()
        return match[0]

    @Slot(int, object)
    def on_captured(self, sid: int, img: QImage):
        if sid != self.scan_id or self.overlay is not None:
            return
        pm = QPixmap.fromImage(img)
        self.overlay = Overlay(self, sid, pm, self.pick_screen(pm))
        self.overlay.closed.connect(self.on_overlay_closed)
        self.overlay.showFullScreen()
        self.overlay.activateWindow()
        self.overlay.setFocus()

    @Slot(int, object)
    def on_scanned(self, sid: int, lines):
        if self.overlay is not None and self.overlay.sid == sid:
            self.overlay.set_lines(lines)

    @Slot(int, str)
    def on_failed(self, sid: int, msg: str):
        if sid == self.scan_id:
            if self.overlay is not None:
                self.overlay.close()
            notify("Scan failed: " + msg)

    @Slot()
    def on_overlay_closed(self):
        self.overlay = None

    # ---- translation --------------------------------------------------
    def get_translator(self):
        if self.cfg["translator"] != "argos":
            return None
        if self.translator is None and self._tr_error is None:
            try:
                self.translator = core.Translator()
            except Exception as ex:
                self._tr_error = str(ex)
                log.warning("translator unavailable: %s", ex)
        return self.translator

    def request_translation(self, sentence: str):
        if not sentence or self.cfg["translator"] != "argos":
            return
        if sentence in self.tcache:
            self.sig.translated.emit(sentence, self.tcache[sentence])
            return
        threading.Thread(target=self._translate_job, args=(sentence,), daemon=True).start()

    def _translate_job(self, sentence: str):
        with self._tr_lock:
            tr = self.get_translator()
            if tr is None:
                out = "(translation model not installed - run: screen-translator setup)"
            else:
                try:
                    out = tr.translate(sentence) or "(no translation)"
                except Exception as ex:
                    log.exception("translate failed")
                    out = f"(translation failed: {ex})"
            if tr is not None and not out.startswith("("):
                self.tcache[sentence] = out
                if len(self.tcache) > 200:
                    self.tcache.pop(next(iter(self.tcache)))
        self.sig.translated.emit(sentence, out)

    def warmup(self):
        for name, fn in (("jieba", self.seg.warm), ("ocr", self.ocr.warm),
                         ("translator", lambda: (self.get_translator() and self.translator.translate("你好")))):
            try:
                fn()
            except Exception:
                log.exception("warm-up of %s failed", name)
        log.info("warm-up done")


def run_daemon(trigger_on_start: bool) -> int:
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(core.DATA_DIR / "screen-translator.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    lock = open(core.RUNTIME_DIR / "screen-translator.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # another daemon is already running
    cfg = core.load_config()
    if not os.path.exists(os.path.expanduser(cfg["cedict_path"])):
        notify("Dictionary missing. Run:  screen-translator setup")
        return 1
    qt = QApplication(sys.argv[:1])
    qt.setQuitOnLastWindowClosed(False)
    qt.setApplicationName("Screen Translator")
    qt.setDesktopFileName("screen-translator")
    try:
        daemon = App(cfg)
    except Exception as ex:
        log.exception("startup failed")
        notify(f"Could not start: {ex}")
        return 1
    threading.Thread(target=daemon.warmup, daemon=True).start()
    if trigger_on_start:
        QTimer.singleShot(0, daemon.trigger)
    log.info("daemon ready")
    return qt.exec()


if __name__ == "__main__":
    sys.exit(run_daemon("--trigger-on-start" in sys.argv))
