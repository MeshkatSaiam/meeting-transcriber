"""
Meeting Transcriber — Mobile (Android) User Interface
Optimized for touchscreens, portrait orientation, and direct Gemini API processing.
No Windows-specific DLLs, no sounddevice dependency.
"""

import os
import sys
import json
import threading
import traceback
from pathlib import Path
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.core.clipboard import Clipboard
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.filechooser import FileChooserIconView
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import get_color_from_hex

from transcribe import (
    DEFAULT_MODEL,
    AVAILABLE_MODELS,
    run_transcription_pipeline,
    generate_meeting_notes,
    save_transcript_docx,
    TranscriptionCancelledException,
    is_ffmpeg_available,
)

# ==============================================================================
# Settings & Storage Helpers
# ==============================================================================

DEFAULT_SETTINGS = {
    "gemini_api_key": "",
    "default_model": DEFAULT_MODEL,
    "generate_notes": True,
}

def get_app_user_data_dir() -> Path:
    """Returns dedicated user_data_dir for Android/Desktop cross-compatibility."""
    try:
        app = App.get_running_app()
        if app and hasattr(app, "user_data_dir") and app.user_data_dir:
            d = Path(app.user_data_dir)
            d.mkdir(parents=True, exist_ok=True)
            return d
    except Exception:
        pass
    d = Path.home() / ".meeting_transcriber"
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_settings_file_path() -> Path:
    return get_app_user_data_dir() / "settings.json"

def load_app_settings() -> dict:
    s_file = get_settings_file_path()
    if not s_file.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(s_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            return merged
    except Exception:
        return dict(DEFAULT_SETTINGS)

def save_app_settings(settings: dict):
    try:
        s_file = get_settings_file_path()
        with open(s_file, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print(f"[Error saving settings]: {e}", file=sys.stderr)

# ==============================================================================
# UI Component Classes
# ==============================================================================

class MobileCard(BoxLayout):
    """Clean dark card container with 10px rounded corners and subtle border."""
    def __init__(self, bg_color="#1E293B", border_color="#334155", radius=10, **kwargs):
        super().__init__(**kwargs)
        self.bg_hex = bg_color
        self.border_hex = border_color
        self.card_radius = radius
        with self.canvas.before:
            self.color_bg = Color(*get_color_from_hex(self.bg_hex))
            self.rect_bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[self.card_radius])
        self.bind(pos=self._update_rect, size=self._update_rect)

    def _update_rect(self, *args):
        self.rect_bg.pos = self.pos
        self.rect_bg.size = self.size

# ==============================================================================
# Main Mobile Screen
# ==============================================================================

class MobileTranscriberLayout(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=0, **kwargs)

        self.settings = load_app_settings()
        if self.settings.get("gemini_api_key"):
            os.environ["GEMINI_API_KEY"] = self.settings["gemini_api_key"]

        self.selected_audio_path = None
        self.current_transcript = ""
        self.current_notes = ""
        self.is_processing = False
        self.cancel_event = threading.Event()
        self.active_tab = "transcribe"  # "transcribe", "results", "settings"
        self.results_subtab = "transcript"  # "transcript" or "notes"

        self._build_ui()

    def _build_ui(self):
        self.clear_widgets()

        # 1. Header Bar
        header = BoxLayout(orientation="horizontal", size_hint_y=None, height=54, padding=[14, 8, 14, 8], spacing=8)
        with header.canvas.before:
            Color(*get_color_from_hex("#0F172A"))
            self.header_bg = RoundedRectangle(pos=header.pos, size=header.size)
        header.bind(pos=lambda *a: setattr(self.header_bg, 'pos', header.pos),
                    size=lambda *a: setattr(self.header_bg, 'size', header.size))

        title_lbl = Label(
            text="Meeting Transcriber",
            font_size="17sp",
            bold=True,
            color=get_color_from_hex("#F8FAFC"),
            halign="left",
            valign="middle"
        )
        title_lbl.bind(size=title_lbl.setter("text_size"))
        header.add_widget(title_lbl)

        # Tab Navigation in Header
        btn_transcribe = Button(
            text="Transcribe",
            size_hint_x=None,
            width=85,
            font_size="12sp",
            bold=(self.active_tab == "transcribe"),
            background_color=get_color_from_hex("#38BDF8" if self.active_tab == "transcribe" else "#334155")
        )
        btn_transcribe.bind(on_release=lambda *a: self.switch_tab("transcribe"))
        header.add_widget(btn_transcribe)

        btn_results = Button(
            text="Results",
            size_hint_x=None,
            width=70,
            font_size="12sp",
            bold=(self.active_tab == "results"),
            background_color=get_color_from_hex("#38BDF8" if self.active_tab == "results" else "#334155")
        )
        btn_results.bind(on_release=lambda *a: self.switch_tab("results"))
        header.add_widget(btn_results)

        btn_settings = Button(
            text="Settings",
            size_hint_x=None,
            width=70,
            font_size="12sp",
            bold=(self.active_tab == "settings"),
            background_color=get_color_from_hex("#38BDF8" if self.active_tab == "settings" else "#334155")
        )
        btn_settings.bind(on_release=lambda *a: self.switch_tab("settings"))
        header.add_widget(btn_settings)

        self.add_widget(header)

        # 2. Body Content (switches between tabs)
        self.content_area = BoxLayout(orientation="vertical", padding=[12, 10, 12, 10], spacing=10)
        self.add_widget(self.content_area)

        self._render_current_tab()

    def switch_tab(self, tab_name: str):
        self.active_tab = tab_name
        self._build_ui()

    def _render_current_tab(self):
        self.content_area.clear_widgets()

        if self.active_tab == "transcribe":
            self._render_transcribe_tab()
        elif self.active_tab == "results":
            self._render_results_tab()
        elif self.active_tab == "settings":
            self._render_settings_tab()

    # --------------------------------------------------------------------------
    # Tab 1: Transcribe
    # --------------------------------------------------------------------------

    def _render_transcribe_tab(self):
        scroll = ScrollView(size_hint=(1, 1), do_scroll_x=False)
        box = BoxLayout(orientation="vertical", spacing=12, size_hint_y=None, padding=[0, 4, 0, 8])
        box.bind(minimum_height=box.setter("height"))

        # Card: Audio File Selection
        card_file = MobileCard(orientation="vertical", size_hint_y=None, height=130, padding=[12, 10, 12, 10], spacing=8)
        lbl_file_title = Label(
            text="Audio Recording",
            font_size="13sp",
            bold=True,
            color=get_color_from_hex("#38BDF8"),
            size_hint_y=None,
            height=20,
            halign="left",
            valign="middle"
        )
        lbl_file_title.bind(size=lbl_file_title.setter("text_size"))
        card_file.add_widget(lbl_file_title)

        file_status_text = "No audio file selected.\nTap 'Choose Audio File' to select a recording."
        if self.selected_audio_path:
            p = Path(self.selected_audio_path)
            try:
                sz_mb = p.stat().st_size / (1024 * 1024)
                file_status_text = f"Selected: {p.name}\nSize: {sz_mb:.1f} MB | Format: {p.suffix.upper()}"
            except Exception:
                file_status_text = f"Selected: {p.name}"

        self.lbl_selected_file = Label(
            text=file_status_text,
            font_size="12sp",
            color=get_color_from_hex("#E2E8F0" if self.selected_audio_path else "#94A3B8"),
            size_hint_y=None,
            height=36,
            halign="left",
            valign="middle"
        )
        self.lbl_selected_file.bind(size=self.lbl_selected_file.setter("text_size"))
        card_file.add_widget(self.lbl_selected_file)

        btn_choose = Button(
            text="Choose Audio File (.m4a, .mp3, .wav)",
            size_hint_y=None,
            height=42,
            font_size="13sp",
            bold=True,
            background_color=get_color_from_hex("#2563EB")
        )
        btn_choose.bind(on_release=self._open_file_picker)
        card_file.add_widget(btn_choose)
        box.add_widget(card_file)

        # Card: Model & Options
        card_opts = MobileCard(orientation="vertical", size_hint_y=None, height=125, padding=[12, 10, 12, 10], spacing=8)
        lbl_model_title = Label(
            text="Model & Processing Options",
            font_size="13sp",
            bold=True,
            color=get_color_from_hex("#38BDF8"),
            size_hint_y=None,
            height=20,
            halign="left",
            valign="middle"
        )
        lbl_model_title.bind(size=lbl_model_title.setter("text_size"))
        card_opts.add_widget(lbl_model_title)

        model_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=8)
        lbl_model = Label(text="Model:", size_hint_x=None, width=55, font_size="12sp", color=get_color_from_hex("#94A3B8"), halign="left", valign="middle")
        lbl_model.bind(size=lbl_model.setter("text_size"))
        model_row.add_widget(lbl_model)

        self.model_spinner = Spinner(
            text=self.settings.get("default_model", DEFAULT_MODEL),
            values=AVAILABLE_MODELS,
            size_hint_y=None,
            height=34,
            font_size="12sp",
            background_color=get_color_from_hex("#0F172A"),
            color=get_color_from_hex("#F8FAFC")
        )
        model_row.add_widget(self.model_spinner)
        card_opts.add_widget(model_row)

        notes_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=32, spacing=8)
        self.chk_notes = CheckBox(size_hint_x=None, width=30, active=self.settings.get("generate_notes", True))
        lbl_notes = Label(text="Generate Executive Meeting Minutes & Notes", font_size="11sp", color=get_color_from_hex("#E2E8F0"), halign="left", valign="middle")
        lbl_notes.bind(size=lbl_notes.setter("text_size"))
        notes_row.add_widget(self.chk_notes)
        notes_row.add_widget(lbl_notes)
        card_opts.add_widget(notes_row)
        box.add_widget(card_opts)

        # Action: Start / Cancel Button
        card_action = MobileCard(orientation="vertical", size_hint_y=None, height=135, padding=[12, 10, 12, 10], spacing=8)
        self.btn_transcribe = Button(
            text="Start Transcription",
            size_hint_y=None,
            height=48,
            font_size="15sp",
            bold=True,
            background_color=get_color_from_hex("#059669")
        )
        self.btn_transcribe.bind(on_release=self._start_transcription)
        card_action.add_widget(self.btn_transcribe)

        self.progress_bar = ProgressBar(max=1.0, value=0.0, size_hint_y=None, height=14)
        card_action.add_widget(self.progress_bar)

        self.lbl_status = Label(
            text="Ready to transcribe.",
            font_size="11sp",
            color=get_color_from_hex("#94A3B8"),
            size_hint_y=None,
            height=26,
            halign="center",
            valign="middle"
        )
        self.lbl_status.bind(size=self.lbl_status.setter("text_size"))
        card_action.add_widget(self.lbl_status)
        box.add_widget(card_action)

        # Quick Tip Box
        card_tip = MobileCard(orientation="vertical", size_hint_y=None, height=85, padding=[10, 8, 10, 8], spacing=4, bg_color="#0F172A")
        tip_title = Label(text="💡 Tip: Recording on Android", font_size="11sp", bold=True, color=get_color_from_hex("#F59E0B"), size_hint_y=None, height=18, halign="left")
        tip_title.bind(size=tip_title.setter("text_size"))
        tip_desc = Label(
            text="Use Fossify Voice Recorder for background & widget recording. Then select your saved audio file here to transcribe.",
            font_size="10sp",
            color=get_color_from_hex("#94A3B8"),
            halign="left",
            valign="top"
        )
        tip_desc.bind(size=tip_desc.setter("text_size"))
        card_tip.add_widget(tip_title)
        card_tip.add_widget(tip_desc)
        box.add_widget(card_tip)

        scroll.add_widget(box)
        self.content_area.add_widget(scroll)

    # --------------------------------------------------------------------------
    # Tab 2: Results
    # --------------------------------------------------------------------------

    def _render_results_tab(self):
        box = BoxLayout(orientation="vertical", spacing=8)

        # Sub-tab selector: [Transcript] | [Meeting Notes]
        subtab_bar = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=6)
        btn_sub_trans = Button(
            text="Transcript",
            font_size="13sp",
            bold=(self.results_subtab == "transcript"),
            background_color=get_color_from_hex("#38BDF8" if self.results_subtab == "transcript" else "#334155")
        )
        btn_sub_trans.bind(on_release=lambda *a: self._set_results_subtab("transcript"))
        subtab_bar.add_widget(btn_sub_trans)

        btn_sub_notes = Button(
            text="Meeting Notes",
            font_size="13sp",
            bold=(self.results_subtab == "notes"),
            background_color=get_color_from_hex("#38BDF8" if self.results_subtab == "notes" else "#334155")
        )
        btn_sub_notes.bind(on_release=lambda *a: self._set_results_subtab("notes"))
        subtab_bar.add_widget(btn_sub_notes)
        box.add_widget(subtab_bar)

        # Text display area
        card_text = MobileCard(orientation="vertical", padding=[8, 8, 8, 8])
        active_text = self.current_transcript if self.results_subtab == "transcript" else self.current_notes
        if not active_text.strip():
            active_text = "No results yet. Go to the Transcribe tab to process an audio recording."

        self.txt_display = TextInput(
            text=active_text,
            readonly=True,
            font_size="13sp",
            background_color=get_color_from_hex("#0F172A"),
            foreground_color=get_color_from_hex("#F8FAFC"),
            cursor_color=get_color_from_hex("#38BDF8")
        )
        card_text.add_widget(self.txt_display)
        box.add_widget(card_text)

        # Bottom action bar: Copy & Save .docx
        actions = BoxLayout(orientation="horizontal", size_hint_y=None, height=44, spacing=8)
        btn_copy = Button(
            text="Copy Text",
            size_hint_x=0.4,
            font_size="13sp",
            background_color=get_color_from_hex("#475569")
        )
        btn_copy.bind(on_release=self._copy_results_to_clipboard)
        actions.add_widget(btn_copy)

        btn_save = Button(
            text="Save .docx Document",
            size_hint_x=0.6,
            font_size="13sp",
            bold=True,
            background_color=get_color_from_hex("#059669")
        )
        btn_save.bind(on_release=self._save_results_docx)
        actions.add_widget(btn_save)
        box.add_widget(actions)

        self.content_area.add_widget(box)

    def _set_results_subtab(self, subtab: str):
        self.results_subtab = subtab
        self._render_current_tab()

    # --------------------------------------------------------------------------
    # Tab 3: Settings
    # --------------------------------------------------------------------------

    def _render_settings_tab(self):
        scroll = ScrollView(size_hint=(1, 1), do_scroll_x=False)
        box = BoxLayout(orientation="vertical", spacing=12, size_hint_y=None, padding=[0, 4, 0, 8])
        box.bind(minimum_height=box.setter("height"))

        # Card: Gemini API Key
        card_api = MobileCard(orientation="vertical", size_hint_y=None, height=135, padding=[12, 10, 12, 10], spacing=8)
        lbl_api_title = Label(
            text="Gemini API Key",
            font_size="13sp",
            bold=True,
            color=get_color_from_hex("#38BDF8"),
            size_hint_y=None,
            height=20,
            halign="left",
            valign="middle"
        )
        lbl_api_title.bind(size=lbl_api_title.setter("text_size"))
        card_api.add_widget(lbl_api_title)

        self.input_api_key = TextInput(
            text=self.settings.get("gemini_api_key", ""),
            password=True,
            multiline=False,
            font_size="13sp",
            size_hint_y=None,
            height=38,
            background_color=get_color_from_hex("#0F172A"),
            foreground_color=get_color_from_hex("#F8FAFC")
        )
        card_api.add_widget(self.input_api_key)

        api_btn_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=34, spacing=8)
        btn_toggle_pw = Button(
            text="Show Key",
            size_hint_x=0.35,
            font_size="11sp",
            background_color=get_color_from_hex("#475569")
        )
        btn_toggle_pw.bind(on_release=lambda *a: self._toggle_password_visibility(btn_toggle_pw))
        api_btn_row.add_widget(btn_toggle_pw)

        btn_save_key = Button(
            text="Save Key",
            size_hint_x=0.65,
            font_size="12sp",
            bold=True,
            background_color=get_color_from_hex("#059669")
        )
        btn_save_key.bind(on_release=self._save_api_key)
        api_btn_row.add_widget(btn_save_key)
        card_api.add_widget(api_btn_row)
        box.add_widget(card_api)

        # Card: System Info
        card_info = MobileCard(orientation="vertical", size_hint_y=None, height=130, padding=[12, 10, 12, 10], spacing=6)
        lbl_info_title = Label(
            text="System & Diagnostics",
            font_size="13sp",
            bold=True,
            color=get_color_from_hex("#38BDF8"),
            size_hint_y=None,
            height=20,
            halign="left",
            valign="middle"
        )
        lbl_info_title.bind(size=lbl_info_title.setter("text_size"))
        card_info.add_widget(lbl_info_title)

        ffmpeg_status = "Available (Parallel Chunk Mode)" if is_ffmpeg_available() else "Not present (Direct Gemini Upload Mode)"
        info_lines = [
            f"FFmpeg Status: {ffmpeg_status}",
            f"Platform: {sys.platform}",
            f"Storage: {get_app_user_data_dir()}",
        ]
        lbl_details = Label(
            text="\n".join(info_lines),
            font_size="11sp",
            color=get_color_from_hex("#94A3B8"),
            halign="left",
            valign="top"
        )
        lbl_details.bind(size=lbl_details.setter("text_size"))
        card_info.add_widget(lbl_details)
        box.add_widget(card_info)

        # Card: Over-The-Air Code Sync (Fast Development)
        card_ota = MobileCard(orientation="vertical", size_hint_y=None, height=155, padding=[12, 10, 12, 10], spacing=6)
        lbl_ota_title = Label(
            text="Over-The-Air (OTA) Code Sync",
            font_size="13sp",
            bold=True,
            color=get_color_from_hex("#38BDF8"),
            size_hint_y=None,
            height=20,
            halign="left",
            valign="middle"
        )
        lbl_ota_title.bind(size=lbl_ota_title.setter("text_size"))
        card_ota.add_widget(lbl_ota_title)

        lbl_ota_desc = Label(
            text="Download the latest Python code directly from GitHub in 3 seconds without rebuilding the APK.",
            font_size="11sp",
            color=get_color_from_hex("#94A3B8"),
            size_hint_y=None,
            height=30,
            halign="left",
            valign="top"
        )
        lbl_ota_desc.bind(size=lbl_ota_desc.setter("text_size"))
        card_ota.add_widget(lbl_ota_desc)

        ota_btn_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=8)
        self.btn_sync_code = Button(
            text="Sync Latest Code from GitHub",
            size_hint_x=0.7,
            font_size="12sp",
            bold=True,
            background_color=get_color_from_hex("#2563EB")
        )
        self.btn_sync_code.bind(on_release=self._start_ota_sync)
        ota_btn_row.add_widget(self.btn_sync_code)

        btn_reset_code = Button(
            text="Reset",
            size_hint_x=0.3,
            font_size="11sp",
            background_color=get_color_from_hex("#475569")
        )
        btn_reset_code.bind(on_release=self._reset_ota_code)
        ota_btn_row.add_widget(btn_reset_code)
        card_ota.add_widget(ota_btn_row)

        self.lbl_ota_status = Label(
            text=self._get_ota_status_text(),
            font_size="10sp",
            color=get_color_from_hex("#10B981" if self._has_ota_updates() else "#94A3B8"),
            size_hint_y=None,
            height=20,
            halign="left",
            valign="middle"
        )
        self.lbl_ota_status.bind(size=self.lbl_ota_status.setter("text_size"))
        card_ota.add_widget(self.lbl_ota_status)
        box.add_widget(card_ota)

        scroll.add_widget(box)
        self.content_area.add_widget(scroll)

    def _toggle_password_visibility(self, button: Button):
        self.input_api_key.password = not self.input_api_key.password
        button.text = "Hide Key" if not self.input_api_key.password else "Show Key"

    def _save_api_key(self, instance=None):
        key = self.input_api_key.text.strip()
        self.settings["gemini_api_key"] = key
        save_app_settings(self.settings)
        os.environ["GEMINI_API_KEY"] = key
        self._show_info_modal("Settings Saved", "Your Gemini API Key has been saved successfully.")

    # --------------------------------------------------------------------------
    # Over-The-Air (OTA) Code Sync Handlers
    # --------------------------------------------------------------------------

    def _has_ota_updates(self) -> bool:
        ota_dir = get_app_user_data_dir() / "ota_updates"
        return ota_dir.exists() and (ota_dir / "mobile_gui.py").exists()

    def _get_ota_status_text(self) -> str:
        if self._has_ota_updates():
            return "Active version: Custom OTA synced scripts"
        return "Active version: Bundled APK scripts"

    def _start_ota_sync(self, instance=None):
        self.btn_sync_code.disabled = True
        self.btn_sync_code.text = "Syncing code..."
        threading.Thread(target=self._ota_sync_worker, daemon=True).start()

    def _ota_sync_worker(self):
        import urllib.request
        try:
            ota_dir = get_app_user_data_dir() / "ota_updates"
            ota_dir.mkdir(parents=True, exist_ok=True)
            files = ["mobile_gui.py", "transcribe.py"]
            base = "https://raw.githubusercontent.com/MeshkatSaiam/meeting-transcriber/main/"
            for f in files:
                url = base + f
                dest = ota_dir / f
                urllib.request.urlretrieve(url, str(dest))
            Clock.schedule_once(lambda dt: self._on_ota_success())
        except Exception as e:
            Clock.schedule_once(lambda dt, err=str(e): self._on_ota_error(err))

    def _on_ota_success(self):
        self.btn_sync_code.disabled = False
        self.btn_sync_code.text = "Sync Latest Code from GitHub"
        self.lbl_ota_status.text = "Active version: Custom OTA synced scripts (Restart to apply)"
        self.lbl_ota_status.color = get_color_from_hex("#10B981")
        self._show_info_modal(
            "Code Sync Complete!",
            "Latest Python code was downloaded successfully!\n\nPlease restart the app to run the new version."
        )

    def _on_ota_error(self, err_msg: str):
        self.btn_sync_code.disabled = False
        self.btn_sync_code.text = "Sync Latest Code from GitHub"
        self._show_info_modal("Sync Error", f"Could not fetch code updates from GitHub:\n\n{err_msg}")

    def _reset_ota_code(self, instance=None):
        import shutil
        ota_dir = get_app_user_data_dir() / "ota_updates"
        if ota_dir.exists():
            shutil.rmtree(str(ota_dir), ignore_errors=True)
        self.lbl_ota_status.text = "Active version: Bundled APK scripts"
        self.lbl_ota_status.color = get_color_from_hex("#94A3B8")
        self._show_info_modal("Reset to Default", "Reverted to APK's original bundled scripts. Please restart the app.")

    # --------------------------------------------------------------------------
    # File Picker Modal
    # --------------------------------------------------------------------------

    def _open_file_picker(self, instance=None):
        modal = ModalView(size_hint=(0.95, 0.90), auto_dismiss=True)
        content = BoxLayout(orientation="vertical", padding=10, spacing=8)

        lbl = Label(text="Select Audio Recording", font_size="15sp", bold=True, size_hint_y=None, height=30)
        content.add_widget(lbl)

        # Start browsing in common audio directories or current dir
        start_path = str(Path.home())
        if sys.platform == "android":
            start_path = "/storage/emulated/0"

        fc = FileChooserIconView(
            path=start_path,
            filters=["*.m4a", "*.mp3", "*.wav", "*.aac", "*.ogg", "*.flac", "*.opus", "*.wma", "*.M4A", "*.MP3", "*.WAV"],
            size_hint=(1, 1)
        )
        content.add_widget(fc)

        btn_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=44, spacing=8)
        btn_cancel = Button(text="Cancel", size_hint_x=0.4, background_color=get_color_from_hex("#475569"))
        btn_cancel.bind(on_release=modal.dismiss)
        btn_row.add_widget(btn_cancel)

        btn_select = Button(text="Select File", size_hint_x=0.6, bold=True, background_color=get_color_from_hex("#059669"))
        def _on_select(btn):
            if fc.selection:
                self.selected_audio_path = fc.selection[0]
                modal.dismiss()
                self._render_current_tab()
        btn_select.bind(on_release=_on_select)
        btn_row.add_widget(btn_select)

        content.add_widget(btn_row)
        modal.add_widget(content)
        modal.open()

    # --------------------------------------------------------------------------
    # Transcription Worker Flow
    # --------------------------------------------------------------------------

    def _start_transcription(self, instance=None):
        if self.is_processing:
            # Cancellation trigger
            self.cancel_event.set()
            self.lbl_status.text = "Cancelling transcription..."
            self.btn_transcribe.disabled = True
            return

        if not self.selected_audio_path or not Path(self.selected_audio_path).exists():
            self._show_info_modal("No File Selected", "Please choose a valid audio recording file before starting.")
            return

        key = self.settings.get("gemini_api_key") or os.getenv("GEMINI_API_KEY")
        if not key:
            self._show_info_modal("Missing API Key", "Please add your Gemini API Key in the Settings tab before transcribing.")
            self.switch_tab("settings")
            return

        self.is_processing = True
        self.cancel_event.clear()
        self.progress_bar.value = 0.05
        self.btn_transcribe.text = "Cancel Transcription"
        self.btn_transcribe.background_color = get_color_from_hex("#DC2626")
        self.lbl_status.text = "Initializing transcription pipeline..."

        model_name = self.model_spinner.text.strip() or DEFAULT_MODEL
        gen_notes = self.chk_notes.active

        threading.Thread(
            target=self._transcribe_worker,
            args=(self.selected_audio_path, model_name, gen_notes),
            daemon=True
        ).start()

    def _transcribe_worker(self, audio_path: str, model_name: str, gen_notes: bool):
        try:
            def _log(msg):
                print(f"[Mobile Worker]: {msg}", flush=True)

            def _status(action, current, total):
                prog = max(0.05, float(current) / max(total, 1)) if total > 0 else 0.1
                Clock.schedule_once(lambda dt: self._update_progress(prog, action))

            # Run core pipeline
            res = run_transcription_pipeline(
                audio_path=audio_path,
                model=model_name,
                auto_save=False,
                log_callback=_log,
                status_callback=_status,
                cancel_event=self.cancel_event
            )

            transcript_text = res.get("transcript", "")
            self.current_transcript = transcript_text

            # Meeting Notes generation
            notes_text = ""
            if gen_notes and transcript_text and not self.cancel_event.is_set():
                Clock.schedule_once(lambda dt: self._update_progress(0.85, "Analyzing transcript & generating notes..."))
                notes_dict = generate_meeting_notes(
                    transcript_text=transcript_text,
                    model=model_name,
                    cancel_event=self.cancel_event
                )
                notes_text = notes_dict.get("notes", "")
                self.current_notes = notes_text

            Clock.schedule_once(lambda dt: self._on_transcription_success(transcript_text, notes_text))

        except TranscriptionCancelledException:
            Clock.schedule_once(lambda dt: self._on_transcription_cancelled())
        except Exception as exc:
            Clock.schedule_once(lambda dt, e=exc: self._on_transcription_error(str(e)))

    def _update_progress(self, val: float, text: str):
        self.progress_bar.value = min(1.0, val)
        self.lbl_status.text = text

    def _on_transcription_success(self, transcript: str, notes: str):
        self.is_processing = False
        self.btn_transcribe.text = "Start Transcription"
        self.btn_transcribe.background_color = get_color_from_hex("#059669")
        self.btn_transcribe.disabled = False
        self.progress_bar.value = 1.0
        self.lbl_status.text = "Transcription complete! View results below."

        # Automatically switch to Results tab
        self.switch_tab("results")

    def _on_transcription_cancelled(self):
        self.is_processing = False
        self.btn_transcribe.text = "Start Transcription"
        self.btn_transcribe.background_color = get_color_from_hex("#059669")
        self.btn_transcribe.disabled = False
        self.progress_bar.value = 0.0
        self.lbl_status.text = "Transcription was cancelled."

    def _on_transcription_error(self, err_msg: str):
        self.is_processing = False
        self.btn_transcribe.text = "Start Transcription"
        self.btn_transcribe.background_color = get_color_from_hex("#059669")
        self.btn_transcribe.disabled = False
        self.progress_bar.value = 0.0
        self.lbl_status.text = f"Error: {err_msg[:60]}..."
        self._show_info_modal("Transcription Error", f"An error occurred during transcription:\n\n{err_msg}")

    # --------------------------------------------------------------------------
    # Export & Clipboard Helpers
    # --------------------------------------------------------------------------

    def _copy_results_to_clipboard(self, instance=None):
        text = self.current_transcript if self.results_subtab == "transcript" else self.current_notes
        if text:
            Clipboard.copy(text)
            self._show_info_modal("Copied", f"The {self.results_subtab} has been copied to your clipboard.")
        else:
            self._show_info_modal("Empty", "No text available to copy.")

    def _save_results_docx(self, instance=None):
        if not self.current_transcript and not self.current_notes:
            self._show_info_modal("No Content", "Please transcribe an audio file before exporting to .docx.")
            return

        out_dir = get_app_user_data_dir() / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        base_name = Path(self.selected_audio_path).stem if self.selected_audio_path else "Meeting"
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        doc_filename = f"{base_name}_{timestamp_str}.docx"
        doc_path = out_dir / doc_filename

        try:
            save_transcript_docx(
                output_path=doc_path,
                title=base_name,
                merged_transcript=self.current_transcript,
                meeting_notes=self.current_notes,
                metadata={
                    "model": self.settings.get("default_model", DEFAULT_MODEL),
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            )
            self._show_info_modal("Saved Successfully", f"Document saved:\n\n{doc_path.name}\n\nLocation:\n{out_dir}")
        except Exception as e:
            self._show_info_modal("Save Error", f"Could not save document:\n{e}")

    # --------------------------------------------------------------------------
    # Info / Alert Modal
    # --------------------------------------------------------------------------

    def _show_info_modal(self, title: str, message: str):
        modal = ModalView(size_hint=(0.85, 0.40), auto_dismiss=True)
        content = BoxLayout(orientation="vertical", padding=14, spacing=10)

        lbl_t = Label(text=title, font_size="15sp", bold=True, color=get_color_from_hex("#38BDF8"), size_hint_y=None, height=26)
        content.add_widget(lbl_t)

        lbl_m = Label(text=message, font_size="12sp", color=get_color_from_hex("#F8FAFC"), halign="center", valign="middle")
        lbl_m.bind(size=lbl_m.setter("text_size"))
        content.add_widget(lbl_m)

        btn_ok = Button(text="OK", size_hint_y=None, height=38, bold=True, background_color=get_color_from_hex("#2563EB"))
        btn_ok.bind(on_release=modal.dismiss)
        content.add_widget(btn_ok)

        modal.add_widget(content)
        modal.open()

# ==============================================================================
# Mobile Application Root
# ==============================================================================

class MobileTranscriberApp(App):
    def build(self):
        self.title = "Meeting Transcriber"
        return MobileTranscriberLayout()

if __name__ == "__main__":
    MobileTranscriberApp().run()
