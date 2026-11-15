#!/usr/bin/env bash
# Screen Translator installer - Bazzite / Fedora Atomic / any KDE Plasma distro.
# Everything goes into your home directory (no rpm-ostree layering, no root).
#
#   ./install.sh              install or update
#   ./install.sh uninstall    remove (keeps ~/.config/screen-translator and your saved words)
#   HOTKEY="Meta+Q" ./install.sh      use another shortcut      (default: Alt+Q)
#   PYTHON=python3.12 ./install.sh    force a specific Python (default: auto-detect, fetches 3.12 via uv if needed)
set -euo pipefail

APP=screen-translator
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
HOTKEY="${HOTKEY:-Alt+Q}"
SCAN_DESKTOP="$APP-scan.desktop"

kwrite="$(command -v kwriteconfig6 || command -v kwriteconfig5 || true)"

reload_shortcuts() {
  systemctl --user restart plasma-kglobalaccel.service 2>/dev/null && return 0
  if command -v kquitapp6 >/dev/null; then
    kquitapp6 kglobalaccel 2>/dev/null || true; sleep 1
    (setsid nohup kglobalaccel6 >/dev/null 2>&1 &) || true
  elif command -v kquitapp5 >/dev/null; then
    kquitapp5 kglobalaccel5 2>/dev/null || true; sleep 1
    (setsid nohup kglobalaccel5 >/dev/null 2>&1 &) || true
  fi
}

if [[ "${1:-}" == "uninstall" ]]; then
  pkill -f "$DATA/app/screen_translator.py" 2>/dev/null || true
  [[ -n "$kwrite" ]] && "$kwrite" --file kglobalshortcutsrc --group services --group "$SCAN_DESKTOP" --key _launch --delete || true
  kept=""
  if [[ -f "$DATA/saved_words.txt" ]]; then   # never delete the user's saved words
    kept="$HOME/screen-translator-saved_words.txt"
    mv "$DATA/saved_words.txt" "$kept"
  fi
  rm -rf "$DATA" "$BIN/$APP" "$APPS/$SCAN_DESKTOP" "$AUTOSTART/$APP.desktop"
  reload_shortcuts
  echo "Removed. Your config (~/.config/$APP) was left in place."
  if [[ -n "$kept" ]]; then echo "Your saved words were moved to: $kept"; fi
  exit 0
fi

echo "==> Checking requirements"
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
command -v spectacle >/dev/null || echo "WARNING: Spectacle not found. It is used for screen capture (it ships with KDE; on Bazzite KDE it is preinstalled)."

# The OCR/ML wheels (onnxruntime, ctranslate2, ...) only exist for Python 3.10-3.13.
pick_python() {
  local cand
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$cand" >/dev/null || continue
    if "$cand" -c 'import sys, venv; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
      command -v "$cand"; return 0
    fi
  done
  return 1
}

echo "==> Creating Python environment in $DATA/venv"
mkdir -p "$DATA/app" "$BIN" "$APPS" "$AUTOSTART"
rm -rf "$DATA/venv" "$DATA/uvboot"
py_ok() { "$1" -c 'import sys, venv; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; }
if [[ -n "${PYTHON:-}" ]] && command -v "$PYTHON" >/dev/null && py_ok "$PYTHON"; then
  echo "Using PYTHON=$PYTHON ($("$PYTHON" -V))"
  "$PYTHON" -m venv "$DATA/venv"
elif py="$(pick_python)"; then
  [[ -n "${PYTHON:-}" ]] && echo "Ignoring PYTHON=$PYTHON (not a Python 3.10-3.13)"
  echo "Using $py ($("$py" -V))"
  "$py" -m venv "$DATA/venv"
else
  echo "System Python is $(python3 -V) - too new for the OCR packages. Fetching Python 3.12 with uv ..."
  UV="$(command -v uv || true)"
  if [[ -z "$UV" ]]; then
    python3 -m venv "$DATA/uvboot"
    "$DATA/uvboot/bin/pip" install --quiet uv
    UV="$DATA/uvboot/bin/uv"
  fi
  "$UV" venv --python 3.12 --seed "$DATA/venv"
fi
"$DATA/venv/bin/python" -V
"$DATA/venv/bin/pip" install --quiet --upgrade pip
echo "==> Installing packages (PySide6, RapidOCR, jieba, CTranslate2 ... a few hundred MB, please wait)"
"$DATA/venv/bin/pip" install --quiet PySide6-Essentials rapidocr onnxruntime jieba ctranslate2 sentencepiece numpy pycccedict

echo "==> Installing program files"
install -m 644 "$SRC/screen_translator.py" "$SRC/st_core.py" "$DATA/app/"
cat > "$BIN/$APP" <<EOF
#!/usr/bin/env bash
exec "$DATA/venv/bin/python" "$DATA/app/screen_translator.py" "\$@"
EOF
chmod +x "$BIN/$APP"

cat > "$APPS/$SCAN_DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Screen Translator (scan screen)
Comment=Scan the screen for Chinese text
Exec=$BIN/$APP trigger
Icon=accessories-dictionary
Terminal=false
Categories=Utility;
X-KDE-GlobalAccel-CommandShortcut=true
EOF

cat > "$AUTOSTART/$APP.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Screen Translator (background service)
Exec=$BIN/$APP daemon
Icon=accessories-dictionary
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

echo "==> Downloading dictionary (CC-CEDICT) and offline translation model"
"$BIN/$APP" setup || echo "Setup had problems - you can re-run '$APP setup' later."

echo "==> Registering the $HOTKEY shortcut"
if [[ -n "$kwrite" ]]; then
  "$kwrite" --file kglobalshortcutsrc --group services --group "$SCAN_DESKTOP" --key _launch "$HOTKEY,none,Screen Translator (scan screen)"
  reload_shortcuts
else
  echo "kwriteconfig not found - set the shortcut manually (see below)."
fi

echo "==> Starting the background service"
pkill -f "$DATA/app/screen_translator.py" 2>/dev/null || true
(setsid nohup "$DATA/venv/bin/python" "$DATA/app/screen_translator.py" daemon >/dev/null 2>&1 &)

cat <<EOF

Done.  Press $HOTKEY to scan the screen.

If $HOTKEY does nothing, add the shortcut by hand (30 seconds):
  System Settings -> Keyboard -> Shortcuts -> "Add New" -> "Command or Script..."
  Command:  $BIN/$APP trigger        Shortcut: $HOTKEY
(If KDE says the key is already taken, pick another one - e.g. HOTKEY=Meta+Q ./install.sh)

Config:       ~/.config/$APP/config.json     (saved-words file path, tone colours, ...)
Saved words:  $DATA/saved_words.txt   (WORD<TAB>DEFINITION)
Log:          $DATA/screen-translator.log
EOF
