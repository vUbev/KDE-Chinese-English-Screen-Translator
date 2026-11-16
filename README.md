# Screen Translator (Chinese → English) for KDE Plasma

Press **Alt+Q** → the screen freezes, every line of Chinese text is highlighted →
click a word → dictionary popup. Click anywhere else (or press Esc) to dismiss.

Everything runs locally: OCR (RapidOCR / PaddleOCR models), dictionary (CC-CEDICT),
and the context translation (Argos Translate zh→en model, run through CTranslate2).

## Install

```bash
chmod +x install.sh
./install.sh
```

Installs into `~/.local/share/screen-translator` (own Python venv, no layering, no root).
Needs `python3` and Spectacle (both ship with Bazzite KDE). The first run downloads
the dictionary (~4 MB) and the translation model (~100 MB); the pip packages are a few hundred MB.

`./install.sh uninstall` removes it again. Your saved words (`~/saved_words.txt`) are not touched.

## Using it

| Action | Result |
|---|---|
| **Alt+Q** | Scan the current monitor. Press again to dismiss. |
| Click a highlighted word | Popup: context translation, dictionary entry (simplified \| traditional, coloured pinyin), per-character components |
| Mouse wheel up/down, or **→ / ←** | Make the selected word one character longer/shorter (e.g. 环形 → 环形路 → 环形路口) |
| **Ctrl/Shift + wheel**, or **Shift + ← / →** | Same, for the left edge |
| **★ Save word** | Appends `WORD<TAB>DEFINITION` to `~/saved_words.txt` in your home folder (skips duplicates) |
| Click elsewhere / Esc | Dismiss |


Saved words are stored in `~/saved_words.txt`. They come pre-formatted for anki so you can just import directly. The "Example" is the original context you saved the word from.

<img width="3782" height="1959" alt="image" src="https://github.com/user-attachments/assets/4f5a34c7-4b61-4454-840b-1af88d808672" />

<img width="775" height="580" alt="image" src="https://github.com/user-attachments/assets/62b137c1-1bcc-4b51-afe0-7e96c194323c" />

## Config

`~/.config/screen-translator/config.json` is created on first run:

```json
{
  "saved_words_file": "/home/you/saved_words.txt",
  "translator": "argos",        // "none" = dictionary only
  "min_confidence": 0.5,
  "only_chinese": true,
  "theme": "auto",              // "auto" follows KDE, or force "dark" / "light"
  "tone_colors_dark":  { "1": "#ff8080", "2": "#80ff80", "3": "#8080ff", "4": "#df80ff", "5": "#c7c7c7" },
  "tone_colors_light": { "1": "#e30000", "2": "#01b31c", "3": "#150ff0", "4": "#8800bf", "5": "#777777" }
}
```

Restart the background service after editing: `pkill -f screen_translator.py; screen-translator trigger`.

## Troubleshooting

* **Alt+Q does nothing** – add it manually: System Settings → Keyboard → Shortcuts → Add New →
  Command or Script → `~/.local/bin/screen-translator trigger`.
* **Nothing happens at all / no overlay** – check `~/.local/share/screen-translator/screen-translator.log`.
  Try `spectacle --background --nonotify --current --output /tmp/t.png` in a terminal; if that fails,
  capture is the problem.
* **Overlay appears on the wrong monitor** (multi-monitor with identical resolutions) – the screenshot is
  taken of KDE's "current" monitor; move the mouse to the monitor you want before pressing the shortcut.
* **pip fails to install a package** – Bazzite's newest Python may lack wheels for some packages; use an older one:
  `PYTHON=python3.12 ./install.sh` (e.g. via `uv python install 3.12`).
* **No "Context Translation"** – run `screen-translator setup` again while online.
* Test the dictionary without the GUI: `screen-translator lookup 环形路 司机`.

## Credits / licence

Dictionary data: [CC-CEDICT](https://cc-cedict.org) (CC BY-SA 4.0). OCR: [RapidOCR](https://github.com/RapidAI/RapidOCR).
Segmentation: [jieba](https://github.com/fxsjy/jieba). Translation model: [Argos Translate](https://github.com/argosopentech/argos-translate).
