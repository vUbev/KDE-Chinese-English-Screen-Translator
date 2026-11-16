"""
Screen Translator - core logic (no GUI imports in here).

Dictionary (CC-CEDICT), tone-coloured pinyin, word segmentation, OCR line
handling, sentence/context extraction, offline translation and the
"saved words" file.
"""
from __future__ import annotations

import csv
import html
import json
import logging
import os
import re
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

APP = "screen-translator"
HOME = Path.home()
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / APP
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / APP
CONFIG_FILE = CONFIG_DIR / "config.json"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
SOCKET_PATH = str(RUNTIME_DIR / f"{APP}.sock")
MODEL_DIR = DATA_DIR / "models" / "zh_en"

TONE_COLORS_LIGHT = {  # for light backgrounds (darker, saturated colours stay readable)
    1: "#e30000",  # first tone
    2: "#01b31c",  # second tone
    3: "#150ff0",  # third tone
    4: "#8800bf",  # fourth tone
    5: "#777777",  # fifth / neutral tone
}
TONE_COLORS_DARK = {  # for dark backgrounds
    1: "#ff8080",
    2: "#80ff80",
    3: "#8080ff",
    4: "#df80ff",
    5: "#c7c7c7",
}

THEMES = {
    "dark": dict(bg="#1b1c23", border="#3b3e4d", text="#f0f0f5", muted="#8b90a0", body="#d4d7e0",
                 btn_bg="#2d3040", btn_border="#444860", btn_hover="#3a3e54",
                 btn_off_bg="#24262f", btn_off_text="#8b90a0"),
    "light": dict(bg="#fbfbfd", border="#c9ccd6", text="#1c1d24", muted="#6b7080", body="#3a3d4a",
                  btn_bg="#eceef4", btn_border="#c3c7d4", btn_hover="#dfe2ec",
                  btn_off_bg="#f1f2f6", btn_off_text="#9a9eac"),
}


def get_theme(cfg: dict, dark: bool) -> dict:
    t = dict(THEMES["dark" if dark else "light"])
    t["tones"] = cfg["tone_colors_dark" if dark else "tone_colors_light"]
    return t


def kde_prefers_dark() -> bool | None:
    """Read KDE's colour scheme from ~/.config/kdeglobals. None = could not tell."""
    path = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "kdeglobals"
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    section = bg = scheme = None
    for line in lines:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line:
            k, v = line.split("=", 1)
            if section == "Colors:Window" and k == "BackgroundNormal":
                bg = v
            elif section == "General" and k == "ColorScheme":
                scheme = v
    if bg:
        try:
            r, g, b = [int(x) for x in bg.split(",")[:3]]
            return (0.299 * r + 0.587 * g + 0.114 * b) < 128
        except ValueError:
            pass
    if scheme:
        return "dark" in scheme.lower()
    return None

DEFAULT_CONFIG = {
    # Where "Save word" appends lines of:  WORD<TAB>DEFINITION
    "saved_words_file": str(HOME / "saved_words.txt"),
    "cedict_path": str(DATA_DIR / "cedict_ts.u8"),
    # "argos" = offline translation model, "none" = dictionary only
    "translator": "argos",
    "min_confidence": 0.5,
    "only_chinese": True,  # only highlight OCR lines that contain Chinese
    "theme": "auto",       # "auto" (follow KDE), "dark" or "light"
    "tone_colors_dark": TONE_COLORS_DARK,
    "tone_colors_light": TONE_COLORS_LIGHT,
}

log = logging.getLogger(APP)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        for k, v in user.items():
            if k in ("tone_colors_dark", "tone_colors_light") and isinstance(v, dict):
                cfg[k].update({int(a): b for a, b in v.items()})
            elif k == "tone_colors" and isinstance(v, dict):  # old config format -> light palette
                cfg["tone_colors_light"].update({int(a): b for a, b in v.items()})
            else:
                cfg[k] = v
    except FileNotFoundError:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    except Exception as ex:  # broken JSON etc: keep defaults
        log.warning("could not read %s: %s", CONFIG_FILE, ex)
    cfg.pop("tone_colors", None)
    for key in ("tone_colors_dark", "tone_colors_light"):
        cfg[key] = {int(k): v for k, v in cfg[key].items()}
    return cfg


# --------------------------------------------------------------------------
# characters
# --------------------------------------------------------------------------
def is_cjk(c: str) -> bool:
    o = ord(c)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF
            or 0x20000 <= o <= 0x2A6DF or 0x2A700 <= o <= 0x2EBEF)


def has_cjk(s: str) -> bool:
    return any(is_cjk(c) for c in s)


# --------------------------------------------------------------------------
# pinyin
# --------------------------------------------------------------------------
_MARKS = {"a": "āáǎà", "e": "ēéěè", "i": "īíǐì", "o": "ōóǒò", "u": "ūúǔù", "ü": "ǖǘǚǜ"}


def mark_syllable(syl: str) -> tuple[str, int]:
    """'huan2' -> ('huán', 2);  'lu:4' -> ('lǜ', 4);  'ma5' -> ('ma', 5)."""
    tone = 5
    base = syl
    if syl and syl[-1] in "12345":
        tone, base = int(syl[-1]), syl[:-1]
    base = base.replace("u:", "ü").replace("U:", "Ü")
    if tone == 5 or not base:
        return base, tone
    low = base.lower()
    idx = -1
    if "a" in low:
        idx = low.index("a")
    elif "e" in low:
        idx = low.index("e")
    elif "ou" in low:
        idx = low.index("ou")
    else:
        for i in range(len(low) - 1, -1, -1):
            if low[i] in "iouü":
                idx = i
                break
    if idx < 0:
        return base, tone
    marked = _MARKS[low[idx]][tone - 1]
    if base[idx].isupper():
        marked = marked.upper()
    return base[:idx] + marked + base[idx + 1:], tone


def pinyin_list(pin: str) -> list[tuple[str, int]]:
    return [mark_syllable(s) for s in pin.split()]


def pinyin_plain(pin: str) -> str:
    return " ".join(t for t, _ in pinyin_list(pin))


def pinyin_joined(pin: str) -> str:
    """Pinyin written as one word, like 'zhǔyǔ' (an apostrophe where needed: 'xī'ān')."""
    out = ""
    for text, _ in pinyin_list(pin):
        if out and text[:1].lower() in "aeoāáǎàēéěèōóǒò":
            out += "'"
        out += text
    return out


def pinyin_html(pin: str, colors: dict) -> str:
    return " ".join(
        f'<span style="color:{colors.get(tone, colors[5])}">{html.escape(text)}</span>'
        for text, tone in pinyin_list(pin)
    )


# --------------------------------------------------------------------------
# CC-CEDICT
# --------------------------------------------------------------------------
_LINE = re.compile(r"^(\S+) (\S+) \[([^\]]*)\] /(.+)/\s*$")
_HAN = r"[㐀-鿿\U00020000-\U0002ebef]+"
_PAIR = re.compile(rf"({_HAN})\|({_HAN})")
_PY_BRACKET = re.compile(r"\[([A-Za-z:·]+[1-5](?: [A-Za-z:·]+[1-5])*)\]")
_WEAK = re.compile(r"^(surname |variant of |old variant of |erhua variant|see |see also |"
                   r"also written |Japanese variant|archaic variant)")


class Entry:
    __slots__ = ("trad", "simp", "pinyin", "defs")

    def __init__(self, trad, simp, pinyin, defs):
        self.trad, self.simp, self.pinyin, self.defs = trad, simp, pinyin, defs

    @property
    def weak(self) -> bool:
        return all(_WEAK.match(d) for d in self.defs)


def clean_def(d: str) -> str:
    d = _PAIR.sub(r"\2", d)  # '個|个' -> '个'
    d = _PY_BRACKET.sub(
        lambda m: "[" + " ".join(mark_syllable(s)[0] for s in m.group(1).split()) + "]", d)
    return d


_JUNK_SENSE = re.compile(r"^(abbr\. |abbreviation |used in |surname |variant of |old variant of |erhua variant|"
                         r"see |see also |also written |Japanese variant|archaic variant|CL:|Taiwan pr\.|also pr\.)",
                         re.I)


def good_defs(entry: Entry, limit: int = 3) -> list[str]:
    """The meanings worth showing for a single character: skips cross-references,
    abbreviations ("abbr. for Israel"), "used in 家伙" and surname-only senses."""
    return [clean_def(d) for d in entry.defs if not _JUNK_SENSE.match(d)][:limit]


def display_defs(entry: Entry, limit: int = 8) -> list[str]:
    out = [clean_def(d) for d in entry.defs if not d.startswith("CL:")]
    return (out or [clean_def(d) for d in entry.defs])[:limit]


class Dictionary:
    def __init__(self, path: str | Path):
        self.index: dict[str, list[Entry]] = {}
        self.maxlen = 1
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                m = _LINE.match(line)
                if not m:
                    continue
                trad, simp, pin, defs = m.groups()
                e = Entry(trad, simp, pin, defs.split("/"))
                for key in {simp, trad}:
                    self.index.setdefault(key, []).append(e)
                    if len(key) > self.maxlen:
                        self.maxlen = len(key)
        for lst in self.index.values():
            if len(lst) > 1:
                lst.sort(key=lambda e: e.weak)  # stable: real meanings first

    def has(self, word: str) -> bool:
        return word in self.index

    def lookup(self, word: str) -> list[Entry]:
        return self.index.get(word, [])

    def components(self, simp: str, trad: str | None = None, syllables: list[str] | None = None) -> list[dict]:
        out = []
        use_syl = syllables if syllables and len(syllables) == len(simp) else None
        for i, ch in enumerate(simp):
            if not is_cjk(ch):
                continue
            cands = [e for e in self.index.get(ch, []) if len(e.simp) == 1]
            syl = use_syl[i] if use_syl else None

            def rank(e):
                # 1) an entry with a real meaning beats "abbr. for ..." / "used in ..." / "surname ..."
                # 2) then prefer the reading this word uses, 3) then the same capitalisation (xi vs Xi)
                return (0 if good_defs(e) else 1,
                        0 if syl and e.pinyin.lower() == syl.lower() else 1,
                        0 if syl and e.pinyin == syl else 1)

            pick = min(cands, key=rank) if cands else None  # min() keeps CC-CEDICT order on ties
            t = trad[i] if trad and len(trad) == len(simp) else (pick.trad if pick else ch)
            pin = syl if syl else (pick.pinyin if pick else "")
            defs = (good_defs(pick) or display_defs(pick, 3)) if pick else []
            gloss = ""
            for dd in defs[:3]:  # up to 3 meanings, but keep the line short
                if gloss and len(gloss) + len(dd) > 80:
                    break
                gloss = f"{gloss}; {dd}" if gloss else dd
            out.append({"simp": ch, "trad": t, "pinyin": pin, "gloss": gloss})
        return out

    def build_card(self, text: str) -> dict:
        entries = self.lookup(text)
        card = {"text": text, "found": bool(entries), "entries": [], "components": []}
        for e in entries[:3]:
            card["entries"].append({"simp": e.simp, "trad": e.trad, "pinyin": e.pinyin, "defs": display_defs(e)})
        if entries:
            first = entries[0]
            if len(first.simp) > 1:
                card["components"] = self.components(first.simp, first.trad, first.pinyin.split())
        elif len(text) > 1:
            card["components"] = self.components(text)
        return card


def save_definition(card: dict) -> str:
    """Single-line definition for the saved-words file."""
    entries = card["entries"]
    if not entries:
        return ""
    if len(entries) == 1:
        return "; ".join(entries[0]["defs"])
    parts = [f"[{pinyin_plain(e['pinyin'])}] " + "; ".join(e["defs"]) for e in entries]
    return " | ".join(parts)


def card_html(card: dict, translation: str | None, theme: dict) -> str:
    muted, body, colors = theme["muted"], theme["body"], theme["tones"]
    h = lambda t: f'<p style="margin:12px 0 3px 0;font-size:11px;color:{muted}"><b>{t}</b></p>'
    out = []
    if translation is not None:
        out.append(f'<p style="margin:0 0 3px 0;font-size:11px;color:{muted}"><b>Context Translation</b></p>')
        out.append(f'<p style="margin:0;font-size:15px">{html.escape(translation)}</p>')
    out.append(h("Dictionary") if translation is not None else
               f'<p style="margin:0 0 3px 0;font-size:11px;color:{muted}"><b>Dictionary</b></p>')
    if card["found"]:
        for i, e in enumerate(card["entries"]):
            head = html.escape(e["simp"])
            if e["trad"] != e["simp"]:
                head += f' <span style="color:{muted}">|</span> {html.escape(e["trad"])}'
            top = "2px" if i == 0 else "10px"
            out.append(f'<p style="margin:{top} 0 2px 0;font-size:24px">{head}</p>')
            out.append(f'<p style="margin:0 0 3px 0;font-size:16px">{pinyin_html(e["pinyin"], colors)}</p>')
            out.append(f'<p style="margin:0;font-size:14px;color:{body}">{html.escape("; ".join(e["defs"]))}</p>')
    else:
        out.append(f'<p style="margin:2px 0;font-size:24px">{html.escape(card["text"])}</p>')
        out.append(f'<p style="margin:0;font-size:13px;color:{muted}">No dictionary entry for this selection. '
                   f'Scroll the mouse wheel down to shorten it.</p>')
    if card["components"]:
        out.append(h("Components"))
        for c in card["components"]:
            trad = f' <span style="color:{muted};font-size:14px">({html.escape(c["trad"])})</span>' if c["trad"] != c["simp"] else ""
            out.append(f'<p style="margin:4px 0 0 0;font-size:18px">{html.escape(c["simp"])}{trad} '
                       f'<span style="font-size:15px">{pinyin_html(c["pinyin"], colors) if c["pinyin"] else ""}</span></p>')
            if c["gloss"]:
                out.append(f'<p style="margin:0;font-size:13px;color:{body}">{html.escape(c["gloss"])}</p>')
    return "".join(out)


# --------------------------------------------------------------------------
# saved words file:  WORD <TAB> DEFINITION
# --------------------------------------------------------------------------
def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


CARD_STYLE = (
    "<style>.w9{--box:#f8fafc;--text:#1a202c;--muted:#5b6678;--accent:#2b82d9;--head:#2a69ac;--line:#dfe5ee;"
    "--nb:#eff7ff;--nt:#0f4c75;color:var(--text);font-family:-apple-system,Roboto,'Noto Sans SC',sans-serif;"
    "text-align:center;font-size:10px}"
    ".night_mode .w9,.nightMode .w9,.w9.night_mode,.w9.nightMode{--box:#14171c;--text:#f1f3f6;--muted:#9aa5b5;"
    "--accent:#5ab0ff;--head:#6fb6f5;--line:#2a303a;--nb:#0e2236;--nt:#bfe0ff}"
    ".w9 .reading{background:var(--box);border:1px solid var(--line);border-radius:8px;padding:8px}"
    ".w9 .py{display:block;color:var(--accent);font-size:17px;font-weight:600}"
    ".w9 .en{display:block;color:var(--muted);font-size:13px;font-weight:600;margin-top:2px}"
    ".w9 .label{text-align:left;font-size:9px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;"
    "color:var(--head);border-bottom:1px solid var(--line);padding-bottom:4px;margin:14px 0 6px}"
    ".w9 .ex{background:var(--box);border:1px solid var(--line);border-radius:7px;padding:7px 8px;text-align:left}"
    ".w9 .zh{font-size:16px;line-height:1.4}"
    ".w9 .item{display:flex;align-items:center;gap:10px;text-align:left;background:var(--box);"
    "border:1px solid var(--line);border-radius:7px;padding:7px 8px;margin-bottom:5px}"
    ".w9 .item .w{flex:1;font-size:17px}.w9 .item .r{flex:1}"
    ".w9 .item .p{display:block;color:var(--accent);font-size:11px;font-weight:600}"
    ".w9 .item .m{display:block;color:var(--muted);font-size:10px;margin-top:1px}</style>"
)
ANKI_HEADER = "#separator:tab\n#html:true\n"


def format_card_field(card: dict, context: str = "") -> str:
    """Second column of a saved line: one single-line HTML block (for Anki, 'Allow HTML in fields'):

    <style>...</style><div class="w9"><div class="reading"><span class="py">pinyin</span>
    <span class="en">definition</span></div><div class="label">Example</div>
    <div class="ex"><div class="zh">original sentence</div></div></div>

    A word with several readings gets one "reading" div per reading. No line breaks are ever written,
    so every saved word stays on exactly one line of the file.
    """
    e = lambda s: html.escape(_one_line(s), quote=False)
    parts = [CARD_STYLE, '<div class="w9">']
    for en in card["entries"]:
        parts.append(f'<div class="reading"><span class="py">{e(pinyin_joined(en["pinyin"]))}</span>'
                     f'<span class="en">{e("; ".join(en["defs"]))}</span></div>')
    if _one_line(context):
        parts.append(f'<div class="label">Example</div><div class="ex"><div class="zh">{e(context)}</div></div>')
    parts.append("</div>")
    return "".join(parts)


def _read_rows(path):
    """Rows of the saved-words file (a field may be "quoted", with "" for a quote mark)."""
    with open(path, encoding="utf-8", newline="") as f:
        yield from csv.reader(f, delimiter="\t", quotechar='"')


def is_saved(path: str | Path, word: str) -> bool:
    try:
        return any(row and row[0] == word for row in _read_rows(path))
    except FileNotFoundError:
        return False
    except csv.Error:
        return False


def ensure_header(path: Path) -> None:
    """Make the file start with Anki's '#separator:tab' / '#html:true' lines (adds them if missing)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if text.startswith(ANKI_HEADER):
        return
    lines = text.splitlines(keepends=True)
    while lines and re.match(r"#(separator|html)\b", lines[0]):  # drop an old/partial header
        lines.pop(0)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(ANKI_HEADER + "".join(lines), encoding="utf-8")
    os.replace(tmp, path)


def save_word(path: str | Path, word: str, field: str) -> str:
    """Append  word<TAB>field  as one line (the field is quoted, with " written as "" - the
    format Anki imports). Returns 'saved' or 'exists'."""
    path = Path(path).expanduser()
    if is_saved(path, word):
        return "exists"
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_header(path)
    needs_nl = False
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_nl = f.read(1) != b"\n"
    with open(path, "a", encoding="utf-8", newline="") as f:
        if needs_nl:
            f.write("\n")
        csv.writer(f, delimiter="\t", quotechar='"', quoting=csv.QUOTE_MINIMAL,
                   lineterminator="\n").writerow([_one_line(word), _one_line(field)])
    return "saved"


# --------------------------------------------------------------------------
# word segmentation
# --------------------------------------------------------------------------
class Segmenter:
    """jieba for boundaries, CC-CEDICT to split anything jieba glues together
    that the dictionary does not know (e.g. 司机们 -> 司机 + 们)."""

    def __init__(self, dictionary: Dictionary):
        self.d = dictionary
        try:
            import warnings
            warnings.filterwarnings("ignore", category=SyntaxWarning)  # jieba has old regex strings
            import jieba
            jieba.setLogLevel(logging.ERROR)
            self.jieba = jieba
        except ImportError:
            self.jieba = None

    def warm(self):
        if self.jieba:
            list(self.jieba.cut("预热"))

    def _split_known(self, word: str, start: int) -> list[tuple[str, int, int]]:
        out, i = [], 0
        while i < len(word):
            for n in range(min(self.d.maxlen, len(word) - i), 0, -1):
                piece = word[i:i + n]
                if n == 1 or self.d.has(piece):
                    out.append((piece, start + i, start + i + n))
                    i += n
                    break
        return out

    def segment(self, text: str) -> list[tuple[str, int, int]]:
        raw: list[tuple[str, int, int]] = []
        if self.jieba:
            raw = list(self.jieba.tokenize(text))
        else:
            i = 0
            while i < len(text):
                if is_cjk(text[i]):
                    j = i
                    while j < len(text) and is_cjk(text[j]):
                        j += 1
                    raw.extend(self._split_known(text[i:j], i))
                    i = j
                else:
                    raw.append((text[i], i, i + 1))
                    i += 1
        out = []
        for w, s, e in raw:
            if len(w) > 1 and is_cjk(w[0]) and not self.d.has(w):
                out.extend(self._split_known(w, s))
            else:
                out.append((w, s, e))
        return out


# --------------------------------------------------------------------------
# OCR lines and sentence context
# --------------------------------------------------------------------------
SENT_END = "。！？!?；;…\n"
CLAUSE_END = "，,、：:"


@dataclass
class OcrLine:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    spans: list  # (x0, x1) per character, image pixels

    @property
    def h(self):
        return max(self.y1 - self.y0, 1.0)

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2


def _char_spans(txt, x0, x1, words):
    if words:
        try:
            if "".join(w[0] for w in words) == txt:
                spans = []
                for w in words:
                    xs = [p[0] for p in w[2]]
                    a, b = min(xs), max(xs)
                    n = len(w[0])
                    for k in range(n):
                        spans.append((a + (b - a) * k / n, a + (b - a) * (k + 1) / n))
                return spans
        except Exception:
            pass
    weights = [1.0 if (is_cjk(c) or ord(c) > 0x2000) else 0.55 for c in txt]
    total = sum(weights) or 1.0
    spans, acc = [], 0.0
    for w in weights:
        spans.append((x0 + (x1 - x0) * acc / total, x0 + (x1 - x0) * (acc + w) / total))
        acc += w
    return spans


def build_lines(boxes, txts, scores, word_results, min_conf=0.5, only_cjk=True) -> list[OcrLine]:
    lines = []
    if txts is None or boxes is None:
        return lines
    for i, txt in enumerate(txts):
        score = scores[i] if scores is not None else 1.0
        if score < min_conf or not txt or (only_cjk and not has_cjk(txt)):
            continue
        xs = [float(p[0]) for p in boxes[i]]
        ys = [float(p[1]) for p in boxes[i]]
        words = word_results[i] if word_results and i < len(word_results) else None
        lines.append(OcrLine(txt, min(xs), min(ys), max(xs), max(ys),
                             _char_spans(txt, min(xs), max(xs), words)))
    lines.sort(key=lambda l: (round(l.y0 / 10), l.x0))
    return lines


def _follows(a: OcrLine, b: OcrLine) -> bool:
    """Does line b continue the text of line a?"""
    ha = a.h
    same_row = abs(a.cy - b.cy) < 0.5 * ha and -0.5 * ha <= b.x0 - a.x1 <= 1.5 * ha
    next_row = (-0.2 * ha <= b.y0 - a.y1 <= 0.8 * ha
                and (abs(b.x0 - a.x0) <= 1.5 * ha or abs(b.cx - a.cx) <= 1.5 * ha)
                and len(a.text) >= 6 and a.text[-1] not in SENT_END)
    return same_row or next_row


def _chain(lines: list[OcrLine], li: int) -> list[int]:
    chain = [li]
    cur = li
    for _ in range(6):
        nxt = [j for j in range(len(lines)) if j not in chain and _follows(lines[cur], lines[j])]
        if not nxt:
            break
        cur = min(nxt, key=lambda j: (lines[j].y0 - lines[cur].y1) ** 2 + (lines[j].x0 - lines[cur].x0) ** 2)
        chain.append(cur)
    cur = li
    for _ in range(6):
        prv = [j for j in range(len(lines)) if j not in chain and _follows(lines[j], lines[cur])]
        if not prv:
            break
        cur = min(prv, key=lambda j: (lines[cur].y0 - lines[j].y1) ** 2 + (lines[cur].x0 - lines[j].x0) ** 2)
        chain.insert(0, cur)
    return chain


@dataclass
class Context:
    sentence: str
    chars: list      # (line_index, char_index) for every sentence character
    tokens: list     # (word, start, end) within the sentence
    tok: int         # index of the clicked token


def build_context(lines: list[OcrLine], li: int, ci: int, segmenter: Segmenter) -> Context | None:
    chain = _chain(lines, li)
    text, cmap, pos = "", [], 0
    for j in chain:
        if j == li:
            pos = len(text) + ci
        text += lines[j].text
        cmap += [(j, k) for k in range(len(lines[j].text))]
    pos = min(pos, len(text) - 1)

    def bounds(ends, lo, hi):
        s = pos
        while s > lo and text[s - 1] not in ends:
            s -= 1
        e = pos
        while e < hi and text[e] not in ends:
            e += 1
        if e < hi:
            e += 1
        return s, e

    s, e = bounds(SENT_END, 0, len(text))
    if e - s > 150:
        s, e = bounds(CLAUSE_END, s, e)
    sentence = text[s:e]
    tokens = segmenter.segment(sentence)
    rel = pos - s
    tok = next((i for i, (_, a, b) in enumerate(tokens) if a <= rel < b), None)
    if tok is None or not has_cjk(tokens[tok][0]):
        return None
    return Context(sentence, cmap[s:e], tokens, tok)


# --------------------------------------------------------------------------
# offline translation (Argos Translate zh->en model, run directly with
# CTranslate2 + SentencePiece so that no PyTorch/stanza is needed)
# --------------------------------------------------------------------------
def tidy_translation(decoded: str, pieces: list[str] | None = None) -> str:
    """Make sure word separators are real spaces (some models leave '▁' or '_' in the output)."""
    text = decoded
    if "▁" not in text and "_" not in text and (" " in text or not pieces):
        return text.strip()
    if "▁" not in text and pieces and any("▁" in p for p in pieces):
        text = "".join(pieces)  # sp.decode did not know these pieces: join them ourselves
    text = text.replace("▁", " ")
    if " " not in text.strip():  # still no spaces at all: underscores are the separators
        text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?;:%)])", r"\1", text)
    return text.strip()


class Translator:
    def __init__(self, model_dir: Path = MODEL_DIR):
        import ctranslate2
        import sentencepiece
        sp = next(model_dir.rglob("sentencepiece.model"), None)
        ct = next(model_dir.rglob("model.bin"), None)
        if not sp or not ct:
            raise FileNotFoundError(f"translation model not found in {model_dir} (run: screen-translator setup)")
        meta = {}
        mj = next(model_dir.rglob("metadata.json"), None)
        if mj:
            try:
                meta = json.loads(mj.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.prefix = meta.get("target_prefix") or ""
        self.sp = sentencepiece.SentencePieceProcessor(model_file=str(sp))
        self.tr = ctranslate2.Translator(str(ct.parent), device="cpu", intra_threads=max(2, (os.cpu_count() or 4) // 2))

    def translate(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        toks = self.sp.encode(text, out_type=str)
        kw = dict(beam_size=4, max_batch_size=1, replace_unknowns=True, length_penalty=0.2)
        if self.prefix:
            kw["target_prefix"] = [[self.prefix]]
        hyp = self.tr.translate_batch([toks], **kw)[0].hypotheses[0]
        if self.prefix and hyp and hyp[0] == self.prefix:
            hyp = hyp[1:]
        return tidy_translation(self.sp.decode(hyp), hyp)


# --------------------------------------------------------------------------
# setup helpers (run once: dictionary + translation model download)
# --------------------------------------------------------------------------
_UA = {"User-Agent": "Mozilla/5.0 (screen-translator setup)"}
CEDICT_URL = "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.zip"
ARGOS_INDEX = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"


def _download(url: str, dest: Path):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done >> 20} / {total >> 20} MB", end="", flush=True)
    print()


def install_cedict(dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".zip")
    try:
        print("Downloading CC-CEDICT ...")
        _download(CEDICT_URL, tmp)
        with zipfile.ZipFile(tmp) as z:
            name = next(n for n in z.namelist() if n.endswith(".u8"))
            dest.write_bytes(z.read(name))
        return True
    except Exception as ex:
        print(f"  mdbg.net download failed ({ex}); trying the copy bundled with the 'pycccedict' pip package ...")
    finally:
        tmp.unlink(missing_ok=True)
    try:
        import gzip
        import importlib.util
        spec = importlib.util.find_spec("pycccedict")
        if spec and spec.submodule_search_locations:
            gz = next(Path(spec.submodule_search_locations[0]).rglob("*.txt.gz"))
            dest.write_bytes(gzip.open(gz).read())
            return True
    except Exception as ex:
        print(f"  fallback failed: {ex}")
    return False


def install_translation_model() -> bool:
    try:
        print("Looking up the Argos Translate zh->en model ...")
        req = urllib.request.Request(ARGOS_INDEX, headers=_UA)
        idx = json.load(urllib.request.urlopen(req, timeout=60))
        pk = [p for p in idx if p.get("from_code") == "zh" and p.get("to_code") == "en" and p.get("links")]
        pk.sort(key=lambda p: [int(x) if x.isdigit() else 0 for x in str(p.get("package_version", "0")).split(".")])
        url = pk[-1]["links"][0]
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MODEL_DIR / "model.argosmodel"
        print(f"Downloading {url}")
        _download(url, tmp)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(MODEL_DIR)
        tmp.unlink(missing_ok=True)
        Translator(MODEL_DIR)  # smoke test: loads the model
        return True
    except Exception as ex:
        print(f"  translation model install failed: {ex}")
        return False
