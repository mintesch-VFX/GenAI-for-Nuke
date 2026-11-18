"""
setup_panel.py — GenAI-Setup (API-Keys + Standard-Ausgabeordner) und der große
Prompt-Editor. Beides sind PySide-Dialoge; laufen nur in Nuke (Qt vorhanden).

Das Setup oeffnet sich von selbst (erste Node ohne Login/Key, fehlender Login beim Generate).
Manuell: import GenAI.setup_panel as S; S.show_setup()   (kein Menüeintrag)
Prompt "Expand"-Button    ->  edit_prompt(node)

Keys/Einstellungen werden über config in ~/.nuke/GenAI_keys.json gespeichert.
"""

import os
import threading
import webbrowser

try:
    from PySide6 import QtWidgets, QtCore
except ImportError:  # ältere Nuke-Versionen
    from PySide2 import QtWidgets, QtCore

from . import config, mcp_auth


# (Key-Name, Anzeige-Label, Hinweis + Bezugsquelle)
_KEY_FIELDS = [
    ("MAGNIFIC_API_KEY", "Magnific API Key", "Magnific im API-Modus (statt Login)  —  magnific.com → API"),
    ("FAL_KEY", "fal Key", "API-Modus: nötig, wenn Bilder/Video als Eingang mitgehen (fal.ai hostet den Upload), und für Seedance 2, Gemini Omni, Kling 4.0 (API)  —  fal.ai"),
    ("OPENAI_API_KEY", "OpenAI API Key", "GPT 2 im API-Modus  —  platform.openai.com"),
    ("GOOGLE_API_KEY", "Google AI Studio Key", "optional (direkt)  —  aistudio.google.com"),
    ("LTX_API_KEY", "LTX API Key", "Alpha Gen (Matte), Upscale  —  console.ltx.io (Developer Console, getrennt vom LTX-Studio-Account; Credits vorab kaufen)"),
]


def _nuke_main_window():
    for w in QtWidgets.QApplication.topLevelWidgets():
        if w.inherits("QMainWindow"):
            return w
    return None


class _LoginBridge(QtCore.QObject):
    """Signale vom Login-Hintergrund-Thread zurueck in den Qt-Hauptthread."""
    info = QtCore.Signal(str)
    finished = QtCore.Signal(bool, str)


class SetupDialog(QtWidgets.QDialog):
    """Keys eintragen + Standard-Ausgabeordner festlegen."""

    def __init__(self, parent=None):
        super(SetupDialog, self).__init__(parent)
        self.setWindowTitle("GenAI — Einrichtung")
        self.setMinimumWidth(580)
        self._edits = {}

        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("GenAI einrichten")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(title)

        # ---- Schritt 1: Magnific-Login (reicht zum Starten) ---------------------------
        step1 = QtWidgets.QLabel("<b>Mit Magnific anmelden</b>  —  mehr brauchst du zum Start nicht.")
        layout.addWidget(step1)
        mcp_note = QtWidgets.QLabel(
            "Einmal im Browser bestätigen, kein API-Key nötig. Benutzt werden die Credits deines "
            "eigenen Magnific-Accounts (bitte nicht mit anderen teilen)."
        )
        mcp_note.setWordWrap(True)
        mcp_note.setStyleSheet("color: grey; font-size: 10px;")
        layout.addWidget(mcp_note)
        mcp_row = QtWidgets.QHBoxLayout()
        self._mcp_status = QtWidgets.QLabel()
        self._mcp_status.setWordWrap(True)
        mcp_row.addWidget(self._mcp_status, 1)
        self._mcp_login = QtWidgets.QPushButton("Anmelden ...")
        self._mcp_login.clicked.connect(self._mcp_start_login)
        mcp_row.addWidget(self._mcp_login)
        self._mcp_logout = QtWidgets.QPushButton("Abmelden")
        self._mcp_logout.clicked.connect(self._mcp_do_logout)
        mcp_row.addWidget(self._mcp_logout)
        layout.addLayout(mcp_row)
        self._bridge = _LoginBridge()
        self._bridge.info.connect(self._mcp_status.setText)
        self._bridge.finished.connect(self._mcp_login_finished)
        self._mcp_cancel = False
        self._mcp_refresh_ui()

        # ---- Optional: eigene API-Keys (eingeklappt, solange keiner gesetzt ist) -------
        layout.addWidget(self._hline())
        any_key = any(config.get_key(n) for n, _l, _h in _KEY_FIELDS)
        self._keys_toggle = QtWidgets.QCheckBox("Eigene API-Keys eintragen (optional)")
        self._keys_toggle.setChecked(any_key)
        layout.addWidget(self._keys_toggle)

        self._keys_wrap = QtWidgets.QWidget()
        keys_layout = QtWidgets.QVBoxLayout(self._keys_wrap)
        keys_layout.setContentsMargins(0, 0, 0, 0)
        intro = QtWidgets.QLabel(
            "Nur nötig für weitere Anbieter oder den API-Modus (Dropdown rechts neben Model: API). "
            "Die Keys werden lokal in\n  " + config.keys_file_path() + "\n"
            "gespeichert (nur auf diesem Rechner). Nur Modelle, deren Key hinterlegt ist, erscheinen "
            "im Model-Dropdown."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: grey; font-size: 10px;")
        keys_layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        for name, label, hint in _KEY_FIELDS:
            edit = QtWidgets.QLineEdit()
            edit.setEchoMode(QtWidgets.QLineEdit.Password)
            current = config.get_key(name)
            if current:
                edit.setText(current)
                edit.setPlaceholderText("gesetzt")
            else:
                edit.setPlaceholderText("nicht gesetzt")
            self._edits[name] = edit
            box = QtWidgets.QVBoxLayout()
            box.setSpacing(1)
            box.addWidget(edit)
            hint_lbl = QtWidgets.QLabel(hint)
            hint_lbl.setWordWrap(True)
            hint_lbl.setStyleSheet("color: grey; font-size: 10px;")
            box.addWidget(hint_lbl)
            wrap = QtWidgets.QWidget()
            wrap.setLayout(box)
            form.addRow(label + ":", wrap)
        keys_layout.addLayout(form)

        show = QtWidgets.QCheckBox("Keys anzeigen")
        show.toggled.connect(self._toggle_echo)
        keys_layout.addWidget(show)
        layout.addWidget(self._keys_wrap)
        self._keys_wrap.setVisible(any_key)
        self._keys_toggle.toggled.connect(self._toggle_keys)

        # ---- Standard-Ausgabeordner ---------------------------------------------------
        layout.addWidget(self._hline())
        out_row = QtWidgets.QHBoxLayout()
        out_row.addWidget(QtWidgets.QLabel("Standard-Ausgabeordner:"))
        self._outdir = QtWidgets.QLineEdit(config.get_value("output_dir") or "")
        self._outdir.setPlaceholderText("leer = automatisch aus dem Shot (<shot>/GenAI)")
        out_row.addWidget(self._outdir, 1)
        browse = QtWidgets.QPushButton("...")
        browse.setFixedWidth(30)
        browse.clicked.connect(self._browse)
        out_row.addWidget(browse)
        layout.addLayout(out_row)
        note = QtWidgets.QLabel(
            "Leer = pro Shot automatisch (<shot>/GenAI/UPLOAD + DOWNLOAD). Gesetzt = fester "
            "Basis-Pfad; darunter werden GenAI/UPLOAD + DOWNLOAD angelegt. Der Node-eigene "
            "'Output Folder' überschreibt das im Einzelfall."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: grey; font-size: 10px;")
        layout.addWidget(note)

        # ---- Buttons ------------------------------------------------------------------
        layout.addWidget(self._hline())
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Close)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _toggle_keys(self, on):
        self._keys_wrap.setVisible(on)
        self.adjustSize()

    # ---- Magnific MCP ---------------------------------------------------------

    def _mcp_refresh_ui(self):
        self._mcp_status.setText(mcp_auth.status_text())
        self._mcp_login.setEnabled(True)
        self._mcp_logout.setEnabled(mcp_auth.is_logged_in())
        self._mcp_login.setText("Erneut anmelden ..." if mcp_auth.is_logged_in() else "Anmelden ...")

    def _mcp_start_login(self):
        self._mcp_login.setEnabled(False)
        self._mcp_cancel = False
        self._mcp_status.setText("starting the login ...")
        threading.Thread(target=self._mcp_login_worker, daemon=True).start()

    def _mcp_login_worker(self):
        try:
            state = mcp_auth.start_device_login()
            url = state.get("verification_uri_complete") or state.get("verification_uri")
            self._bridge.info.emit("Browser opened -- confirm the code {c} on the Magnific page "
                                   "(valid {m} min) ...".format(
                                       c=state.get("user_code", "?"),
                                       m=max(1, int(state.get("expires_in", 600)) // 60)))
            webbrowser.open(url)
            mcp_auth.finish_device_login(state, cancelled=lambda: self._mcp_cancel)
            self._bridge.finished.emit(True, "")
        except Exception as exc:
            self._bridge.finished.emit(False, str(exc))

    def _mcp_login_finished(self, ok, message):
        self._mcp_refresh_ui()
        if not ok:
            self._mcp_status.setText("Anmeldung fehlgeschlagen: " + message)
            return
        self._refresh_nodes()

    def _mcp_do_logout(self):
        mcp_auth.logout()
        self._mcp_refresh_ui()
        self._refresh_nodes()

    def done(self, result):
        self._mcp_cancel = True      # laufenden Login-Wartevorgang beenden
        super(SetupDialog, self).done(result)

    @staticmethod
    def _refresh_nodes():
        """Model-Dropdown aller GenAI-Nodes im Script an die Modellliste anpassen."""
        try:
            from . import nuke_node
            nuke_node.refresh_all_nodes()
        except Exception:
            pass

    # ---- intern ---------------------------------------------------------------

    def _hline(self):
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        return line

    def _toggle_echo(self, on):
        mode = QtWidgets.QLineEdit.Normal if on else QtWidgets.QLineEdit.Password
        for edit in self._edits.values():
            edit.setEchoMode(mode)

    def _browse(self):
        start = self._outdir.text().strip() or os.path.expanduser("~")
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Standard-Ausgabeordner", start)
        if d:
            self._outdir.setText(d)

    def _save(self):
        values = {}
        for name, edit in self._edits.items():
            values[name] = edit.text().strip()  # leer -> löscht den Eintrag
        values["output_dir"] = self._outdir.text().strip()
        try:
            path = config.save_values(values)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "GenAI", "Speichern fehlgeschlagen:\n%s" % exc)
            return
        self._refresh_nodes()
        QtWidgets.QMessageBox.information(
            self, "GenAI",
            "Gespeichert in:\n%s\n\nAlle GenAI-Nodes zeigen jetzt die passenden Modelle." % path)
        self.accept()


_setup_dialog = None


def show_setup():
    """Setup-Dialog öffnen (modal-frei, damit Nuke bedienbar bleibt)."""
    global _setup_dialog
    try:
        if _setup_dialog is not None and _setup_dialog.isVisible():
            _setup_dialog.raise_()
            _setup_dialog.activateWindow()
            return _setup_dialog
    except RuntimeError:        # Qt-Objekt wurde schon zerstoert
        pass
    _setup_dialog = SetupDialog(_nuke_main_window())
    _setup_dialog.show()
    _setup_dialog.raise_()
    return _setup_dialog


# ---- Großer Prompt-Editor ------------------------------------------------------

class PromptDialog(QtWidgets.QDialog):
    """Frei skalierbares Fenster zum Bearbeiten des Prompts."""

    def __init__(self, node, parent=None):
        super(PromptDialog, self).__init__(parent)
        self._node = node
        self.setWindowTitle("GenAI — Prompt")
        self.resize(640, 420)  # frei an der Ecke vergrößerbar
        layout = QtWidgets.QVBoxLayout(self)
        self._text = QtWidgets.QPlainTextEdit()
        try:
            self._text.setPlainText(node["prompt"].value() or "")
        except Exception:
            pass
        layout.addWidget(self._text, 1)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._apply)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _apply(self):
        try:
            self._node["prompt"].setValue(self._text.toPlainText())
        except Exception:
            pass
        self.accept()


_prompt_dialog = None


def edit_prompt(node):
    """Großen Prompt-Editor für die Node öffnen."""
    global _prompt_dialog
    _prompt_dialog = PromptDialog(node, _nuke_main_window())
    _prompt_dialog.show()
    _prompt_dialog.raise_()
    return _prompt_dialog
