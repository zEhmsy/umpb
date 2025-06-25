# virtual_steamdeck.py — v0.8.0 umpb
from __future__ import annotations
from pynput import keyboard as _kb
from pathlib import Path
import json

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from PySide6.QtCore import QPoint, QSize, Qt, Signal, QFileInfo
from PySide6.QtGui import QFont, QIcon, QPixmap, QPainter, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QMessageBox,
    QKeySequenceEdit, QDialog, QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QStyle,
    QFileIconProvider
)

# ---------------------------------------------------------------------------
# Helpers & Data model
# ---------------------------------------------------------------------------

def _icon(glyph: str) -> QIcon:
    """Crea un'icona 64×64 disegnando il glyph Unicode centrato."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.white)

    font = QFont()                 # carattere di default del sistema
    font.setPixelSize(46)          # grandezza visibile
    painter.setFont(font)

    painter.drawText(pm.rect(), Qt.AlignCenter, glyph)
    painter.end()

    return QIcon(pm)

@dataclass
class Shortcut:
    id: str
    name: str
    key: str
    action: str
    color: str  # e.g. "bg-blue-500" (mapped later)
    type: str   # "shortcut" | "app"
    path: Optional[str] = None

    def qt_icon(self) -> QIcon:
        """Icona visualizzata sul tile."""
        if self.type == "app" and self.path:
            # usa l’icona di sistema del file / bundle
            provider = QFileIconProvider()
            return provider.icon(QFileInfo(self.path))
        glyphs = {
            "undo": "↶", "redo": "↷", "copy": "⎘", "cut": "✂",
            "save": "💾", "find": "🔍", "new": "📄", "explorer": "📁",
        }
        return _icon(glyphs.get(self.action, "🔘"))

# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class VirtualSteamDeck(QMainWindow):
    toggle_requested = Signal()
    tile_requested   = Signal(int)   # indice 0-7 da hot-key

    # ---- layout constants
    GRID_ROWS = 2
    GRID_COLS = 4
    TILE = 62           # tile size (px)
    GRID_SPACING = 12    # space between tiles (px)
    BODY_MARGIN = 10     # all body margins (px)

    # ---- misc
    HOTKEY = "ctrl+shift+d"
    MAX_PAGES = 5
    LAYOUT_PATH = Path.home() / ".umpb_layout.json"

    def __init__(self):
        super().__init__()
        # Compute window size to guarantee NO overlap
        # ---------- persistenza layout ---------- #

        width = (
            self.GRID_COLS * self.TILE
            + (self.GRID_COLS - 1) * self.GRID_SPACING
            + self.BODY_MARGIN * 2
        )
        height = 260  # empirically comfortable with header+nav+sys+info

        self.setWindowTitle("UMPB")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(width + 40, height)  # +40 to account for window border shadows

        # ----- state
        self.is_edit_mode = False
        self.is_dragging = False
        self.drag_offset = QPoint()
        self.current_page = 0
        self.pages: List[List[Shortcut]] = [self._default_shortcuts()]
        self._load_layout()

        # build & hotkey
        self._build_ui()
        self.toggle_requested.connect(self.toggle_visibility)
        self.tile_requested.connect(self._trigger_tile)
        self._install_hotkey()

    def _load_layout(self):
            if self.LAYOUT_PATH.exists():
                try:
                    with self.LAYOUT_PATH.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.pages = [[Shortcut(**sc) for sc in page] for page in data]
                except Exception as e:
                    print("⚠️  Failed to load layout:", e)

    def _save_layout(self):
        try:
            data = [[sc.__dict__ for sc in page] for page in self.pages]
            with self.LAYOUT_PATH.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print("⚠️  Failed to save layout:", e)

    # -------------------------------------------------------------------
    # UI construction
    # -------------------------------------------------------------------
    def _build_ui(self):
        root = QFrame(objectName="root")
        root.setStyleSheet(
            "QFrame#root{background:rgba(30,41,59,0.85);border:2px solid rgba(71,85,105,0.5);border-radius:16px;}"
        )
        self.setCentralWidget(root)
        main_v = QVBoxLayout(root)
        main_v.setContentsMargins(0, 0, 0, 0)
        main_v.setSpacing(0)

        # -------- header
        header = self._make_header()
        main_v.addWidget(header)

        # -------- body
        self.body = QFrame()
        body_v = QVBoxLayout(self.body)
        body_v.setContentsMargins(self.BODY_MARGIN, self.BODY_MARGIN, self.BODY_MARGIN, self.BODY_MARGIN)
        body_v.setSpacing(4)

        # grid
        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setHorizontalSpacing(self.GRID_SPACING)
        self.grid_layout.setVerticalSpacing(self.GRID_SPACING)
        body_v.addWidget(self.grid_widget)

        # nav dots
        self.nav_widget = QWidget()
        self.nav_layout = QHBoxLayout(self.nav_widget)
        self.nav_layout.setContentsMargins(2, 2, 2, 2)   # niente margini
        self.nav_layout.setAlignment(Qt.AlignCenter)
        self.nav_layout.setSpacing(6)
        body_v.addWidget(self.nav_widget)

        # info label
        self.info_label = QLabel(alignment=Qt.AlignCenter)
        f = QFont(); f.setPointSize(9); self.info_label.setFont(f)
        self.info_label.setStyleSheet("color:#64748b;")
        body_v.addWidget(self.info_label)

        main_v.addWidget(self.body)

        # first render
        self._refresh_ui()

    # ---------------------- header helpers -----------------------------
    def _make_header(self) -> QWidget:
        bar = QFrame()
        bar.setStyleSheet("background:rgba(51,65,85,0.9); border-radius:12px;")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 8, 8, 8)

        self.lbl_title = QLabel("UMPB")
        self.lbl_title.setStyleSheet("color:#e2e8f0;font-weight:600;font-size:13px;")
        self.lbl_page = QLabel()
        self.lbl_page.setStyleSheet("color:#94a3b8;font-size:11px;")
        h.addWidget(self.lbl_title)
        h.addWidget(self.lbl_page)
        h.addStretch()
        h.addWidget(self._header_button(QStyle.SP_TitleBarMinButton,      self._toggle_minimise))
        h.addWidget(self._header_button(QStyle.SP_DialogOkButton,         self._handle_home))
        self.btn_settings = self._header_button(QStyle.SP_FileDialogNewFolder, self._handle_settings)
        h.addWidget(self.btn_settings)
        h.addWidget(self._header_button(QStyle.SP_TitleBarCloseButton,    self._handle_power))

        # drag events on header
        bar.mousePressEvent = self._drag_start
        bar.mouseMoveEvent = self._drag_move
        bar.mouseReleaseEvent = self._drag_stop
        return bar

    @staticmethod
    def _header_button(std_pix: QStyle.StandardPixmap, slot: Callable) -> QPushButton:
        b = QPushButton()
        b.setFixedSize(18, 18)
        b.setCursor(Qt.PointingHandCursor)
        b.setIcon(b.style().standardIcon(std_pix))
        b.setIconSize(QSize(16, 16))
        b.setStyleSheet(
            "QPushButton{background:transparent;border:none;color:#94a3b8;}"
            "QPushButton:hover{color:#f87171;}"
        )
        b.clicked.connect(slot)
        return b

    # -------------------------------------------------------------------
    # Dragging window
    # -------------------------------------------------------------------
    def _drag_start(self, e):
        self.is_dragging = True
        self.drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        self.setCursor(Qt.SizeAllCursor)

    def _drag_move(self, e):
        if self.is_dragging:
            self.move(e.globalPosition().toPoint() - self.drag_offset)

    def _drag_stop(self, _):
        self.is_dragging = False
        self.setCursor(Qt.ArrowCursor)

    # -------------------------------------------------------------------
    # Rendering helpers
    # -------------------------------------------------------------------
    def _refresh_ui(self):
        self.lbl_title.setText("Edit Mode" if self.is_edit_mode else "UMPB")
        self.lbl_page.setText(
            f"  Page {self.current_page + 1}/{len(self.pages)}" if len(self.pages) > 1 else ""
        )
        # icona toggle Edit/Normal
        edit_icon = QStyle.SP_DialogApplyButton if self.is_edit_mode else QStyle.SP_FileDialogNewFolder
        self.btn_settings.setIcon(self.btn_settings.style().standardIcon(edit_icon))

        self.info_label.setText(
            "Tap ➕ to add • ✕ to delete • New pages with ➕ circle" if self.is_edit_mode else "Press Ctrl+Shift+D to toggle • Settings = Edit mode"
        )

        # --- grid
        while self.grid_layout.count():
            w = self.grid_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

        cur = self.pages[self.current_page]
        idx = 0
        for r in range(self.GRID_ROWS):
            for c in range(self.GRID_COLS):
                if idx < len(cur):
                    tile = self._make_shortcut_button(cur[idx])
                elif self.is_edit_mode and idx == len(cur):
                    tile = self._make_add_shortcut_button()
                else:
                    placeholder = QLabel()
                    placeholder.setFixedSize(self.TILE, self.TILE)
                    tile = placeholder
                self.grid_layout.addWidget(tile, r, c)
                idx += 1

        # --- nav dots
        while self.nav_layout.count():
            w = self.nav_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

        for i in range(len(self.pages)):
            dot = QPushButton()
            dot.setFixedSize(8, 8)
            dot.setCursor(Qt.PointingHandCursor)
            dot.setStyleSheet(
                f"border-radius:4px;background-color:{'#3b82f6' if i == self.current_page else '#475569'};"
            )
            dot.clicked.connect(lambda _, x=i: self._goto_page(x))
            self.nav_layout.addWidget(dot)

        if self.is_edit_mode and len(self.pages) < self.MAX_PAGES:
            add = QPushButton("+")
            add.setFixedSize(18, 18)
            add.setCursor(Qt.PointingHandCursor)
            add.setStyleSheet(
                "border-radius:4px;background:rgba(51,65,85,0.5);color:#94a3b8;border:1px solid rgba(71,85,105,0.6);"
            )
            add.clicked.connect(self._add_page)
            self.nav_layout.addWidget(add)

    # ---------------- tile factories -----------------------------------
    def _make_shortcut_button(self, sc: Shortcut) -> QWidget:
        btn = QPushButton()
        btn.setFixedSize(self.TILE, self.TILE)
        btn.setCursor(Qt.PointingHandCursor)

        # --- icona + testo ------------------------------------------------
        btn.setIcon(sc.qt_icon())

        if sc.type == "app":
            # solo icona grande
            btn.setText("")
            btn.setIconSize(QSize(self.TILE - 12, self.TILE - 12))

        elif sc.type == "shortcut":
            known = {"undo", "redo", "copy", "cut", "save", "find", "new", "explorer"}
            if sc.action in known:
                # scorciatoie note → solo glyph, un po’ più grande
                btn.setText("")
                btn.setIconSize(QSize(40, 40))
            else:
                # scorciatoie custom → iconcina e testo (ctrl+…)
                btn.setIconSize(QSize(28, 28))
                btn.setText(sc.key.lower())

        else:
            btn.setIconSize(QSize(32, 32))
            btn.setText(sc.name)

        # --- stile --------------------------------------------------------
        btn.setToolTip(sc.name)
        btn.setStyleSheet(
            "QPushButton{background:rgba(51,65,85,0.7);color:#cbd5e1;border:none;border-radius:8px;"
            "font-size:10px;text-align:center;}"
            "QPushButton:hover{background:rgba(71,85,105,0.9);}"
        )
        btn.clicked.connect(lambda: self._handle_shortcut(sc))

        # pulsante delete in edit-mode
        if self.is_edit_mode:
            del_btn = QPushButton("✕", btn)
            del_btn.setFixedSize(16, 16)
            del_btn.move(self.TILE - 20, 4)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet(
                "border-radius:8px;background:#ef4444;color:white;font-size:10px;"
            )
            del_btn.clicked.connect(lambda _, sid=sc.id: self._delete_shortcut(sid))

        return btn


    def _make_add_shortcut_button(self) -> QPushButton:
        btn = QPushButton("➕\nAdd")
        btn.setFixedSize(self.TILE, self.TILE)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton{background:rgba(51,65,85,0.5);color:#94a3b8;border:2px dashed rgba(71,85,105,0.6);border-radius:8px;font-size:11px;}"
            "QPushButton:hover{border-color:rgba(203,213,225,0.8);}"
        )
        btn.clicked.connect(self._add_shortcut)
        return btn


    # -------------------------------------------------------------------
    # Shortcut actions
    # -------------------------------------------------------------------
    def _handle_shortcut(self, sc: Shortcut):
        """Esegue la scorciatoia o avvia l’app collegata."""
        if self.is_edit_mode:
            return  # in edit-mode i click non fanno nulla

        print(f"Shortcut triggered: {sc.name} ({sc.key})")

        if sc.type == "shortcut":
            self._send_keystroke(sc.key)
        elif sc.type == "app" and sc.path:
            self._launch_app(sc.path)

    def _trigger_tile(self, idx: int):
        """Esegue il tile idx via hot-key."""
        if self.is_edit_mode:
            return
        cur = self.pages[self.current_page]
        if 0 <= idx < len(cur):
            self._handle_shortcut(cur[idx])

    # -------------------------------------------------------------------
    # Cross-platform keystroke sender (pynput)
    # -------------------------------------------------------------------
    def _send_keystroke(self, combo: str):
        """Invia la combinazione di tasti (es. 'Ctrl+Alt+K')."""
        try:
            from pynput.keyboard import Key, Controller

            mapper = {
                "ctrl": Key.ctrl,
                "shift": Key.shift,
                "alt": Key.alt,
                "cmd": Key.cmd,
                "meta": Key.cmd,
            }
            ctl = Controller()
            parts = [p.strip().lower() for p in combo.split("+")]
            # correzione: su macOS un eventuale 'ctrl' → 'cmd' (salvo lo voglia davvero)
            if sys.platform == "darwin":
                parts = ["cmd" if p == "ctrl" else p for p in parts]
            mods = [mapper[p] for p in parts if p in mapper]
            chars = [p for p in parts if p not in mapper]

            for m in mods:
                ctl.press(m)
            for ch in chars:
                if len(ch) == 1:
                    ctl.press(ch)
                    ctl.release(ch)
            for m in reversed(mods):
                ctl.release(m)
        except Exception as e:
            print("Couldn't send keystroke:", e)

    @staticmethod
    def _launch_app(path: str):
        """Avvia l’app collegata, cross-platform."""
        import os, subprocess, sys

        if sys.platform.startswith("darwin"):
            subprocess.Popen(["open", path])
        elif sys.platform.startswith("win"):
            os.startfile(path)          # type: ignore
        else:                           # Linux / BSD
            subprocess.Popen(["xdg-open", path])

    # -------------------------------------------------------------------
    # edit helpers
    # -------------------------------------------------------------------
    def _delete_shortcut(self, sid: str):
        """Rimuove una scorciatoia dalla pagina corrente e aggiorna la UI."""
        self.pages[self.current_page] = [
            s for s in self.pages[self.current_page] if s.id != sid
        ]
        self._refresh_ui()
        self._save_layout()

    def _add_shortcut(self):
        self._prompt_new_shortcut()

    class KeySequenceDialog(QDialog):
        """Piccolo dialogo che registra la prossima scorciatoia premuta."""
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Premi la combinazione")
            lay = QVBoxLayout(self)

            self.kse = QKeySequenceEdit(self)
            lay.addWidget(self.kse)

            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            lay.addWidget(bb)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)

        def key_sequence(self) -> Optional[str]:
            if self.exec() == QDialog.Accepted:
                seq = self.kse.keySequence().toString(QKeySequence.NativeText)
                return seq if seq else None
            return None


    def _prompt_new_shortcut(self):
        """Dialogo per creare un nuovo shortcut (app o hot-key)."""
        from pathlib import Path
        import time

        if len(self.pages[self.current_page]) >= self.GRID_ROWS * self.GRID_COLS:
            return

        ask = QMessageBox(self)
        ask.setIcon(QMessageBox.NoIcon)
        ask.setWindowTitle("New Shortcut")
        ask.setText("Che tipo di shortcut vuoi creare?")
        btn_app  = ask.addButton("App",  QMessageBox.AcceptRole)
        btn_keys = ask.addButton("Combinazione tasti", QMessageBox.AcceptRole)
        ask.addButton(QMessageBox.Cancel)
        ask.exec()

        if ask.clickedButton() is btn_app:
            path, _ = QFileDialog.getOpenFileName(self, "Scegli l'app")
            if not path:
                return
            name = Path(path).stem
            sc = Shortcut(
                id=str(int(time.time()*1000)),
                name=name,
                key="",
                action=name.lower(),
                color="bg-purple-500",
                type="app",
                path=path,
            )
            self.pages[self.current_page].append(sc)
            self._refresh_ui()
            return

        elif ask.clickedButton() is btn_keys:
            dlg = self.KeySequenceDialog(self)
            combo = dlg.key_sequence()
            if not combo:
                return
            name, _ = QInputDialog.getText(self, "Nome", "Nome del comando:", text=combo)
            sc = Shortcut(
                id=str(int(time.time()*1000)),
                name=name if name else combo,
                key=combo,
                action=name.lower(),
                color="bg-blue-500",
                type="shortcut",
            )
            self.pages[self.current_page].append(sc)
            self._refresh_ui()
            self._save_layout()
            return

        else:
            return  # Cancel

    def _add_page(self):
        """Crea una nuova pagina (max 10) e ci naviga subito."""
        if len(self.pages) >= self.MAX_PAGES:
            return
        self.pages.append([])
        self.current_page = len(self.pages) - 1
        self._refresh_ui()
        self._save_layout()

    def _goto_page(self, idx: int):
        """Salta alla pagina indicizzata `idx`."""
        self.current_page = idx
        self._refresh_ui()

    # -------------------------------------------------------------------
    # System-control handlers
    # -------------------------------------------------------------------
    def _handle_home(self):
        self.current_page = 0
        self.is_edit_mode = False
        self._refresh_ui()

    def _handle_settings(self):
        self.is_edit_mode = not self.is_edit_mode
        self._refresh_ui()

    def _handle_power(self):
        self._save_layout()
        QApplication.quit()

    # -------------------------------------------------------------------
    # Window/minimise & visibility
    # -------------------------------------------------------------------
    def _toggle_minimise(self):
        self.is_minimized = not getattr(self, "is_minimized", False)
        self.body.setVisible(not self.is_minimized)

    def toggle_visibility(self, state: Optional[bool] = None):
        """Mostra/nasconde l’overlay. Se `state` è None effettua toggle."""
        if state is None:
            state = not self.isVisible()
        self.setVisible(state)
        # print("Steam Deck overlay toggled:", state)

    # -------------------------------------------------------------------
    # Global hot-key (Ctrl+Shift+D)
    # -------------------------------------------------------------------
    def _install_hotkey(self):
        """Hot-keys globali:
            ⌘/Ctrl + Shift + D   → mostra/nasconde
            ⌘/Ctrl + Alt + 1-8   → attiva il tile 1-8
        """
        from pynput import keyboard

        is_mac = sys.platform == "darwin"
        MOD_CMD = keyboard.Key.cmd   if is_mac else keyboard.Key.ctrl
        MOD_ALT = keyboard.Key.alt
        MOD_SFT = keyboard.Key.shift

        combo_toggle = {MOD_CMD, MOD_SFT, keyboard.KeyCode.from_char("d")}
        # --- combo_tiles: ogni numero ha due varianti (digit e char con Shift) -------
        symbols = ["!", "@", "#", "$", "%", "^", "&", "*"]  # US / Intl quasi identico
        combo_tiles = []
        for n in range(1, 9):
            digit   = keyboard.KeyCode.from_char(str(n))
            symbol  = keyboard.KeyCode.from_char(symbols[n-1])
            combo_tiles.append(({MOD_ALT, digit},  n-1))
            combo_tiles.append(({MOD_ALT, symbol}, n-1))  # variante shiftata

        pressed: set = set()

        def on_press(key):
            pressed.add(key)
            #print("PRESS:", key, "| set =", pressed)

            if combo_toggle.issubset(pressed):
                #print("→ toggle combo")
                self.toggle_requested.emit()
                pressed.clear()

            for combo, idx in combo_tiles:
                if combo.issubset(pressed):
                    #print(f"→ tile {idx+1} combo")
                    self.tile_requested.emit(idx)
                    pressed.clear()

        def on_release(key):
            pressed.discard(key)

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()

    # -------------------------------------------------------------------
    # Default shortcuts
    # -------------------------------------------------------------------
    def _default_shortcuts(self) -> List[Shortcut]:
        IS_MAC = sys.platform == "darwin"
        return [
            Shortcut(
                "1", "Photoshop", "/", "editing", "bg-purple-500", "app", path="/Applications/Adobe Photoshop 2025/Adobe Photoshop 2025.app"if sys.platform == "darwin" else "C:\\\\Windows\\\\Adobe Photoshop 2025.exe",),
            Shortcut(
                "2", "ArduinoIDE", "/", "programming", "bg-purple-500", "app", path="/Applications/Arduino IDE.app"if sys.platform == "darwin" else "C:\\\\Windows\\\\Arduino.exe",),
            Shortcut("3", "Discord", "/", "chat", "bg-purple-500", "app", path="/Applications/Discord.app"if sys.platform == "darwin" else "C:\\\\Windows\\\\Discord.exe",),
            Shortcut("4", "Zenmap", "/", "hacking", "bg-purple-500", "app", path="/Applications/Zenmap.app"if sys.platform == "darwin" else "C:\\\\Windows\\\\Zenmap.exe",),
            Shortcut("5", "FortiClient", "/", "vpn", "bg-purple-500", "app", path="/Applications/FortiClient.app"if sys.platform == "darwin" else "C:\\\\Windows\\\\FortiClient.exe"),
            Shortcut("6", "AnyDesk", "/", "remote", "bg-purple-500", "app", path="/Applications/AnyDesk.app"if sys.platform == "darwin" else "C:\\\\Windows\\\\AnyDesk.exe"),
            Shortcut("7", "Save",   "cmd+s" if IS_MAC else "ctrl+s",  "save", "bg-green-500", "shortcut"),
            Shortcut("8", "Find",   "cmd+f" if IS_MAC else "ctrl+f",  "find", "bg-green-500", "shortcut"),
        ]


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("umpb")

    # ► icona dell’intera app (finestre + dialoghi QMessageBox)
    app.setWindowIcon(QIcon("icon1024.png"))

    deck = VirtualSteamDeck()
    deck.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
