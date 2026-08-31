import os
import sys
import json
import time
import threading
import subprocess
import traceback
import ctypes
import uuid
from ctypes import wintypes
from pathlib import Path
from datetime import datetime
import sounddevice as sd
import soundfile as sf
import numpy as np

# Configure Kivy window properties before importing UI modules
os.environ["KIVY_NO_ARGS"] = "1"
from kivy.config import Config
Config.set("graphics", "width", "900")
Config.set("graphics", "height", "700")
Config.set("graphics", "minimum_width", "800")
Config.set("graphics", "minimum_height", "600")

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.widget import Widget
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner
from kivy.uix.checkbox import CheckBox
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.slider import Slider
from kivy.uix.relativelayout import RelativeLayout
from kivy.uix.image import Image
from kivy.uix.modalview import ModalView
from kivy.graphics import Color, Rectangle, RoundedRectangle, Line
from kivy.graphics.texture import Texture
from kivy.utils import get_color_from_hex

from transcribe import (
    run_transcription_pipeline,
    generate_meeting_notes,
    save_transcript_docx,
    extract_transcript_from_file,
    upload_to_google_drive,
    delete_intermediate_files,
    get_file_recording_date,
    get_audio_duration,
    format_timestamp,
    format_topic_slug,
    build_meeting_base_name,
    load_voice_samples,
    save_voice_sample,
    update_voice_sample_include,
    update_voice_sample_metadata,
    delete_voice_sample,
    get_sample_display_label,
    extract_waveform_peaks,
    DEFAULT_MODEL,
    TranscriptionCancelledException
)

# Ensure UTF-8 output for Bengali and Unicode characters in Windows terminal
if getattr(sys, "stdout", None) is not None and hasattr(sys.stdout, "encoding") and sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if getattr(sys, "stderr", None) is not None and hasattr(sys.stderr, "encoding") and sys.stderr.encoding != "utf-8":
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

AVAILABLE_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-pro",
    "gemini-3.1-pro"
]

HISTORY_FILE = Path("history.json")

# ==============================================================================
# Windows Native Audio Engine (winmm.dll / MCI)
# Rock-solid playback with millisecond position tracking on all Windows systems.
# ==============================================================================

winmm = ctypes.windll.winmm

class WindowsAudioPlayer:
    """
    Robust native Windows audio player using winmm.dll MCI commands.
    Eliminates third-party audio thread crashes and provides 100% reliable position tracking.
    """
    def __init__(self):
        self.alias = f"player_{uuid.uuid4().hex[:8]}"
        self.is_open = False
        self.is_playing = False
        self.current_path = ""
        self.duration_sec = 0.0

    def open_file(self, file_path: Path | str) -> bool:
        self.close()
        p = Path(file_path).resolve()
        if not p.exists():
            return False

        self.current_path = str(p)
        self.duration_sec = get_audio_duration(p)
        
        # MCI open command
        cmd = f'open "{self.current_path}" alias {self.alias}'
        res = winmm.mciSendStringW(cmd, None, 0, None)
        if res == 0:
            self.is_open = True
            winmm.mciSendStringW(f"set {self.alias} time format milliseconds", None, 0, None)
            return True
        return False

    def play(self, from_sec: float = 0.0, to_sec: float | None = None):
        if not self.is_open:
            return
        from_ms = int(max(0.0, from_sec) * 1000)
        if to_sec is not None and to_sec > from_sec:
            to_ms = int(to_sec * 1000)
            cmd = f"play {self.alias} from {from_ms} to {to_ms}"
        else:
            cmd = f"play {self.alias} from {from_ms}"
        winmm.mciSendStringW(cmd, None, 0, None)
        self.is_playing = True

    def pause(self):
        if self.is_open and self.is_playing:
            winmm.mciSendStringW(f"pause {self.alias}", None, 0, None)
            self.is_playing = False

    def resume(self):
        if self.is_open and not self.is_playing:
            winmm.mciSendStringW(f"resume {self.alias}", None, 0, None)
            self.is_playing = True

    def stop(self):
        if self.is_open:
            winmm.mciSendStringW(f"stop {self.alias}", None, 0, None)
            self.is_playing = False

    def seek(self, pos_sec: float):
        if self.is_open:
            ms = int(max(0.0, pos_sec) * 1000)
            winmm.mciSendStringW(f"seek {self.alias} to {ms}", None, 0, None)

    def get_position_sec(self) -> float:
        if not self.is_open:
            return 0.0
        buf = ctypes.create_unicode_buffer(128)
        res = winmm.mciSendStringW(f"status {self.alias} position", buf, 128, None)
        if res == 0 and buf.value:
            try:
                return float(buf.value) / 1000.0
            except ValueError:
                pass
        return 0.0

    def get_mode(self) -> str:
        if not self.is_open:
            return "stopped"
        buf = ctypes.create_unicode_buffer(128)
        res = winmm.mciSendStringW(f"status {self.alias} mode", buf, 128, None)
        if res == 0 and buf.value:
            return buf.value.lower()
        return "stopped"

    def close(self):
        if self.is_open:
            winmm.mciSendStringW(f"close {self.alias}", None, 0, None)
            self.is_open = False
            self.is_playing = False
            self.current_path = ""

# ==============================================================================
# Windows Native GDI Text Rendering Engine (DirectWrite/Uniscribe Integration)
# Renders complex Bengali script (বাংলা) and bilingual English with 100% fidelity.
# ==============================================================================

gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG)
    ]

def render_gdi_text_texture(
    text: str,
    font_size: int = 16,
    font_family: str = "Nirmala UI",
    text_color: tuple[int, int, int] = (228, 228, 231),
    bg_color: tuple[int, int, int] = (24, 24, 27),
    fixed_width: int = 960,
    is_bold: bool = False,
    margin_x: int = 14,
    margin_y: int = 6
) -> tuple[Texture, int, int]:
    """
    Renders text via Windows native GDI with Uniscribe/DirectWrite complex script shaping.
    Converts the resulting 32-bit DIB bitmap directly to a high-performance Kivy Texture.
    Includes explicit error logging with tracebacks.
    """
    try:
        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            raise RuntimeError("Failed to acquire screen device context via user32.GetDC")

        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            user32.ReleaseDC(None, hdc_screen)
            raise RuntimeError("Failed to create compatible DC via gdi32.CreateCompatibleDC")

        weight = 700 if is_bold else 400
        font_handle = gdi32.CreateFontW(
            -font_size, 0, 0, 0, weight, False, False, False,
            1,  # DEFAULT_CHARSET
            0, 0, 5, 0,  # CLEARTYPE_QUALITY
            font_family
        )
        old_font = gdi32.SelectObject(hdc_mem, font_handle)

        DT_CALCRECT = 0x0400
        DT_WORDBREAK = 0x0010
        DT_NOPREFIX = 0x0800

        draw_width = max(100, fixed_width - (margin_x * 2))
        rect = RECT(0, 0, draw_width, 0)
        user32.DrawTextW(hdc_mem, text, -1, ctypes.byref(rect), DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX)

        width = max(100, fixed_width)
        text_height = rect.bottom - rect.top
        height = max(24, text_height + (margin_y * 2))

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # Top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0

        p_bits = ctypes.c_void_p()
        h_bitmap = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(p_bits), None, 0)
        if not h_bitmap or not p_bits.value:
            raise RuntimeError(f"Failed to create DIB section for dimensions {width}x{height}")

        old_bitmap = gdi32.SelectObject(hdc_mem, h_bitmap)

        # Fill background color (COLORREF in 0x00BBGGRR format)
        r, g, b = bg_color
        bg_ref = r | (g << 8) | (b << 16)
        bg_brush = gdi32.CreateSolidBrush(bg_ref)
        draw_rect = RECT(0, 0, width, height)
        user32.FillRect(hdc_mem, ctypes.byref(draw_rect), bg_brush)
        gdi32.DeleteObject(bg_brush)

        # Text color and transparent text background
        tr, tg, tb = text_color
        text_ref = tr | (tg << 8) | (tb << 16)
        gdi32.SetBkMode(hdc_mem, 1)  # TRANSPARENT
        gdi32.SetTextColor(hdc_mem, text_ref)

        # Draw text
        text_rect = RECT(margin_x, margin_y, width - margin_x, height - margin_y)
        user32.DrawTextW(hdc_mem, text, -1, ctypes.byref(text_rect), DT_WORDBREAK | DT_NOPREFIX)

        byte_count = width * height * 4
        raw_bytes = (ctypes.c_char * byte_count).from_address(p_bits.value)
        buffer_data = bytes(raw_bytes)

        # Cleanup GDI handles
        gdi32.SelectObject(hdc_mem, old_font)
        gdi32.SelectObject(hdc_mem, old_bitmap)
        gdi32.DeleteObject(font_handle)
        gdi32.DeleteObject(h_bitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)

        # Create Kivy Texture
        texture = Texture.create(size=(width, height), colorfmt="bgra")
        texture.blit_buffer(buffer_data, colorfmt="bgra", bufferfmt="ubyte")
        texture.flip_vertical()
        return texture, width, height

    except Exception as e:
        print(f"\n[ERROR in render_gdi_text_texture]: {e}\n{traceback.format_exc()}", file=sys.stderr)
        raise

# ==============================================================================
# Interactive Audio Waveform & Selection Widget
# ==============================================================================

class WaveformAudioWidget(Widget):
    """
    Renders an interactive audio waveform using Canvas Line instructions.
    Supports touch-drag region selection, play/pause, and a real-time moving playhead.
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.audio_path: str = ""
        self.total_duration: float = 0.0
        self.peaks: list[float] = []
        self.playhead_sec: float = 0.0
        self.sel_start_sec: float = 0.0
        self.sel_end_sec: float = 0.0
        self.is_selecting: bool = False
        self.player = WindowsAudioPlayer()
        self.selection_callback = None
        self.playback_event = None
        self.bind(pos=self._on_pos_or_size, size=self._on_pos_or_size)

    def _on_pos_or_size(self, *args):
        self.redraw()

    def load_audio(self, audio_path: str, duration: float | None = None, selection_callback=None):
        self.stop_playback()
        self.audio_path = audio_path
        self.selection_callback = selection_callback
        p = Path(audio_path)
        if not p.exists():
            return

        self.total_duration = duration or get_audio_duration(p)
        if self.total_duration <= 0:
            self.total_duration = 1.0

        # Extract waveform peaks downsampled to 350 points
        self.peaks = extract_waveform_peaks(p, num_peaks=350)
        self.playhead_sec = 0.0
        self.sel_start_sec = 0.0
        self.sel_end_sec = 0.0
        self.is_selecting = False

        # Open native Windows player
        self.player.open_file(p)
        self.redraw()

    def redraw(self):
        self.canvas.clear()
        if self.width < 10 or self.height < 10:
            return

        with self.canvas:
            # 1. Dark Rounded Card Background
            Color(0.09, 0.09, 0.11, 1.0)
            RoundedRectangle(pos=self.pos, size=self.size, radius=[6])
            
            Color(0.18, 0.18, 0.22, 1.0)
            Line(rounded_rectangle=[self.x, self.y, self.width, self.height, 6], width=1)

            # 2. Midline
            center_y = self.y + (self.height / 2.0)
            Color(0.25, 0.25, 0.32, 0.35)
            Line(points=[self.x + 6, center_y, self.x + self.width - 6, center_y], width=1)

            pad_x = 8.0
            usable_w = max(10.0, self.width - (pad_x * 2))

            # 3. Waveform Peaks
            if self.peaks:
                num_p = len(self.peaks)
                Color(0.22, 0.74, 0.97, 0.85)  # Sky blue
                max_bar_h = self.height * 0.78

                for i, peak_val in enumerate(self.peaks):
                    bar_x = self.x + pad_x + (i / float(num_p)) * usable_w
                    h_val = max(2.0, peak_val * max_bar_h)
                    y1 = center_y - (h_val / 2.0)
                    y2 = center_y + (h_val / 2.0)
                    Line(points=[bar_x, y1, bar_x, y2], width=1.8)

            # 4. Selection Highlight Overlay Band
            if self.total_duration > 0 and abs(self.sel_end_sec - self.sel_start_sec) > 0.05:
                s1 = max(0.0, min(self.sel_start_sec, self.sel_end_sec))
                s2 = min(self.total_duration, max(self.sel_start_sec, self.sel_end_sec))

                x1 = self.x + pad_x + (s1 / self.total_duration) * usable_w
                x2 = self.x + pad_x + (s2 / self.total_duration) * usable_w
                sel_w = max(2.0, x2 - x1)

                Color(0.23, 0.51, 0.96, 0.35)  # Translucent blue highlight
                Rectangle(pos=(x1, self.y + 2), size=(sel_w, self.height - 4))

                Color(0.38, 0.65, 0.98, 0.9)  # Boundary borders
                Line(points=[x1, self.y + 2, x1, self.y + self.height - 2], width=1.5)
                Line(points=[x2, self.y + 2, x2, self.y + self.height - 2], width=1.5)

            # 5. Playhead Line
            if self.total_duration > 0 and self.playhead_sec >= 0:
                px = self.x + pad_x + (min(self.total_duration, self.playhead_sec) / self.total_duration) * usable_w
                Color(0.94, 0.27, 0.27, 0.95)  # Vibrant red playhead
                Line(points=[px, self.y + 2, px, self.y + self.height - 2], width=2.0)

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos) or self.total_duration <= 0:
            return super().on_touch_down(touch)

        pad_x = 8.0
        usable_w = max(10.0, self.width - (pad_x * 2))
        rel_x = max(0.0, min(usable_w, touch.x - (self.x + pad_x)))
        ratio = rel_x / usable_w
        self.sel_start_sec = ratio * self.total_duration
        self.sel_end_sec = self.sel_start_sec
        self.playhead_sec = self.sel_start_sec
        self.is_selecting = True
        
        self.player.seek(self.playhead_sec)
        self.redraw()
        if self.selection_callback:
            self.selection_callback(self.sel_start_sec, self.sel_end_sec)
        return True

    def on_touch_move(self, touch):
        if not self.is_selecting or self.total_duration <= 0:
            return super().on_touch_move(touch)

        pad_x = 8.0
        usable_w = max(10.0, self.width - (pad_x * 2))
        rel_x = max(0.0, min(usable_w, touch.x - (self.x + pad_x)))
        ratio = rel_x / usable_w
        self.sel_end_sec = ratio * self.total_duration
        self.redraw()
        if self.selection_callback:
            self.selection_callback(min(self.sel_start_sec, self.sel_end_sec), max(self.sel_start_sec, self.sel_end_sec))
        return True

    def on_touch_up(self, touch):
        if self.is_selecting:
            self.is_selecting = False
            s1 = min(self.sel_start_sec, self.sel_end_sec)
            s2 = max(self.sel_start_sec, self.sel_end_sec)
            self.redraw()
            if self.selection_callback:
                self.selection_callback(s1, s2)
            return True
        return super().on_touch_up(touch)

    def toggle_play(self, play_button=None):
        if not self.player.is_open:
            if self.audio_path:
                self.player.open_file(self.audio_path)
            else:
                return False

        if self.player.is_playing:
            self.stop_playback(play_button)
            return False
        else:
            s1 = min(self.sel_start_sec, self.sel_end_sec)
            s2 = max(self.sel_start_sec, self.sel_end_sec)
            start_pos = s1 if (s2 - s1 > 0.3) else self.playhead_sec
            end_pos = s2 if (s2 - s1 > 0.3) else None

            self.player.play(from_sec=start_pos, to_sec=end_pos)
            if play_button:
                play_button.text = "|| Pause"
                play_button.background_color = get_color_from_hex("#D97706")

            if self.playback_event:
                self.playback_event.cancel()
            self.playback_event = Clock.schedule_interval(lambda dt: self._sync_playback(play_button), 1.0 / 30.0)
            return True

    def _sync_playback(self, play_button=None):
        if not self.player.is_open:
            self.stop_playback(play_button)
            return

        pos = self.player.get_position_sec()
        self.playhead_sec = pos
        s1 = min(self.sel_start_sec, self.sel_end_sec)
        s2 = max(self.sel_start_sec, self.sel_end_sec)

        # Stop when reaching end of selection or audio
        if (s2 - s1 > 0.3 and pos >= s2) or self.player.get_mode() == "stopped" or (self.total_duration > 0 and pos >= self.total_duration - 0.1):
            self.stop_playback(play_button)
            self.playhead_sec = s1 if (s2 - s1 > 0.3) else 0.0
            self.redraw()
            return

        self.redraw()

    def stop_playback(self, play_button=None):
        if self.playback_event:
            self.playback_event.cancel()
            self.playback_event = None
        self.player.stop()
        if play_button:
            play_button.text = "> Play"
            play_button.background_color = get_color_from_hex("#2563EB")
        self.redraw()

    def unload_audio(self):
        self.stop_playback()
        self.player.close()

# ==============================================================================
# UI Component Classes (Windows 11 Dark Mica Design Tokens)
# ==============================================================================

class SurfaceCard(BoxLayout):
    """
    Elevated Mica Surface Card with 12px rounded corners, 1px subtle border,
    and dark surface background (#1E2025 / #2B2D35).
    """
    def __init__(self, bg_color="#1E2025", border_color="#2B2D35", radius=12, **kwargs):
        super().__init__(**kwargs)
        self.bg_hex = bg_color
        self.border_hex = border_color
        self.radius_val = radius
        with self.canvas.before:
            self.bg_color_instr = Color(*get_color_from_hex(self.bg_hex))
            self.bg_rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[self.radius_val])
            self.border_color_instr = Color(*get_color_from_hex(self.border_hex))
            self.border_line = Line(rounded_rectangle=(self.x, self.y, self.width, self.height, self.radius_val), width=1.0)
        self.bind(pos=self._update_rect, size=self._update_rect)

    def _update_rect(self, *args):
        self.bg_rect.pos = self.pos
        self.bg_rect.size = self.size
        self.border_line.rounded_rectangle = (self.x, self.y, self.width, self.height, self.radius_val)

    def set_active_glow(self, active: bool):
        if active:
            self.border_color_instr.rgba = get_color_from_hex("#3B82F6")
            self.border_line.width = 1.5
        else:
            self.border_color_instr.rgba = get_color_from_hex(self.border_hex)
            self.border_line.width = 1.0

class NavPillButton(Button):
    """
    Navigation Pill with 20px rounded corners and glowing electric blue active border.
    """
    def __init__(self, is_active=False, **kwargs):
        kwargs.setdefault("background_normal", "")
        kwargs.setdefault("background_color", (0, 0, 0, 0))
        super().__init__(**kwargs)
        self.is_active = is_active
        with self.canvas.before:
            self.bg_col = Color(*get_color_from_hex("#1E2025"))
            self.bg_rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[18])
            self.border_col = Color(*get_color_from_hex("#3B82F6" if is_active else "#2B2D35"))
            self.border_line = Line(rounded_rectangle=(self.x, self.y, self.width, self.height, 18), width=1.5 if is_active else 1.0)
        self.bind(pos=self._update_shape, size=self._update_shape)

    def _update_shape(self, *args):
        self.bg_rect.pos = self.pos
        self.bg_rect.size = self.size
        self.border_line.rounded_rectangle = (self.x, self.y, self.width, self.height, 18)

    def set_active(self, active: bool):
        self.is_active = active
        if active:
            self.color = get_color_from_hex("#FFFFFF")
            self.border_col.rgba = get_color_from_hex("#3B82F6")
            self.border_line.width = 1.5
            self.bold = True
        else:
            self.color = get_color_from_hex("#9CA3AF")
            self.border_col.rgba = get_color_from_hex("#2B2D35")
            self.border_line.width = 1.0
            self.bold = False

class AvatarCircle(BoxLayout):
    """Circular avatar initials badge."""
    def __init__(self, text: str = "U", bg_hex="#2B2D35", border_hex="#3B82F6", size_val=26, **kwargs):
        kwargs.setdefault("size_hint", (None, None))
        kwargs.setdefault("size", (size_val, size_val))
        super().__init__(**kwargs)
        self.bg_hex = bg_hex
        self.border_hex = border_hex
        self.radius_val = size_val / 2.0
        with self.canvas.before:
            self.bg_color_instr = Color(*get_color_from_hex(self.bg_hex))
            self.bg_rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[self.radius_val])
            self.border_color_instr = Color(*get_color_from_hex(self.border_hex))
            self.border_line = Line(rounded_rectangle=(self.x, self.y, self.width, self.height, self.radius_val), width=1.0)
        self.bind(pos=self._update, size=self._update)

        lbl = Label(
            text=text[:2].upper(),
            font_size="10sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            halign="center",
            valign="middle"
        )
        self.add_widget(lbl)

    def _update(self, *args):
        rad = min(self.width, self.height) / 2.0
        self.bg_rect.pos = self.pos
        self.bg_rect.size = self.size
        self.bg_rect.radius = [rad]
        self.border_line.rounded_rectangle = (self.x, self.y, self.width, self.height, rad)

class GraphicalVUMeter(BoxLayout):
    """
    Active graphical VU meter widget matching media_1788025566463.jpg.
    Displays dB scale markings (-18 -15 -12 -9 -3 0 +1 +6 +9), dual-channel LED segment bars, and VU label.
    """
    def __init__(self, **kwargs):
        kwargs.setdefault("orientation", "vertical")
        kwargs.setdefault("size_hint_y", None)
        kwargs.setdefault("height", 34)
        kwargs.setdefault("spacing", 1)
        kwargs.setdefault("padding", [4, 2, 4, 2])
        super().__init__(**kwargs)
        self.level = 0.0

        with self.canvas.before:
            self.bg_col = Color(*get_color_from_hex("#08090B"))
            self.bg_rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[4])
            self.border_col = Color(*get_color_from_hex("#2B2D35"))
            self.border_line = Line(rounded_rectangle=(self.x, self.y, self.width, self.height, 4), width=1.0)
        self.bind(pos=self._update_bg, size=self._update_bg)

        # Scale markings
        scale_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=10)
        for mark in ["-18", "-15", "-12", "-9", "-3", "0", "+1", "+6", "+9"]:
            col = "#10B981" if mark.startswith("-") else ("#F59E0B" if mark in ["0", "+1"] else "#EF4444")
            m_lbl = Label(text=mark, font_size="7sp", color=get_color_from_hex(col), halign="center", valign="middle")
            scale_row.add_widget(m_lbl)
        self.add_widget(scale_row)

        # Segment Bars Canvas Widget
        self.bars_widget = Widget(size_hint=(1, 1))
        self.bars_widget.bind(pos=self.redraw_bars, size=self.redraw_bars)
        self.add_widget(self.bars_widget)

    def _update_bg(self, *args):
        self.bg_rect.pos = self.pos
        self.bg_rect.size = self.size
        self.border_line.rounded_rectangle = (self.x, self.y, self.width, self.height, 4)

    def set_level(self, peak: float):
        self.level = max(0.0, min(1.0, peak))
        self.redraw_bars()

    def redraw_bars(self, *args):
        self.bars_widget.canvas.after.clear()
        with self.bars_widget.canvas.after:
            w = self.bars_widget.width
            h = self.bars_widget.height
            if w <= 10 or h <= 4:
                return

            num_segments = 24
            active_segs = int(self.level * num_segments)
            seg_w = max(2.0, (w - (num_segments - 1) * 2) / num_segments)
            seg_h = max(2.0, (h - 2) / 2.0)

            for channel_idx, y_offset in enumerate([self.bars_widget.y + seg_h + 1, self.bars_widget.y]):
                for i in range(num_segments):
                    sx = self.bars_widget.x + i * (seg_w + 2)
                    ratio = i / float(num_segments)

                    if ratio < 0.65:
                        c = get_color_from_hex("#10B981") if i < active_segs else get_color_from_hex("#043324")
                    elif ratio < 0.85:
                        c = get_color_from_hex("#F59E0B") if i < active_segs else get_color_from_hex("#542407")
                    else:
                        c = get_color_from_hex("#EF4444") if i < active_segs else get_color_from_hex("#591313")

                    Color(*c)
                    Rectangle(pos=(sx, y_offset), size=(seg_w, seg_h))

class VoiceChipButton(BoxLayout):
    """
    Selectable Voice Reference chip matching media_1788025566463.jpg.
    Features a circular avatar badge with initials on the left, and speaker name on the right.
    """
    def __init__(self, sample_dict: dict, on_toggle_callback=None, **kwargs):
        kwargs.setdefault("orientation", "horizontal")
        kwargs.setdefault("size_hint_y", None)
        kwargs.setdefault("height", 32)
        kwargs.setdefault("spacing", 6)
        kwargs.setdefault("padding", [6, 4, 6, 4])
        super().__init__(**kwargs)
        self.sample = sample_dict
        self.on_toggle_callback = on_toggle_callback
        self.is_active = bool(sample_dict.get("include_in_transcription", True))

        name = sample_dict.get("name", "Voice")
        desc = sample_dict.get("description", "").strip()
        if desc:
            short_desc = desc if len(desc) <= 5 else (desc[:3] + "..")
            disp_text = f"{name} ({short_desc})"
        else:
            disp_text = f"{name}"

        initials = (name[:2] if len(name) >= 2 else name).upper()

        with self.canvas.before:
            self.bg_col = Color(*get_color_from_hex("#1E293B" if self.is_active else "#16171B"))
            self.bg_rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[8])
            self.border_col = Color(*get_color_from_hex("#3B82F6" if self.is_active else "#2B2D35"))
            self.border_line = Line(rounded_rectangle=(self.x, self.y, self.width, self.height, 8), width=1.5 if self.is_active else 1.0)
        self.bind(pos=self._update, size=self._update)

        # Avatar circle
        self.avatar = AvatarCircle(text=initials, bg_hex="#27272A", border_hex="#3B82F6" if self.is_active else "#4B5563", size_val=22)
        self.add_widget(self.avatar)

        # Name label
        self.lbl = Label(
            text=disp_text,
            font_size="11sp",
            bold=self.is_active,
            color=get_color_from_hex("#FFFFFF" if self.is_active else "#9CA3AF"),
            halign="left",
            valign="middle",
            shorten=True,
            shorten_from="right"
        )
        self.lbl.bind(size=self.lbl.setter("text_size"))
        self.add_widget(self.lbl)

    def _update(self, *args):
        self.bg_rect.pos = self.pos
        self.bg_rect.size = self.size
        self.border_line.rounded_rectangle = (self.x, self.y, self.width, self.height, 8)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self.is_active = not self.is_active
            self.sample["include_in_transcription"] = self.is_active
            update_voice_sample_include(self.sample.get("id"), self.is_active)
            self.set_active_state(self.is_active)
            if self.on_toggle_callback:
                self.on_toggle_callback(self.sample, self.is_active)
            return True
        return super().on_touch_down(touch)

    def set_active_state(self, active: bool):
        self.is_active = active
        if active:
            self.bg_col.rgba = get_color_from_hex("#1E293B")
            self.border_col.rgba = get_color_from_hex("#3B82F6")
            self.border_line.width = 1.5
            self.lbl.color = get_color_from_hex("#FFFFFF")
            self.lbl.bold = True
        else:
            self.bg_col.rgba = get_color_from_hex("#16171B")
            self.border_col.rgba = get_color_from_hex("#2B2D35")
            self.border_line.width = 1.0
            self.lbl.color = get_color_from_hex("#9CA3AF")
            self.lbl.bold = False

# ==============================================================================
# History Storage Helpers
# ==============================================================================

def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_history_entry(entry: dict):
    history = load_history()
    history = [h for h in history if h.get("file_path") != entry.get("file_path")]
    history.insert(0, entry)
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Error saving history]: {e}", file=sys.stderr)

RECORDER_CONFIG_FILE = Path("recordings") / ".recorder_config.json"

def load_recorder_config() -> dict:
    if not RECORDER_CONFIG_FILE.exists():
        return {"last_device_name": "", "manual_gain": "2.0x (Default)"}
    try:
        with open(RECORDER_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_device_name": "", "manual_gain": "2.0x (Default)"}

def save_recorder_config(config: dict):
    try:
        RECORDER_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RECORDER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"[Error saving recorder config]: {e}", file=sys.stderr)

# ==============================================================================
# Persistent App Storage & Cross-Platform Directories (user_data_dir)
# ==============================================================================

def get_app_user_data_dir() -> Path:
    """Returns the dedicated user_data_dir for Android/Desktop cross-compatibility."""
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

def get_draft_file_path() -> Path:
    return get_app_user_data_dir() / "draft_session.json"

def get_error_log_path() -> Path:
    return get_app_user_data_dir() / "app_error.log"

def get_settings_file_path() -> Path:
    return get_app_user_data_dir() / "settings.json"

def get_daily_usage_file_path() -> Path:
    return get_app_user_data_dir() / "daily_usage.json"

def save_draft_session(data: dict):
    """Silently auto-saves raw transcript & session state the moment it's generated."""
    try:
        draft_file = get_draft_file_path()
        with open(draft_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[Draft Auto-Save] Saved draft session to: {draft_file}", flush=True)
    except Exception as e:
        print(f"[Error saving draft session]: {e}", file=sys.stderr)

def load_draft_session() -> dict | None:
    try:
        draft_file = get_draft_file_path()
        if draft_file.exists():
            with open(draft_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data and data.get("transcript") and not data.get("completed", False):
                    return data
    except Exception as e:
        print(f"[Error loading draft session]: {e}", file=sys.stderr)
    return None

def clear_draft_session():
    try:
        draft_file = get_draft_file_path()
        if draft_file.exists():
            draft_file.unlink()
            print("[Draft Auto-Save] Cleared prior draft session.", flush=True)
    except Exception as e:
        print(f"[Error clearing draft session]: {e}", file=sys.stderr)

def log_error_to_file(error_msg: str, exc_trace: str = ""):
    """Appends timestamped error and full Python traceback to app_error.log in user_data_dir."""
    try:
        log_file = get_error_log_path()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] ERROR: {error_msg}\n")
            if exc_trace:
                f.write(f"{exc_trace}\n")
            f.write("=" * 60 + "\n\n")
    except Exception as e:
        print(f"[Error writing to log file]: {e}", file=sys.stderr)

DEFAULT_SETTINGS = {
    "default_model": "gemini-3.5-flash-lite",
    "default_chunk_mode": "auto",
    "default_chunk_minutes": "30",
    "default_output_folder": "",
    "gemini_api_key": ""
}

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
        print(f"[Settings] Saved settings to: {s_file}", flush=True)
    except Exception as e:
        print(f"[Error saving settings]: {e}", file=sys.stderr)

def get_daily_api_usage() -> tuple[int, str]:
    """Tracks count of Gemini API calls made per calendar day, resetting when date changes."""
    usage_file = get_daily_usage_file_path()
    today_str = datetime.now().strftime("%Y-%m-%d")
    if not usage_file.exists():
        return 0, today_str
    try:
        with open(usage_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("date") == today_str:
                return int(data.get("count", 0)), today_str
            else:
                # New calendar day -> reset
                return 0, today_str
    except Exception:
        return 0, today_str

def increment_daily_api_usage() -> int:
    usage_file = get_daily_usage_file_path()
    today_str = datetime.now().strftime("%Y-%m-%d")
    count, _ = get_daily_api_usage()
    new_count = count + 1
    try:
        with open(usage_file, "w", encoding="utf-8") as f:
            json.dump({"date": today_str, "count": new_count}, f, indent=2)
    except Exception as e:
        print(f"[Error saving daily usage]: {e}", file=sys.stderr)
    return new_count

def prompt_voice_sample_metadata_dialog(
    title: str,
    header_text: str,
    default_name: str = "",
    default_desc: str = ""
) -> tuple[str | None, str]:
    """
    Opens a clean native modal dialog for entering Speaker Name and Description (role/context).
    Returns (name, desc) or (None, "") if cancelled.
    """
    import tkinter as tk
    
    result = {"name": None, "desc": ""}
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(bg="#1E1E24")
    
    win_w, win_h = 440, 260
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = (sw - win_w) // 2
    y = (sh - win_h) // 2
    root.geometry(f"{win_w}x{win_h}+{x}+{y}")
    
    pad_frame = tk.Frame(root, bg="#1E1E24", padx=18, pady=16)
    pad_frame.pack(fill="both", expand=True)
    
    header_lbl = tk.Label(
        pad_frame,
        text=header_text,
        font=("Segoe UI", 10, "bold"),
        fg="#F3F4F6",
        bg="#1E1E24",
        wraplength=400,
        justify="left"
    )
    header_lbl.pack(anchor="w", pady=(0, 10))
    
    name_lbl = tk.Label(
        pad_frame,
        text="Speaker Name (Exact name for transcripts):",
        font=("Segoe UI", 9, "bold"),
        fg="#D1D5DB",
        bg="#1E1E24"
    )
    name_lbl.pack(anchor="w", pady=(0, 2))
    
    name_entry = tk.Entry(
        pad_frame,
        font=("Segoe UI", 10),
        bg="#27272A",
        fg="#FFFFFF",
        insertbackground="#38BDF8",
        relief="flat",
        bd=4
    )
    name_entry.insert(0, default_name)
    name_entry.pack(fill="x", pady=(0, 8))
    name_entry.focus_set()
    
    desc_lbl = tk.Label(
        pad_frame,
        text="Description / Role (Optional, e.g. Client Rep, Vendor):",
        font=("Segoe UI", 9, "bold"),
        fg="#D1D5DB",
        bg="#1E1E24"
    )
    desc_lbl.pack(anchor="w", pady=(0, 2))
    
    desc_entry = tk.Entry(
        pad_frame,
        font=("Segoe UI", 10),
        bg="#27272A",
        fg="#FFFFFF",
        insertbackground="#38BDF8",
        relief="flat",
        bd=4
    )
    desc_entry.insert(0, default_desc)
    desc_entry.pack(fill="x", pady=(0, 12))
    
    btn_frame = tk.Frame(pad_frame, bg="#1E1E24")
    btn_frame.pack(fill="x", side="bottom")
    
    def _on_save():
        n = name_entry.get().strip()
        d = desc_entry.get().strip()
        if n:
            result["name"] = n
            result["desc"] = d
        root.destroy()
        
    def _on_cancel():
        root.destroy()
        
    save_btn = tk.Button(
        btn_frame,
        text="Save Sample",
        font=("Segoe UI", 9, "bold"),
        bg="#2563EB",
        fg="#FFFFFF",
        activebackground="#1D4ED8",
        activeforeground="#FFFFFF",
        relief="flat",
        padx=14,
        pady=4,
        command=_on_save
    )
    save_btn.pack(side="right", padx=(8, 0))
    
    cancel_btn = tk.Button(
        btn_frame,
        text="Cancel",
        font=("Segoe UI", 9),
        bg="#374151",
        fg="#E5E7EB",
        activebackground="#4B5563",
        activeforeground="#FFFFFF",
        relief="flat",
        padx=12,
        pady=4,
        command=_on_cancel
    )
    cancel_btn.pack(side="right")
    
    root.bind("<Return>", lambda e: _on_save())
    root.bind("<Escape>", lambda e: _on_cancel())
    
    root.mainloop()
    return result["name"], result["desc"]

# ==============================================================================
# Main Application GUI
# ==============================================================================

class TranscriberGUI(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=8, padding=14, **kwargs)
        Window.clearcolor = get_color_from_hex("#111113")
        
        # Session State
        self.is_processing = False
        self.current_title: str = "meeting_transcript"
        self.current_topic_slug: str = ""
        self.company_name: str = ""
        self.meeting_type: str = ""
        self.person_names: list[str] = []
        self.recording_date: str = ""
        self.source_audio_path: str = ""
        self.current_transcript: str = ""
        self.current_meeting_notes: str = ""
        self.current_metadata: dict = {}
        self.output_docx_path: Path | None = None
        # Standalone audio player & playback state for sample library clips
        self.sample_player = WindowsAudioPlayer()
        self.active_sample_button = None
        self.active_sample_path = None
        self.active_sample_clock = None

        # Live Microphone Meeting Recorder state
        self.is_recording: bool = False
        self.recording_stream = None
        self.recording_frames: list[np.ndarray] = []
        self.recording_start_time: float = 0.0
        self.recording_clock = None
        self.recording_sample_rate: int = 16000
        self.input_devices_map: list[dict] = []
        self.selected_input_device_idx: int = -1
        self.selected_input_device_name: str = ""
        self.manual_gain_multiplier: float = 2.0
        self.live_input_peak: float = 0.0

        # Cancellation state
        self.cancel_event = threading.Event()

        # Persistent Settings & Daily Usage
        self.settings = load_app_settings()
        os.environ["GEMINI_API_KEY"] = self.settings.get("gemini_api_key", "")
        self.daily_usage_count, self.daily_usage_date = get_daily_api_usage()

        self._setup_window_drag_and_drop()
        self._build_ui()
        self._check_ffmpeg()
        self.refresh_audio_input_devices()
        self.switch_to_transcribe_tab()

        # Check for unfinalized draft recovery on startup
        Clock.schedule_once(lambda dt: self.check_and_prompt_draft_recovery(), 0.6)


    def _check_ffmpeg(self):
        try:
            subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            def show_ffmpeg_warning(dt):
                popup = ModalView(size_hint=(None, None), size=(450, 250), auto_dismiss=False, background_color=(0,0,0,0.8))
                layout = BoxLayout(orientation="vertical", padding=20, spacing=15)
                lbl1 = Label(text="FFmpeg is Missing!", font_size="16sp", bold=True, color=get_color_from_hex("#EF4444"), size_hint_y=None, height=30)
                lbl2 = Label(text="This application requires FFmpeg for audio processing.\nPlease install FFmpeg and ensure it is added to your system PATH.\n(Or place ffmpeg.exe in the same folder as this application).", font_size="13sp", color=get_color_from_hex("#F9FAFB"), text_size=(400, None), halign="center")
                btn = Button(text="Exit Application", size_hint_y=None, height=40, background_normal="", background_color=get_color_from_hex("#EF4444"))
                btn.bind(on_release=lambda x: __import__('sys').exit(1))
                layout.add_widget(lbl1)
                layout.add_widget(lbl2)
                layout.add_widget(btn)
                popup.add_widget(layout)
                popup.open()
            Clock.schedule_once(show_ffmpeg_warning, 0.5)

    def _setup_window_drag_and_drop(self):
        try:
            Window.bind(on_drop_file=self._on_window_drop_file)
        except Exception:
            pass

    def _on_window_drop_file(self, window, file_path, x, y):
        try:
            path_str = file_path.decode("utf-8") if isinstance(file_path, bytes) else str(file_path)
            p = Path(path_str)
            if p.exists() and p.is_file():
                self.file_input.text = str(p.resolve())
                self.load_audio_file(p)
                self.update_live_status(f"Loaded audio: {p.name}", 0, 1)
        except Exception as e:
            print(f"[Drag & Drop Error]: {e}", file=sys.stderr)

    def _build_ui(self):
        # 1. Top Navigation & Global Header Bar
        nav_bar = BoxLayout(orientation="horizontal", size_hint_y=None, height=40, spacing=8)
        
        title_box = BoxLayout(orientation="vertical", size_hint_x=0.28, spacing=1)
        app_title = Label(
            text="Bilingual Meeting Transcriber",
            font_size="15sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            halign="left",
            valign="middle"
        )
        app_title.bind(size=app_title.setter("text_size"))
        title_box.add_widget(app_title)

        pills_box = BoxLayout(orientation="horizontal", size_hint_x=0.58, spacing=6)
        self.tab_btn_transcribe = NavPillButton(
            text="Transcribe & Notes",
            is_active=True,
            font_size="12sp"
        )
        self.tab_btn_transcribe.bind(on_release=lambda x: self.switch_to_transcribe_tab())

        self.tab_btn_samples = NavPillButton(
            text=f"Sample Library ({len(load_voice_samples())})",
            is_active=False,
            font_size="12sp"
        )
        self.tab_btn_samples.bind(on_release=lambda x: self.switch_to_samples_tab())

        self.tab_btn_history = NavPillButton(
            text=f"History ({len(load_history())})",
            is_active=False,
            font_size="12sp"
        )
        self.tab_btn_history.bind(on_release=lambda x: self.switch_to_history_tab())

        self.tab_btn_settings = NavPillButton(
            text="Settings",
            is_active=False,
            font_size="12sp"
        )
        self.tab_btn_settings.bind(on_release=lambda x: self.switch_to_settings_tab())

        pills_box.add_widget(self.tab_btn_transcribe)
        pills_box.add_widget(self.tab_btn_samples)
        pills_box.add_widget(self.tab_btn_history)
        pills_box.add_widget(self.tab_btn_settings)

        self.new_session_btn = Button(
            text="+ New Project",
            bold=True,
            background_normal="",
            background_color=get_color_from_hex("#3B82F6"),
            color=get_color_from_hex("#FFFFFF"),
            size_hint_x=0.14,
            font_size="12sp"
        )
        with self.new_session_btn.canvas.before:
            Color(*get_color_from_hex("#3B82F6"))
            self.new_btn_rect = RoundedRectangle(pos=self.new_session_btn.pos, size=self.new_session_btn.size, radius=[18])
        self.new_session_btn.bind(pos=lambda s, p: setattr(self.new_btn_rect, "pos", p), size=lambda s, sz: setattr(self.new_btn_rect, "size", sz))
        self.new_session_btn.bind(on_release=self.new_session)

        nav_bar.add_widget(title_box)
        nav_bar.add_widget(pills_box)
        nav_bar.add_widget(self.new_session_btn)
        self.add_widget(nav_bar)

        # 2. Main Content Container
        self.content_area = BoxLayout(orientation="vertical", spacing=8)
        self.add_widget(self.content_area)

        # Build Sub-Views
        self._build_transcribe_view()
        self._build_sample_library_view()
        self._build_history_view()
        self._build_settings_view()

    def _build_transcribe_view(self):
        self.transcribe_view = BoxLayout(orientation="vertical", spacing=8)

        # A. Audio Input Surface Card
        input_card = SurfaceCard(orientation="vertical", size_hint_y=None, height=130, padding=[12, 10, 12, 10], spacing=6)
        
        input_header = Label(
            text="Audio Input",
            font_size="13sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            size_hint_y=None,
            height=16,
            halign="left",
            valign="middle"
        )
        input_header.bind(size=input_header.setter("text_size"))
        input_card.add_widget(input_header)

        # Drop Zone Button
        self.drop_zone_btn = Button(
            text="  Drag & Drop Audio (MP3, WAV, etc.) or Browse",
            font_size="12sp",
            bold=True,
            background_normal="",
            background_color=get_color_from_hex("#16171B"),
            color=get_color_from_hex("#9CA3AF"),
            size_hint_y=None,
            height=46
        )
        with self.drop_zone_btn.canvas.before:
            Color(*get_color_from_hex("#16171B"))
            self.dz_bg = RoundedRectangle(pos=self.drop_zone_btn.pos, size=self.drop_zone_btn.size, radius=[8])
            Color(*get_color_from_hex("#2B2D35"))
            self.dz_border = Line(rounded_rectangle=(self.drop_zone_btn.x, self.drop_zone_btn.y, self.drop_zone_btn.width, self.drop_zone_btn.height, 8), width=1.0)
        self.drop_zone_btn.bind(
            pos=lambda s, p: (setattr(self.dz_bg, "pos", p), setattr(self.dz_border, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8))),
            size=lambda s, sz: (setattr(self.dz_bg, "size", sz), setattr(self.dz_border, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8)))
        )
        self.drop_zone_btn.bind(on_release=self.open_audio_file_dialog)
        input_card.add_widget(self.drop_zone_btn)

        # File Input Row
        file_row = BoxLayout(orientation="horizontal", spacing=6, size_hint_y=None, height=32)
        file_lbl = Label(
            text="Audio File:",
            size_hint_x=None,
            width=68,
            color=get_color_from_hex("#E5E7EB"),
            font_size="12sp",
            halign="left",
            valign="middle"
        )
        file_lbl.bind(size=file_lbl.setter("text_size"))
        
        self.file_input = TextInput(
            hint_text="Browse audio/video (MP3, M4A, WAV, MP4...) or upload existing transcript...",
            multiline=False,
            font_size="12sp",
            background_color=get_color_from_hex("#121316"),
            foreground_color=get_color_from_hex("#F9FAFB"),
            cursor_color=get_color_from_hex("#3B82F6"),
            padding=[8, 6, 8, 6]
        )
        self.browse_audio_btn = Button(
            text="Browse Audio",
            size_hint_x=None,
            width=115,
            font_size="12sp",
            background_normal="",
            background_color=get_color_from_hex("#27272A"),
            color=get_color_from_hex("#FFFFFF"),
            bold=True
        )
        self.browse_audio_btn.bind(on_release=self.open_audio_file_dialog)

        self.upload_doc_btn = Button(
            text="Upload Transcript",
            size_hint_x=None,
            width=135,
            font_size="12sp",
            background_normal="",
            background_color=get_color_from_hex("#27272A"),
            color=get_color_from_hex("#E5E7EB")
        )
        self.upload_doc_btn.bind(on_release=self.open_existing_transcript_dialog)

        self.record_meeting_btn = Button(
            text="Record Meeting",
            size_hint_x=None,
            width=135,
            font_size="12sp",
            background_normal="",
            background_color=get_color_from_hex("#3B82F6"),
            color=get_color_from_hex("#FFFFFF"),
            bold=True
        )
        self.record_meeting_btn.bind(on_release=self.toggle_record_meeting)

        file_row.add_widget(file_lbl)
        file_row.add_widget(self.file_input)
        file_row.add_widget(self.browse_audio_btn)
        file_row.add_widget(self.upload_doc_btn)
        file_row.add_widget(self.record_meeting_btn)
        input_card.add_widget(file_row)
        self.transcribe_view.add_widget(input_card)

        # B. Middle Grid: 3 Elevated Surface Cards (Capture Settings, Processing Model, Voice Refs)
        middle_grid = BoxLayout(orientation="horizontal", spacing=8, size_hint_y=None, height=165)

        # Card 1: Capture Settings
        card_capture = SurfaceCard(orientation="vertical", size_hint_x=0.33, padding=[10, 8, 10, 8], spacing=5)
        cap_title = Label(
            text="Capture Settings",
            font_size="12sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            size_hint_y=None,
            height=16,
            halign="left",
            valign="middle"
        )
        cap_title.bind(size=cap_title.setter("text_size"))
        card_capture.add_widget(cap_title)

        self.input_device_spinner = Spinner(
            text="Scanning mics...",
            values=[],
            size_hint_y=None,
            height=28,
            font_size="11sp",
            background_color=get_color_from_hex("#121316"),
            color=get_color_from_hex("#F9FAFB")
        )
        self.input_device_spinner.bind(text=self._on_input_device_selected)
        card_capture.add_widget(self.input_device_spinner)

        gain_row = BoxLayout(orientation="horizontal", spacing=4, size_hint_y=None, height=26)
        gain_lbl = Label(text="Gain:", size_hint_x=None, width=34, color=get_color_from_hex("#9CA3AF"), font_size="11sp", halign="left", valign="middle")
        gain_lbl.bind(size=gain_lbl.setter("text_size"))
        
        self.gain_slider = Slider(min=1.0, max=8.0, value=2.0, step=0.5, size_hint_x=0.42)
        self.gain_slider.bind(value=self._on_gain_slider_changed)

        self.gain_spinner = Spinner(
            text="2.0x (Default)",
            values=["1.0x (Raw)", "1.5x", "2.0x (Default)", "3.0x (Boost)", "5.0x High", "8.0x (Max)"],
            size_hint_x=None,
            width=92,
            font_size="10sp",
            background_color=get_color_from_hex("#121316"),
            color=get_color_from_hex("#F9FAFB")
        )
        self.gain_spinner.bind(text=self._on_gain_setting_changed)
        
        gain_row.add_widget(gain_lbl)
        gain_row.add_widget(self.gain_slider)
        gain_row.add_widget(self.gain_spinner)
        card_capture.add_widget(gain_row)

        vu_row = BoxLayout(orientation="horizontal", spacing=6, size_hint_y=None, height=36)
        vu_lbl = Label(text="VU meter:", size_hint_x=None, width=58, color=get_color_from_hex("#9CA3AF"), font_size="11sp", halign="left", valign="middle")
        vu_lbl.bind(size=vu_lbl.setter("text_size"))
        self.vu_meter_widget = GraphicalVUMeter(size_hint_x=1, height=34)
        vu_row.add_widget(vu_lbl)
        vu_row.add_widget(self.vu_meter_widget)
        card_capture.add_widget(vu_row)
        middle_grid.add_widget(card_capture)

        # Card 2: Processing Model
        card_model = SurfaceCard(orientation="vertical", size_hint_x=0.34, padding=[10, 8, 10, 8], spacing=5)
        model_hdr_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=16)
        mod_title = Label(
            text="Processing Model",
            font_size="12sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            halign="left",
            valign="middle"
        )
        mod_title.bind(size=mod_title.setter("text_size"))
        self.daily_usage_lbl = Label(
            text=f"Requests today: [{self.daily_usage_count}]",
            font_size="11sp",
            bold=True,
            color=get_color_from_hex("#10B981"),
            size_hint_x=None,
            width=135,
            halign="right",
            valign="middle"
        )
        self.daily_usage_lbl.bind(size=self.daily_usage_lbl.setter("text_size"))
        model_hdr_row.add_widget(mod_title)
        model_hdr_row.add_widget(self.daily_usage_lbl)
        card_model.add_widget(model_hdr_row)

        self.model_spinner = Spinner(
            text=f"Model: {self.settings.get('default_model', DEFAULT_MODEL)}",
            values=[f"Model: {m}" for m in AVAILABLE_MODELS],
            size_hint_y=None,
            height=28,
            font_size="11sp",
            background_color=get_color_from_hex("#121316"),
            color=get_color_from_hex("#F9FAFB"),
            bold=True
        )
        self.model_spinner.bind(text=self._on_model_spinner_changed)
        card_model.add_widget(self.model_spinner)

        # Chunking Options
        chunk_row = BoxLayout(orientation="horizontal", spacing=4, size_hint_y=None, height=24)
        is_auto_mode = (self.settings.get("default_chunk_mode", "auto") == "auto")
        self.chk_auto = CheckBox(group="chunk_mode", active=is_auto_mode, size_hint_x=None, width=20)
        self.chk_auto.bind(active=self.on_chunk_mode_change)
        auto_lbl = Label(text="Auto", color=get_color_from_hex("#E5E7EB"), font_size="11sp", size_hint_x=None, width=40, halign="left", valign="middle")
        auto_lbl.bind(size=auto_lbl.setter("text_size"))

        self.chk_manual = CheckBox(group="chunk_mode", active=(not is_auto_mode), size_hint_x=None, width=20)
        self.chk_manual.bind(active=self.on_chunk_mode_change)
        manual_lbl = Label(text="Manual:", color=get_color_from_hex("#E5E7EB"), font_size="11sp", size_hint_x=None, width=48, halign="left", valign="middle")
        manual_lbl.bind(size=manual_lbl.setter("text_size"))

        self.chunk_input = TextInput(
            text=str(self.settings.get("default_chunk_minutes", "30")),
            multiline=False,
            size_hint_x=None,
            width=42,
            disabled=is_auto_mode,
            input_filter="float",
            font_size="11sp",
            background_color=get_color_from_hex("#27272A") if is_auto_mode else get_color_from_hex("#121316"),
            foreground_color=get_color_from_hex("#71717A") if is_auto_mode else get_color_from_hex("#F9FAFB"),
            cursor_color=get_color_from_hex("#3B82F6"),
            padding=[4, 3, 4, 3]
        )
        chunk_row.add_widget(self.chk_auto)
        chunk_row.add_widget(auto_lbl)
        chunk_row.add_widget(self.chk_manual)
        chunk_row.add_widget(manual_lbl)
        chunk_row.add_widget(self.chunk_input)
        card_model.add_widget(chunk_row)

        # Checkboxes and Live Status
        opts_row = BoxLayout(orientation="horizontal", spacing=4, size_hint_y=None, height=22)
        self.auto_rename_chk = CheckBox(active=False, size_hint_x=None, width=18)
        auto_ren_lbl = Label(text="Auto Rename", color=get_color_from_hex("#D1D5DB"), font_size="10sp", size_hint_x=None, width=78, halign="left", valign="middle")
        auto_ren_lbl.bind(size=auto_ren_lbl.setter("text_size"))

        self.drive_chk = CheckBox(active=False, size_hint_x=None, width=18)
        drive_lbl = Label(text="Auto Drive Upload", color=get_color_from_hex("#D1D5DB"), font_size="10sp", size_hint_x=None, width=105, halign="left", valign="middle")
        drive_lbl.bind(size=drive_lbl.setter("text_size"))

        opts_row.add_widget(self.auto_rename_chk)
        opts_row.add_widget(auto_ren_lbl)
        opts_row.add_widget(self.drive_chk)
        opts_row.add_widget(drive_lbl)
        card_model.add_widget(opts_row)

        status_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=18)
        stat_prefix = Label(text="Status: ", font_size="11sp", color=get_color_from_hex("#F9FAFB"), size_hint_x=None, width=45, halign="left", valign="middle")
        stat_prefix.bind(size=stat_prefix.setter("text_size"))
        self.live_action_lbl = Label(text="Ready", font_size="11sp", bold=True, color=get_color_from_hex("#10B981"), halign="left", valign="middle")
        self.live_action_lbl.bind(size=self.live_action_lbl.setter("text_size"))
        status_row.add_widget(stat_prefix)
        status_row.add_widget(self.live_action_lbl)
        card_model.add_widget(status_row)
        middle_grid.add_widget(card_model)

        # Card 3: Voice Refs
        card_voice = SurfaceCard(orientation="vertical", size_hint_x=0.33, padding=[10, 8, 10, 8], spacing=4)
        voice_hdr = BoxLayout(orientation="horizontal", size_hint_y=None, height=16)
        v_title = Label(
            text="Voice Refs",
            font_size="12sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            halign="left",
            valign="middle"
        )
        v_title.bind(size=v_title.setter("text_size"))
        manage_btn = Button(
            text="Manage",
            size_hint_x=None,
            width=70,
            font_size="11sp",
            background_normal="",
            background_color=(0, 0, 0, 0),
            color=get_color_from_hex("#38BDF8")
        )
        manage_btn.bind(on_release=lambda x: self.switch_to_samples_tab())
        voice_hdr.add_widget(v_title)
        voice_hdr.add_widget(manage_btn)
        card_voice.add_widget(voice_hdr)

        self.voice_chips_scroll = ScrollView(size_hint=(1, 1), do_scroll_x=False, do_scroll_y=True)
        self.voice_chips_grid = GridLayout(cols=2, spacing=4, size_hint_y=None)
        self.voice_chips_grid.bind(minimum_height=self.voice_chips_grid.setter("height"))
        self.voice_chips_scroll.add_widget(self.voice_chips_grid)
        card_voice.add_widget(self.voice_chips_scroll)
        middle_grid.add_widget(card_voice)

        self.transcribe_view.add_widget(middle_grid)

        # C. Primary Start Transcription Banner
        self.start_btn = Button(
            text="Start Transcription",
            bold=True,
            font_size="14sp",
            background_normal="",
            background_color=get_color_from_hex("#3B82F6"),
            color=get_color_from_hex("#FFFFFF"),
            size_hint_y=None,
            height=40
        )
        with self.start_btn.canvas.before:
            self.start_bg_color = Color(*get_color_from_hex("#3B82F6"))
            self.start_btn_rect = RoundedRectangle(pos=self.start_btn.pos, size=self.start_btn.size, radius=[8])
        self.start_btn.bind(
            pos=lambda s, p: setattr(self.start_btn_rect, "pos", p),
            size=lambda s, sz: setattr(self.start_btn_rect, "size", sz)
        )
        self.start_btn.bind(on_release=self.start_transcription)
        self.transcribe_view.add_widget(self.start_btn)

        # Progress Bar (Micro hairline)
        self.progress_bar = ProgressBar(max=1.0, value=0.0, size_hint_y=None, height=4)
        self.transcribe_view.add_widget(self.progress_bar)

        # D. Waveform Studio Card with Floating Micro-Toolbar
        self.main_waveform_card = SurfaceCard(orientation="vertical", size_hint_y=None, height=140, padding=[10, 8, 10, 8], spacing=4)
        
        wf_header = BoxLayout(orientation="horizontal", size_hint_y=None, height=16, spacing=8)
        self.main_wf_title_lbl = Label(
            text="Waveform Studio",
            font_size="12sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            halign="left",
            valign="middle"
        )
        self.main_wf_title_lbl.bind(size=self.main_wf_title_lbl.setter("text_size"))

        self.main_wf_sel_lbl = Label(
            text="Click & drag across waveform to clip a voice sample (< 8.0s)",
            font_size="11sp",
            color=get_color_from_hex("#9CA3AF"),
            halign="right",
            valign="middle"
        )
        self.main_wf_sel_lbl.bind(size=self.main_wf_sel_lbl.setter("text_size"))

        wf_header.add_widget(self.main_wf_title_lbl)
        wf_header.add_widget(self.main_wf_sel_lbl)
        self.main_waveform_card.add_widget(wf_header)

        # Waveform Container with Centered Floating Island
        wf_container = RelativeLayout(size_hint=(1, 1))
        self.main_waveform_widget = WaveformAudioWidget(size_hint=(1, 1), pos_hint={'x': 0, 'y': 0})
        wf_container.add_widget(self.main_waveform_widget)

        # Floating micro-toolbar pill matching media_1788025566463.jpg
        self.floating_pill = BoxLayout(
            orientation="horizontal",
            size_hint=(None, None),
            size=(250, 30),
            pos_hint={'center_x': 0.5, 'center_y': 0.28},
            spacing=4,
            padding=[6, 2, 6, 2]
        )
        with self.floating_pill.canvas.before:
            Color(0.12, 0.13, 0.16, 0.92)
            self.fp_bg = RoundedRectangle(pos=self.floating_pill.pos, size=self.floating_pill.size, radius=[15])
            Color(*get_color_from_hex("#2B2D35"))
            self.fp_border = Line(rounded_rectangle=(self.floating_pill.x, self.floating_pill.y, self.floating_pill.width, self.floating_pill.height, 15), width=1.0)
        self.floating_pill.bind(
            pos=lambda s, p: (setattr(self.fp_bg, "pos", p), setattr(self.fp_border, "rounded_rectangle", (s.x, s.y, s.width, s.height, 15))),
            size=lambda s, sz: (setattr(self.fp_bg, "size", sz), setattr(self.fp_border, "rounded_rectangle", (s.x, s.y, s.width, s.height, 15)))
        )

        self.main_play_btn = Button(
            text="> Play",
            size_hint_x=0.33,
            font_size="11sp",
            background_normal="",
            background_color=(0, 0, 0, 0),
            color=get_color_from_hex("#FFFFFF"),
            bold=True
        )
        self.main_play_btn.bind(on_release=lambda x: self.main_waveform_widget.toggle_play(self.main_play_btn))

        self.enhance_audio_btn = Button(
            text="Enhance",
            size_hint_x=0.36,
            font_size="11sp",
            background_normal="",
            background_color=(0, 0, 0, 0),
            color=get_color_from_hex("#FFFFFF"),
            disabled=True,
            opacity=0.4,
            bold=True
        )
        self.enhance_audio_btn.bind(on_release=self.enhance_current_audio_file)

        self.main_save_region_btn = Button(
            text="Save",
            size_hint_x=0.31,
            font_size="11sp",
            background_normal="",
            background_color=(0, 0, 0, 0),
            color=get_color_from_hex("#FFFFFF"),
            disabled=True,
            opacity=0.4,
            bold=True
        )
        self.main_save_region_btn.bind(on_release=self._on_save_selected_region_from_main)

        self.floating_pill.add_widget(self.main_play_btn)
        self.floating_pill.add_widget(self.enhance_audio_btn)
        self.floating_pill.add_widget(self.main_save_region_btn)
        wf_container.add_widget(self.floating_pill)

        self.main_waveform_card.add_widget(wf_container)
        self.transcribe_view.add_widget(self.main_waveform_card)

        # E. Live Diarized Transcript Display (GDI Texture Stream)
        self.display_scroll = ScrollView(
            size_hint=(1, 1),
            do_scroll_x=False,
            do_scroll_y=True,
            bar_width=8,
            scroll_type=["bars", "content"]
        )
        self.transcript_display_layout = BoxLayout(
            orientation="vertical",
            spacing=4,
            size_hint_y=None,
            size_hint_x=1
        )
        self.transcript_display_layout.bind(minimum_height=self.transcript_display_layout.setter("height"))
        self.display_scroll.bind(width=lambda s, w: setattr(self.transcript_display_layout, "width", w))
        self.display_scroll.add_widget(self.transcript_display_layout)
        self.transcribe_view.add_widget(self.display_scroll)

        self.display_placeholder_message("=== Welcome to Bilingual Meeting Transcriber ===\nSelect an audio or video file above, or load an existing transcript to view here.")

        # F. Action Dock (Fixed Bottom Anchor)
        self.actions_bar = SurfaceCard(orientation="horizontal", size_hint_y=None, height=44, padding=[8, 6, 8, 6], spacing=8)

        self.preview_transcript_btn = Button(
            text="Preview Transcript",
            bold=True,
            background_normal="",
            background_color=get_color_from_hex("#27272A"),
            color=get_color_from_hex("#FFFFFF"),
            disabled=False,
            opacity=0.7,
            size_hint_x=0.20,
            font_size="11sp"
        )
        self.preview_transcript_btn.bind(on_release=self.open_transcript_preview_modal)

        self.preview_notes_btn = Button(
            text="Preview Notes",
            bold=True,
            background_normal="",
            background_color=get_color_from_hex("#27272A"),
            color=get_color_from_hex("#FFFFFF"),
            disabled=False,
            opacity=0.7,
            size_hint_x=0.18,
            font_size="11sp"
        )
        self.preview_notes_btn.bind(on_release=self.open_notes_preview_dialog)

        self.notes_btn = Button(
            text="Generate Notes",
            bold=True,
            background_normal="",
            background_color=get_color_from_hex("#27272A"),
            color=get_color_from_hex("#FFFFFF"),
            disabled=False,
            opacity=0.7,
            size_hint_x=0.20,
            font_size="11sp"
        )
        self.notes_btn.bind(on_release=self.on_generate_notes_clicked)

        self.save_transcript_btn = Button(
            text="Save Transcript (.docx)",
            bold=True,
            background_normal="",
            background_color=get_color_from_hex("#27272A"),
            color=get_color_from_hex("#FFFFFF"),
            disabled=False,
            opacity=0.7,
            size_hint_x=0.22,
            font_size="11sp"
        )
        self.save_transcript_btn.bind(on_release=lambda x: self.save_as_dialog("transcript"))

        self.save_notes_btn = Button(
            text="Save Notes (.docx)",
            bold=True,
            background_normal="",
            background_color=get_color_from_hex("#27272A"),
            color=get_color_from_hex("#FFFFFF"),
            disabled=False,
            opacity=0.7,
            size_hint_x=0.20,
            font_size="11sp"
        )
        self.save_notes_btn.bind(on_release=lambda x: self.save_as_dialog("notes"))

        self.actions_bar.add_widget(self.preview_transcript_btn)
        self.actions_bar.add_widget(self.preview_notes_btn)
        self.actions_bar.add_widget(self.notes_btn)
        self.actions_bar.add_widget(self.save_transcript_btn)
        self.actions_bar.add_widget(self.save_notes_btn)
        self.transcribe_view.add_widget(self.actions_bar)
        self._enable_post_processing_buttons()

    def _on_main_waveform_selection(self, s1, s2):
        dur = s2 - s1
        if dur > 0.1:
            capped_dur = min(dur, 8.0)
            cap_note = " (8.0s cap)" if dur > 8.0 else ""
            self.main_wf_sel_lbl.text = f"Selected: {format_timestamp(s1)} - {format_timestamp(s2)} ({capped_dur:.1f}s{cap_note})"
            self.main_wf_sel_lbl.color = get_color_from_hex("#10B981")
            self.main_save_region_btn.disabled = False
            self.main_save_region_btn.opacity = 1.0
        else:
            self.main_wf_sel_lbl.text = "Click & drag across waveform to clip a voice sample (< 8.0s)"
            self.main_wf_sel_lbl.color = get_color_from_hex("#38BDF8")
            self.main_save_region_btn.disabled = True
            self.main_save_region_btn.opacity = 0.4

    def _on_save_selected_region_from_main(self, instance):
        if not self.source_audio_path or not Path(self.source_audio_path).exists():
            return
        s1 = min(self.main_waveform_widget.sel_start_sec, self.main_waveform_widget.sel_end_sec)
        s2 = max(self.main_waveform_widget.sel_start_sec, self.main_waveform_widget.sel_end_sec)
        if s2 - s1 < 0.2:
            return

        def _prompt_save():
            try:
                person_name, person_desc = prompt_voice_sample_metadata_dialog(
                    title="Save Voice Sample",
                    header_text=f"Save Voice Reference Clip [{format_timestamp(s1)} - {format_timestamp(s2)}]:"
                )
                if not person_name or not person_name.strip():
                    return

                person_name = person_name.strip()
                # Save with enforced 8.0s cap and description
                entry = save_voice_sample(person_name, self.source_audio_path, start_sec=s1, end_sec=s2, description=person_desc)
                display_label = get_sample_display_label(entry)
                print(f"[Waveform Clipper] Saved '{display_label}' ({entry['duration_sec']}s) -> {entry['path']}", flush=True)

                Clock.schedule_once(lambda dt: self.render_sample_library_list())
                Clock.schedule_once(lambda dt: self.render_transcribe_samples_bar())
                Clock.schedule_once(lambda dt: self.update_live_status(f"Saved voice sample: '{display_label}' ({entry['duration']})", 0, 1))

            except Exception as e:
                print(f"[Error saving region sample]: {e}", file=sys.stderr)

        threading.Thread(target=_prompt_save, daemon=True).start()

    # ==========================================================================
    # Voice Sample Library View (Dedicated Tab)
    # ==========================================================================

    def _build_sample_library_view(self):
        self.sample_library_view = BoxLayout(orientation="vertical", spacing=8)

        # Header bar matching media_1788025566455.jpg
        header_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=32, spacing=8)
        lib_title = Label(
            text="Voice Reference Sample Library",
            font_size="16sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            halign="left",
            valign="middle"
        )
        lib_title.bind(size=lib_title.setter("text_size"))

        plus_btn = Button(
            text="+",
            size_hint_x=None,
            width=36,
            font_size="14sp",
            bold=True,
            background_normal="",
            background_color=get_color_from_hex("#1E2025"),
            color=get_color_from_hex("#38BDF8")
        )
        with plus_btn.canvas.before:
            Color(*get_color_from_hex("#1E2025"))
            self.p_bg = RoundedRectangle(pos=plus_btn.pos, size=plus_btn.size, radius=[8])
            Color(*get_color_from_hex("#2B2D35"))
            self.p_line = Line(rounded_rectangle=(plus_btn.x, plus_btn.y, plus_btn.width, plus_btn.height, 8), width=1.0)
        plus_btn.bind(
            pos=lambda s, p: (setattr(self.p_bg, "pos", p), setattr(self.p_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8))),
            size=lambda s, sz: (setattr(self.p_bg, "size", sz), setattr(self.p_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8)))
        )
        plus_btn.bind(on_release=self.import_labeled_voice_sample)

        self.import_sample_btn = Button(
            text="+ Import Labeled Sample",
            size_hint_x=None,
            width=190,
            background_normal="",
            background_color=get_color_from_hex("#1E2025"),
            color=get_color_from_hex("#FFFFFF"),
            bold=True,
            font_size="11sp"
        )
        with self.import_sample_btn.canvas.before:
            Color(*get_color_from_hex("#1E2025"))
            self.imp_bg = RoundedRectangle(pos=self.import_sample_btn.pos, size=self.import_sample_btn.size, radius=[18])
            Color(*get_color_from_hex("#3B82F6"))
            self.imp_line = Line(rounded_rectangle=(self.import_sample_btn.x, self.import_sample_btn.y, self.import_sample_btn.width, self.import_sample_btn.height, 18), width=1.5)
        self.import_sample_btn.bind(
            pos=lambda s, p: (setattr(self.imp_bg, "pos", p), setattr(self.imp_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 18))),
            size=lambda s, sz: (setattr(self.imp_bg, "size", sz), setattr(self.imp_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 18)))
        )
        self.import_sample_btn.bind(on_release=self.import_labeled_voice_sample)

        refresh_btn = Button(
            text="Refresh",
            size_hint_x=None,
            width=95,
            background_normal="",
            background_color=get_color_from_hex("#1E2025"),
            color=get_color_from_hex("#FFFFFF"),
            font_size="11sp"
        )
        with refresh_btn.canvas.before:
            Color(*get_color_from_hex("#1E2025"))
            self.rf_bg = RoundedRectangle(pos=refresh_btn.pos, size=refresh_btn.size, radius=[8])
            Color(*get_color_from_hex("#2B2D35"))
            self.rf_line = Line(rounded_rectangle=(refresh_btn.x, refresh_btn.y, refresh_btn.width, refresh_btn.height, 8), width=1.0)
        refresh_btn.bind(
            pos=lambda s, p: (setattr(self.rf_bg, "pos", p), setattr(self.rf_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8))),
            size=lambda s, sz: (setattr(self.rf_bg, "size", sz), setattr(self.rf_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8)))
        )
        refresh_btn.bind(on_release=lambda x: self.render_sample_library_list())

        header_row.add_widget(lib_title)
        header_row.add_widget(plus_btn)
        header_row.add_widget(self.import_sample_btn)
        header_row.add_widget(refresh_btn)
        self.sample_library_view.add_widget(header_row)

        sub_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=20)
        sub_info = Label(
            text="Checked samples are automatically attached as reference voice clips in every chunk of your next transcription run.",
            font_size="11sp",
            color=get_color_from_hex("#9CA3AF"),
            halign="left",
            valign="middle"
        )
        sub_info.bind(size=sub_info.setter("text_size"))
        sort_lbl = Label(text="Sort by: [Added Date v]", font_size="11sp", color=get_color_from_hex("#9CA3AF"), size_hint_x=None, width=140, halign="right", valign="middle")
        sort_lbl.bind(size=sort_lbl.setter("text_size"))
        sub_row.add_widget(sub_info)
        sub_row.add_widget(sort_lbl)
        self.sample_library_view.add_widget(sub_row)

        # Scrollable list container
        self.samples_scroll = ScrollView(size_hint=(1, 1))
        self.samples_list_layout = BoxLayout(orientation="vertical", spacing=8, size_hint_y=None)
        self.samples_list_layout.bind(minimum_height=self.samples_list_layout.setter("height"))
        self.samples_scroll.add_widget(self.samples_list_layout)
        self.sample_library_view.add_widget(self.samples_scroll)

        # Bottom status footer
        self.samples_footer = BoxLayout(orientation="horizontal", size_hint_y=None, height=22)
        self.samples_count_lbl = Label(text="Samples Indexed: 0", font_size="11sp", color=get_color_from_hex("#9CA3AF"), halign="left", valign="middle")
        self.samples_count_lbl.bind(size=self.samples_count_lbl.setter("text_size"))
        samples_ready_lbl = Label(text="Ready", font_size="11sp", color=get_color_from_hex("#10B981"), halign="right", valign="middle")
        samples_ready_lbl.bind(size=samples_ready_lbl.setter("text_size"))
        self.samples_footer.add_widget(self.samples_count_lbl)
        self.samples_footer.add_widget(samples_ready_lbl)
        self.sample_library_view.add_widget(self.samples_footer)

    def render_transcribe_samples_bar(self):
        """
        Renders the active voice samples as selectable chips in the Voice Refs card.
        """
        if not hasattr(self, "voice_chips_grid"):
            return
        self.voice_chips_grid.clear_widgets()
        samples = load_voice_samples()
        
        # Display samples from library, or fallback demo presets if empty
        if not samples:
            samples = [
                {"id": "demo1", "name": "Falgoon", "description": "LE", "include_in_transcription": True},
                {"id": "demo2", "name": "Meshkat", "description": "Eng", "include_in_transcription": True},
                {"id": "demo3", "name": "Taj", "description": "", "include_in_transcription": False},
                {"id": "demo4", "name": "Imtiaj", "description": "", "include_in_transcription": False},
                {"id": "demo5", "name": "Meshkat", "description": "Eng 2", "include_in_transcription": False},
                {"id": "demo6", "name": "Candidate 1", "description": "", "include_in_transcription": False},
            ]

        for sample in samples:
            chip = VoiceChipButton(
                sample_dict=sample,
                on_toggle_callback=lambda s, active: self.render_sample_library_list()
            )
            self.voice_chips_grid.add_widget(chip)

    def render_sample_library_list(self):
        self.samples_list_layout.clear_widgets()
        samples = load_voice_samples()
        self.tab_btn_samples.text = f"Sample Library ({len(samples)})"
        if hasattr(self, "samples_count_lbl"):
            self.samples_count_lbl.text = f"Samples Indexed: {len(samples)}"

        if not samples:
            empty_lbl = Label(
                text="No voice reference samples in the library yet.\nClick '+ Import Labeled Sample' or cut a speaker turn from the transcript waveform preview!",
                color=get_color_from_hex("#71717A"),
                size_hint_y=None,
                height=120,
                halign="center",
                valign="middle"
            )
            self.samples_list_layout.add_widget(empty_lbl)
            return

        for sample in samples:
            card = SurfaceCard(orientation="horizontal", size_hint_y=None, height=72, padding=[10, 8, 10, 8], spacing=10)

            sample_id = sample.get("id")
            display_label = get_sample_display_label(sample)
            person_name = sample.get("name", "Voice")
            initial = person_name[0] if person_name else "V"

            # 1. Checkbox for Include in Next Transcription
            chk_box = BoxLayout(orientation="horizontal", size_hint_x=None, width=28)
            chk = CheckBox(active=bool(sample.get("include_in_transcription", True)), size_hint_x=None, width=26)
            
            def _on_lib_chk_toggle(inst, val, sid=sample_id):
                update_voice_sample_include(sid, val)
                self.render_transcribe_samples_bar()
                
            chk.bind(active=_on_lib_chk_toggle)
            chk_box.add_widget(chk)
            card.add_widget(chk_box)

            # 2. Avatar Circle
            avatar = AvatarCircle(text=initial, bg_hex="#27272A", border_hex="#3B82F6", size_val=36)
            card.add_widget(avatar)

            # 3. Info Column (Name + Description, Duration, Filename, Date)
            info_col = BoxLayout(orientation="vertical", size_hint_x=0.55, spacing=2)
            
            name_lbl = Label(
                text=display_label,
                font_size="13sp",
                bold=True,
                color=get_color_from_hex("#F9FAFB"),
                halign="left",
                valign="middle",
                shorten=True,
                shorten_from="right"
            )
            name_lbl.bind(size=name_lbl.setter("text_size"))

            meta_row = BoxLayout(orientation="horizontal", spacing=8, size_hint_y=None, height=18)
            file_meta = Label(
                text=f"{sample.get('filename', '')}",
                font_size="10sp",
                color=get_color_from_hex("#9CA3AF"),
                size_hint_x=None,
                width=160,
                halign="left",
                valign="middle",
                shorten=True
            )
            file_meta.bind(size=file_meta.setter("text_size"))

            added_meta = Label(
                text=f"Date: Added: {sample.get('date_added', '')}",
                font_size="10sp",
                color=get_color_from_hex("#9CA3AF"),
                size_hint_x=None,
                width=180,
                halign="left",
                valign="middle"
            )
            added_meta.bind(size=added_meta.setter("text_size"))

            dur_badge = Label(
                text=f"{sample.get('duration', '00:08')}",
                font_size="10sp",
                bold=True,
                color=get_color_from_hex("#10B981"),
                size_hint_x=None,
                width=60,
                halign="left",
                valign="middle"
            )
            dur_badge.bind(size=dur_badge.setter("text_size"))

            meta_row.add_widget(file_meta)
            meta_row.add_widget(added_meta)
            meta_row.add_widget(dur_badge)

            info_col.add_widget(name_lbl)
            info_col.add_widget(meta_row)
            card.add_widget(info_col)

            # 4. Actions Column: Play, Edit, Delete
            target_path = Path(sample.get("path", ""))

            play_btn = Button(
                text="> Play Sample",
                size_hint_x=None,
                width=100,
                font_size="11sp",
                background_normal="",
                background_color=get_color_from_hex("#1E2025"),
                color=get_color_from_hex("#38BDF8")
            )
            with play_btn.canvas.before:
                Color(*get_color_from_hex("#1E2025"))
                play_btn.pb_rect = RoundedRectangle(pos=play_btn.pos, size=play_btn.size, radius=[8])
                Color(*get_color_from_hex("#2B2D35"))
                play_btn.pb_line = Line(rounded_rectangle=(play_btn.x, play_btn.y, play_btn.width, play_btn.height, 8), width=1.0)
            play_btn.bind(
                pos=lambda s, p: (setattr(s.pb_rect, "pos", p), setattr(s.pb_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8))),
                size=lambda s, sz: (setattr(s.pb_rect, "size", sz), setattr(s.pb_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8)))
            )
            play_btn.bind(on_release=lambda x, p=target_path, b=play_btn: self._play_sample_audio(p, b))

            edit_btn = Button(
                text="Edit",
                size_hint_x=None,
                width=60,
                font_size="11sp",
                background_normal="",
                background_color=(0, 0, 0, 0),
                color=get_color_from_hex("#3B82F6"),
                bold=True
            )
            edit_btn.bind(on_release=lambda x, s=sample: self._open_edit_sample_dialog(s))

            delete_btn = Button(
                text="Delete",
                size_hint_x=None,
                width=65,
                font_size="11sp",
                background_normal="",
                background_color=(0, 0, 0, 0),
                color=get_color_from_hex("#EF4444")
            )
            delete_btn.bind(on_release=lambda x, sid=sample_id, name=sample.get("name"): self._confirm_delete_sample(sid, name))

            card.add_widget(play_btn)
            card.add_widget(edit_btn)
            card.add_widget(delete_btn)
            self.samples_list_layout.add_widget(card)

    def _open_edit_sample_dialog(self, sample: dict):
        """
        Opens a modal dialog pre-filled with the current name (editable) and description field.
        On save, updates that specific sample's entry in samples.json by its unique ID.
        """
        sample_id = sample.get("id")
        current_name = sample.get("name", "")
        current_desc = sample.get("description", "")
        current_fn = sample.get("filename", "")

        def _run_edit():
            name, desc = prompt_voice_sample_metadata_dialog(
                title="Edit Voice Sample",
                header_text=f"Editing Voice Sample ({current_fn}):",
                default_name=current_name,
                default_desc=current_desc
            )
            if not name or not name.strip():
                return

            # Update sample in samples.json by unique ID
            success = update_voice_sample_metadata(sample_id, name.strip(), desc.strip())
            if success:
                display = f"{name.strip()} - {desc.strip()}" if desc.strip() else name.strip()
                print(f"[Voice Sample Library] Updated sample '{sample_id}': {display}", flush=True)
                Clock.schedule_once(lambda dt: self.render_sample_library_list())
                Clock.schedule_once(lambda dt: self.render_transcribe_samples_bar())
                Clock.schedule_once(lambda dt: self.update_live_status(f"Updated sample: '{display}'", 0, 1))

        threading.Thread(target=_run_edit, daemon=True).start()

    def stop_all_sample_playback(self):
        """
        Immediately halts any currently playing voice sample in the library,
        closes file handles, cancels clock callbacks, and resets UI button states.
        """
        if self.active_sample_clock:
            self.active_sample_clock.cancel()
            self.active_sample_clock = None

        if self.active_sample_button:
            try:
                self.active_sample_button.text = "> Play Sample"
                self.active_sample_button.color = get_color_from_hex("#38BDF8")
            except Exception:
                pass
            self.active_sample_button = None

        self.sample_player.stop()
        self.sample_player.close()
        self.active_sample_path = None

    def _play_sample_audio(self, file_path: Path, button: Button):
        """
        Plays the selected voice sample.
        - 'Stop' halts playback immediately and unloads audio.
        - Starting playback stops whatever was currently playing (no overlapping, 1 at a time).
        """
        p_str = str(file_path.resolve()) if file_path else ""

        # 1. If clicking Stop on the active playing sample
        if button.text.startswith("Stop") or (self.active_sample_path and self.active_sample_path == p_str):
            self.stop_all_sample_playback()
            return

        # 2. Starting playback on a different (or new) sample: Halt previous playback first!
        self.stop_all_sample_playback()

        if not file_path.exists():
            return

        try:
            if self.sample_player.open_file(file_path):
                self.active_sample_path = p_str
                self.active_sample_button = button
                button.text = "Stop"
                button.color = get_color_from_hex("#F59E0B")
                self.sample_player.play()

                def _check_playback_done(dt):
                    if not self.sample_player.is_open or self.sample_player.get_mode() == "stopped":
                        self.stop_all_sample_playback()
                        return False
                    return True

                self.active_sample_clock = Clock.schedule_interval(_check_playback_done, 0.1)
        except Exception as e:
            print(f"[Error playing sample]: {e}", file=sys.stderr)
            self.stop_all_sample_playback()

    def _confirm_delete_sample(self, sample_id: str, person_name: str):
        """
        Deletes a voice sample from the library and disk.
        CRITICAL: Always halts audio playback before deleting to avoid orphaned audio or file locks.
        """
        self.stop_all_sample_playback()

        def _ask_del():
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)

                confirm = messagebox.askyesno(
                    "Delete Voice Sample",
                    f"Remove voice reference sample for '{person_name}' from the library and disk?",
                    parent=root
                )
                root.destroy()

                if confirm:
                    Clock.schedule_once(lambda dt: self.stop_all_sample_playback())
                    delete_voice_sample(sample_id)
                    Clock.schedule_once(lambda dt: self.render_sample_library_list())
                    Clock.schedule_once(lambda dt: self.render_transcribe_samples_bar())
                    Clock.schedule_once(lambda dt: self.update_live_status(f"Deleted voice sample for '{person_name}'", 0, 1))

            except Exception as e:
                print(f"[Error deleting sample]: {e}", file=sys.stderr)

        threading.Thread(target=_ask_del, daemon=True).start()

    def import_labeled_voice_sample(self, instance=None):
        """
        File picker -> Prompts for person's name & description -> Trims to max 8.0s via ffmpeg -> Saves into voice_samples/
        """
        def _prompt_import():
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)

                file_path = filedialog.askopenfilename(
                    title="Select Reference Voice Audio Clip",
                    filetypes=[
                        ("Audio Files", "*.mp3 *.m4a *.wav *.aac *.ogg *.flac *.wma *.m4r"),
                        ("All Files", "*.*")
                    ]
                )
                root.destroy()
                if not file_path:
                    return

                default_name = Path(file_path).stem.replace("_", " ").replace("-", " ")
                person_name, person_desc = prompt_voice_sample_metadata_dialog(
                    title="Import Voice Sample",
                    header_text=f"Importing Reference Voice Clip:\n{Path(file_path).name}",
                    default_name=default_name
                )

                if not person_name or not person_name.strip():
                    return

                person_name = person_name.strip()
                # Save with enforced 8.0s length cap and description
                entry = save_voice_sample(person_name, file_path, start_sec=0.0, end_sec=8.0, description=person_desc)
                display_label = get_sample_display_label(entry)
                print(f"[Voice Sample Library] Imported '{display_label}' ({entry['duration_sec']}s) -> {entry['path']}", flush=True)

                Clock.schedule_once(lambda dt: self.render_sample_library_list())
                Clock.schedule_once(lambda dt: self.render_transcribe_samples_bar())
                Clock.schedule_once(lambda dt: self.update_live_status(f"Imported voice sample: '{display_label}' ({entry['duration']})", 0, 1))

            except Exception as e:
                print(f"\n[ERROR in import_labeled_voice_sample]: {e}\n{traceback.format_exc()}", file=sys.stderr)

        threading.Thread(target=_prompt_import, daemon=True).start()

    def get_active_reference_samples(self) -> list[dict]:
        all_samples = load_voice_samples()
        return [s for s in all_samples if s.get("include_in_transcription", True)]

    # ==========================================================================
    # History View
    # ==========================================================================

    def _build_history_view(self):
        self.history_view = BoxLayout(orientation="vertical", spacing=8)

        # Header matching media_1788025566459.jpg
        header_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=32, spacing=8)
        hist_title = Label(
            text="Saved Transcripts & Meeting Notes History",
            font_size="16sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            halign="left",
            valign="middle"
        )
        hist_title.bind(size=hist_title.setter("text_size"))
        
        refresh_btn = Button(
            text="Refresh History",
            size_hint_x=None,
            width=140,
            background_normal="",
            background_color=get_color_from_hex("#1E2025"),
            color=get_color_from_hex("#FFFFFF"),
            font_size="11sp"
        )
        with refresh_btn.canvas.before:
            Color(*get_color_from_hex("#1E2025"))
            self.hr_bg = RoundedRectangle(pos=refresh_btn.pos, size=refresh_btn.size, radius=[8])
            Color(*get_color_from_hex("#2B2D35"))
            self.hr_line = Line(rounded_rectangle=(refresh_btn.x, refresh_btn.y, refresh_btn.width, refresh_btn.height, 8), width=1.0)
        refresh_btn.bind(
            pos=lambda s, p: (setattr(self.hr_bg, "pos", p), setattr(self.hr_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8))),
            size=lambda s, sz: (setattr(self.hr_bg, "size", sz), setattr(self.hr_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8)))
        )
        refresh_btn.bind(on_release=lambda x: self.render_history_list())

        header_row.add_widget(hist_title)
        header_row.add_widget(refresh_btn)
        self.history_view.add_widget(header_row)

        sub_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=20)
        sort_left = Label(text="Sort by:  Date", font_size="11sp", color=get_color_from_hex("#9CA3AF"), size_hint_x=None, width=100, halign="left", valign="middle")
        sort_left.bind(size=sort_left.setter("text_size"))
        filler = Widget(size_hint_x=1)
        sort_right = Label(text="Sort by: [Date v]   [=] Filter", font_size="11sp", color=get_color_from_hex("#9CA3AF"), size_hint_x=None, width=170, halign="right", valign="middle")
        sort_right.bind(size=sort_right.setter("text_size"))
        sub_row.add_widget(sort_left)
        sub_row.add_widget(filler)
        sub_row.add_widget(sort_right)
        self.history_view.add_widget(sub_row)

        # Scrollable list container
        self.history_scroll = ScrollView(size_hint=(1, 1))
        self.history_list_layout = BoxLayout(orientation="vertical", spacing=8, size_hint_y=None)
        self.history_list_layout.bind(minimum_height=self.history_list_layout.setter("height"))
        self.history_scroll.add_widget(self.history_list_layout)
        self.history_view.add_widget(self.history_scroll)

        # Bottom status footer
        self.history_footer = BoxLayout(orientation="horizontal", size_hint_y=None, height=22)
        self.history_count_lbl = Label(text="Transcripts Indexed: 0", font_size="11sp", color=get_color_from_hex("#9CA3AF"), halign="left", valign="middle")
        self.history_count_lbl.bind(size=self.history_count_lbl.setter("text_size"))
        hist_ready_lbl = Label(text="Ready", font_size="11sp", color=get_color_from_hex("#10B981"), halign="right", valign="middle")
        hist_ready_lbl.bind(size=hist_ready_lbl.setter("text_size"))
        self.history_footer.add_widget(self.history_count_lbl)
        self.history_footer.add_widget(hist_ready_lbl)
        self.history_view.add_widget(self.history_footer)

    def switch_to_transcribe_tab(self):
        self.stop_all_sample_playback()
        self.content_area.clear_widgets()
        self.content_area.add_widget(self.transcribe_view)
        self.tab_btn_transcribe.set_active(True)
        self.tab_btn_samples.set_active(False)
        self.tab_btn_history.set_active(False)
        self.tab_btn_settings.set_active(False)
        self.render_transcribe_samples_bar()

    def switch_to_samples_tab(self):
        self.stop_all_sample_playback()
        self.content_area.clear_widgets()
        self.content_area.add_widget(self.sample_library_view)
        self.tab_btn_transcribe.set_active(False)
        self.tab_btn_samples.set_active(True)
        self.tab_btn_history.set_active(False)
        self.tab_btn_settings.set_active(False)
        self.render_sample_library_list()

    def switch_to_history_tab(self):
        self.stop_all_sample_playback()
        self.content_area.clear_widgets()
        self.content_area.add_widget(self.history_view)
        self.tab_btn_transcribe.set_active(False)
        self.tab_btn_samples.set_active(False)
        self.tab_btn_history.set_active(True)
        self.tab_btn_settings.set_active(False)
        self.render_history_list()

    def switch_to_settings_tab(self):
        self.stop_all_sample_playback()
        self.content_area.clear_widgets()
        self.content_area.add_widget(self.settings_view)
        self.tab_btn_transcribe.set_active(False)
        self.tab_btn_samples.set_active(False)
        self.tab_btn_history.set_active(False)
        self.tab_btn_settings.set_active(True)
        self.refresh_settings_view()

    # ==========================================================================
    # Settings View & Persisted Defaults
    # ==========================================================================

    def _build_settings_view(self):
        self.settings_view = BoxLayout(orientation="vertical", spacing=12, padding=[12, 10, 12, 10])

        # Header
        hdr = Label(
            text="Application Settings & Persisted Defaults",
            font_size="15sp",
            bold=True,
            color=get_color_from_hex("#F9FAFB"),
            size_hint_y=None,
            height=26,
            halign="left",
            valign="middle"
        )
        hdr.bind(size=hdr.setter("text_size"))
        self.settings_view.add_widget(hdr)

        # Settings scroll
        s_scroll = ScrollView(size_hint=(1, 1))
        s_layout = BoxLayout(orientation="vertical", spacing=12, size_hint_y=None)
        s_layout.bind(minimum_height=s_layout.setter("height"))

        
        # Card 0: Gemini API Key
        card_api = SurfaceCard(orientation="vertical", size_hint_y=None, height=72, spacing=6, padding=[12, 8, 12, 8])
        lbl_api = Label(text="Gemini API Key (Required for transcription):", font_size="12sp", bold=True, color=get_color_from_hex("#38BDF8"), size_hint_y=None, height=18, halign="left", valign="middle")
        lbl_api.bind(size=lbl_api.setter("text_size"))
        self.settings_api_input = TextInput(
            text=self.settings.get("gemini_api_key", ""),
            password=True,
            hint_text="Enter Gemini API Key (AIzaSy...)",
            multiline=False,
            font_size="12sp",
            background_color=get_color_from_hex("#121316"),
            foreground_color=get_color_from_hex("#F9FAFB"),
            padding=[6, 6, 6, 6],
            size_hint_y=None,
            height=32
        )
        self.settings_api_input.bind(text=self._on_settings_api_key_changed)
        card_api.add_widget(lbl_api)
        card_api.add_widget(self.settings_api_input)
        s_layout.add_widget(card_api)

        # Card 1: Default Model
        card_model = SurfaceCard(orientation="vertical", size_hint_y=None, height=72, spacing=6, padding=[12, 8, 12, 8])
        lbl1 = Label(text="Preferred Default Gemini Model:", font_size="12sp", bold=True, color=get_color_from_hex("#38BDF8"), size_hint_y=None, height=18, halign="left", valign="middle")
        lbl1.bind(size=lbl1.setter("text_size"))
        self.settings_model_spinner = Spinner(
            text=self.settings.get("default_model", DEFAULT_MODEL),
            values=AVAILABLE_MODELS,
            size_hint_y=None,
            height=32,
            font_size="12sp",
            background_color=get_color_from_hex("#121316"),
            color=get_color_from_hex("#F9FAFB")
        )
        self.settings_model_spinner.bind(text=self._on_settings_model_changed)
        card_model.add_widget(lbl1)
        card_model.add_widget(self.settings_model_spinner)
        s_layout.add_widget(card_model)

        # Card 2: Default Chunking Mode & Duration
        card_chunk = SurfaceCard(orientation="vertical", size_hint_y=None, height=80, spacing=6, padding=[12, 8, 12, 8])
        lbl2 = Label(text="Default Chunking Mode & Sizing:", font_size="12sp", bold=True, color=get_color_from_hex("#38BDF8"), size_hint_y=None, height=18, halign="left", valign="middle")
        lbl2.bind(size=lbl2.setter("text_size"))
        
        chunk_row = BoxLayout(orientation="horizontal", spacing=10, size_hint_y=None, height=32)
        is_auto = (self.settings.get("default_chunk_mode", "auto") == "auto")
        self.settings_chk_auto = CheckBox(group="settings_chunk_mode", active=is_auto, size_hint_x=None, width=24)
        self.settings_chk_auto.bind(active=self._on_settings_chunk_mode_changed)
        lbl_auto = Label(text="Auto Sizing", size_hint_x=None, width=90, font_size="12sp", color=get_color_from_hex("#E5E7EB"), halign="left", valign="middle")
        lbl_auto.bind(size=lbl_auto.setter("text_size"))

        self.settings_chk_manual = CheckBox(group="settings_chunk_mode", active=(not is_auto), size_hint_x=None, width=24)
        self.settings_chk_manual.bind(active=self._on_settings_chunk_mode_changed)
        lbl_manual = Label(text="Manual (minutes):", size_hint_x=None, width=120, font_size="12sp", color=get_color_from_hex("#E5E7EB"), halign="left", valign="middle")
        lbl_manual.bind(size=lbl_manual.setter("text_size"))

        self.settings_chunk_input = TextInput(
            text=str(self.settings.get("default_chunk_minutes", "30")),
            multiline=False,
            size_hint_x=None,
            width=65,
            disabled=is_auto,
            input_filter="float",
            font_size="12sp",
            background_color=get_color_from_hex("#27272A") if is_auto else get_color_from_hex("#121316"),
            foreground_color=get_color_from_hex("#71717A") if is_auto else get_color_from_hex("#F9FAFB"),
            padding=[6, 6, 6, 6]
        )
        self.settings_chunk_input.bind(text=self._on_settings_chunk_minutes_changed)

        chunk_row.add_widget(self.settings_chk_auto)
        chunk_row.add_widget(lbl_auto)
        chunk_row.add_widget(self.settings_chk_manual)
        chunk_row.add_widget(lbl_manual)
        chunk_row.add_widget(self.settings_chunk_input)

        card_chunk.add_widget(lbl2)
        card_chunk.add_widget(chunk_row)
        s_layout.add_widget(card_chunk)

        # Card 3: Default Output Directory
        card_out = SurfaceCard(orientation="vertical", size_hint_y=None, height=80, spacing=6, padding=[12, 8, 12, 8])
        lbl3 = Label(text="Default Output Folder for Saved Transcripts (.docx):", font_size="12sp", bold=True, color=get_color_from_hex("#38BDF8"), size_hint_y=None, height=18, halign="left", valign="middle")
        lbl3.bind(size=lbl3.setter("text_size"))

        out_row = BoxLayout(orientation="horizontal", spacing=8, size_hint_y=None, height=32)
        self.settings_out_input = TextInput(
            text=str(self.settings.get("default_output_folder", str(Path("output").resolve()))),
            hint_text="Default folder path...",
            multiline=False,
            font_size="12sp",
            background_color=get_color_from_hex("#121316"),
            foreground_color=get_color_from_hex("#F9FAFB"),
            padding=[6, 6, 6, 6]
        )
        self.settings_out_input.bind(text=self._on_settings_output_folder_changed)

        browse_out_btn = Button(
            text="Browse Folder...",
            size_hint_x=None,
            width=130,
            font_size="12sp",
            background_normal="",
            background_color=get_color_from_hex("#27272A"),
            color=get_color_from_hex("#FFFFFF")
        )
        browse_out_btn.bind(on_release=self._browse_default_output_folder)

        out_row.add_widget(self.settings_out_input)
        out_row.add_widget(browse_out_btn)

        card_out.add_widget(lbl3)
        card_out.add_widget(out_row)
        s_layout.add_widget(card_out)

        # Card 4: System Diagnostics, Daily Usage & Error Log Viewer (Section C & E)
        card_diag = SurfaceCard(orientation="vertical", size_hint_y=None, height=105, spacing=8, padding=[12, 8, 12, 8])
        lbl4 = Label(text="System Diagnostics, API Usage & Error Log:", font_size="12sp", bold=True, color=get_color_from_hex("#38BDF8"), size_hint_y=None, height=18, halign="left", valign="middle")
        lbl4.bind(size=lbl4.setter("text_size"))

        count, today_str = get_daily_api_usage()
        self.settings_daily_usage_lbl = Label(
            text=f"Gemini API Requests Today ({today_str}): {count}",
            font_size="12sp",
            color=get_color_from_hex("#10B981"),
            size_hint_y=None,
            height=18,
            halign="left",
            valign="middle"
        )
        self.settings_daily_usage_lbl.bind(size=self.settings_daily_usage_lbl.setter("text_size"))

        diag_btns = BoxLayout(orientation="horizontal", spacing=10, size_hint_y=None, height=32)
        view_log_btn = Button(
            text="View Error Log",
            size_hint_x=None,
            width=150,
            font_size="12sp",
            background_normal="",
            background_color=get_color_from_hex("#4F46E5"),
            color=get_color_from_hex("#FFFFFF"),
            bold=True
        )
        view_log_btn.bind(on_release=lambda x: self._open_log_viewer_window())

        clear_log_btn = Button(
            text="Delete Clear Error Log",
            size_hint_x=None,
            width=140,
            font_size="12sp",
            background_normal="",
            background_color=get_color_from_hex("#27272A"),
            color=get_color_from_hex("#E5E7EB")
        )
        clear_log_btn.bind(on_release=self._clear_error_log)

        diag_btns.add_widget(view_log_btn)
        diag_btns.add_widget(clear_log_btn)

        card_diag.add_widget(lbl4)
        card_diag.add_widget(self.settings_daily_usage_lbl)
        card_diag.add_widget(diag_btns)
        s_layout.add_widget(card_diag)

        s_scroll.add_widget(s_layout)
        self.settings_view.add_widget(s_scroll)


    def _on_settings_api_key_changed(self, instance, text):
        self.settings["gemini_api_key"] = text.strip()
        import os
        os.environ["GEMINI_API_KEY"] = text.strip()
        save_app_settings(self.settings)

    def _on_settings_model_changed(self, spinner, text: str):
        self.settings["default_model"] = text
        save_app_settings(self.settings)
        if hasattr(self, "model_spinner") and self.model_spinner:
            self.model_spinner.text = text

    def _on_settings_chunk_mode_changed(self, checkbox, value):
        if self.settings_chk_auto.active:
            self.settings["default_chunk_mode"] = "auto"
            self.settings_chunk_input.disabled = True
            self.settings_chunk_input.background_color = get_color_from_hex("#27272A")
            self.settings_chunk_input.foreground_color = get_color_from_hex("#71717A")
            if hasattr(self, "chk_auto") and self.chk_auto:
                self.chk_auto.active = True
        else:
            self.settings["default_chunk_mode"] = "manual"
            self.settings_chunk_input.disabled = False
            self.settings_chunk_input.background_color = get_color_from_hex("#1E1E24")
            self.settings_chunk_input.foreground_color = get_color_from_hex("#F9FAFB")
            if hasattr(self, "chk_manual") and self.chk_manual:
                self.chk_manual.active = True
        save_app_settings(self.settings)

    def _on_settings_chunk_minutes_changed(self, instance, text: str):
        self.settings["default_chunk_minutes"] = text.strip() or "30"
        save_app_settings(self.settings)
        if hasattr(self, "chunk_input") and self.chunk_input:
            self.chunk_input.text = self.settings["default_chunk_minutes"]

    def _on_settings_output_folder_changed(self, instance, text: str):
        self.settings["default_output_folder"] = text.strip()
        save_app_settings(self.settings)

    def _browse_default_output_folder(self, instance=None):
        def _ask():
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                folder = filedialog.askdirectory(
                    title="Select Default Output Folder for Saved Transcripts"
                )
                root.destroy()
                if folder:
                    Clock.schedule_once(lambda dt: self._set_output_folder_setting(folder))
            except Exception as e:
                print(f"[Error selecting folder]: {e}", file=sys.stderr)

        threading.Thread(target=_ask, daemon=True).start()

    def _set_output_folder_setting(self, folder_path: str):
        self.settings_out_input.text = folder_path
        self._on_settings_output_folder_changed(self.settings_out_input, folder_path)

    def _clear_error_log(self, instance=None):
        try:
            log_file = get_error_log_path()
            if log_file.exists():
                log_file.unlink()
            self.update_live_status("Error log cleared.", 0, 1)
        except Exception as e:
            print(f"[Error clearing log]: {e}", file=sys.stderr)

    def refresh_settings_view(self):
        count, today_str = get_daily_api_usage()
        if hasattr(self, "settings_daily_usage_lbl") and self.settings_daily_usage_lbl:
            self.settings_daily_usage_lbl.text = f"Gemini API Requests Today ({today_str}): {count}"
        if hasattr(self, "daily_usage_lbl") and self.daily_usage_lbl:
            self.daily_usage_lbl.text = f"Requests today: {count}"

    def on_api_call_increment(self):
        new_count = increment_daily_api_usage()
        if hasattr(self, "daily_usage_lbl") and self.daily_usage_lbl:
            self.daily_usage_lbl.text = f"Requests today: {new_count}"
        if hasattr(self, "settings_daily_usage_lbl") and self.settings_daily_usage_lbl:
            _, today_str = get_daily_api_usage()
            self.settings_daily_usage_lbl.text = f"Gemini API Requests Today ({today_str}): {new_count}"

    def _open_log_viewer_window(self):
        def _show():
            try:
                import tkinter as tk
                from tkinter import scrolledtext
                log_file = get_error_log_path()
                content = ""
                if log_file.exists():
                    try:
                        with open(log_file, "r", encoding="utf-8") as f:
                            content = f.read()
                    except Exception as e:
                        content = f"[Error reading log file]: {e}"
                
                if not content.strip():
                    content = "No error entries recorded. App error log is clean."

                root = tk.Tk()
                root.title("Application Error Log - In-App Viewer")
                root.attributes("-topmost", True)
                root.configure(bg="#18181B")
                root.geometry("760x520")

                hdr_frame = tk.Frame(root, bg="#18181B", padx=14, pady=10)
                hdr_frame.pack(fill="x")
                lbl = tk.Label(
                    hdr_frame,
                    text=f"Log File Location: {log_file}",
                    bg="#18181B",
                    fg="#9CA3AF",
                    font=("Segoe UI", 9)
                )
                lbl.pack(side="left")

                txt_area = scrolledtext.ScrolledText(
                    root,
                    wrap=tk.WORD,
                    bg="#111113",
                    fg="#F3F4F6",
                    insertbackground="#38BDF8",
                    font=("Consolas", 10),
                    padx=10,
                    pady=10
                )
                txt_area.insert(tk.END, content)
                txt_area.config(state=tk.DISABLED)
                txt_area.pack(fill="both", expand=True, padx=14, pady=4)

                btn_frame = tk.Frame(root, bg="#18181B", padx=14, pady=10)
                btn_frame.pack(fill="x")

                close_btn = tk.Button(
                    btn_frame,
                    text="Close",
                    bg="#374151",
                    fg="#FFFFFF",
                    font=("Segoe UI", 10, "bold"),
                    relief="flat",
                    padx=16,
                    pady=4,
                    command=root.destroy
                )
                close_btn.pack(side="right")

                root.mainloop()
            except Exception as e:
                print(f"[Error opening log viewer window]: {e}", file=sys.stderr)

        threading.Thread(target=_show, daemon=True).start()

    # ==========================================================================
    # Non-Modal Native DirectWrite Preview Windows (Transcript & Notes)
    # Renders complex Bengali script and English with 100% fidelity, copy support, and scrollbar.
    # ==========================================================================

    def open_transcript_preview_modal(self, instance=None):
        """
        Opens a clean, non-modal Tkinter window showing the full diarized transcript
        with native DirectWrite/Uniscribe font shaping for Bangla & English.
        """
        if not self.current_transcript or not self.current_transcript.strip():
            self.live_action_lbl.text = "No transcript available yet. Please transcribe audio or upload a transcript first."
            self.live_action_lbl.color = get_color_from_hex("#EF4444")
            return

        title_text = f"Diarized Transcript Preview - {self.current_title or 'Meeting'}"
        content_text = self.current_transcript.strip()
        self._spawn_native_preview_window(title=title_text, content=content_text, is_notes=False)

    def open_notes_preview_dialog(self, instance=None):
        """
        Opens a clean, non-modal Tkinter window showing the meeting notes.
        """
        if not self.current_meeting_notes or not self.current_meeting_notes.strip():
            self.live_action_lbl.text = "No meeting notes generated yet. Click 'Generate Notes' to create notes first."
            self.live_action_lbl.color = get_color_from_hex("#EF4444")
            return

        title_text = f"Meeting Notes Preview - {self.current_title or 'Meeting'}"
        content_text = self.current_meeting_notes.strip()
        self._spawn_native_preview_window(title=title_text, content=content_text, is_notes=True)

    def _spawn_native_preview_window(self, title: str, content: str, is_notes: bool = False, width: int = 700, height: int = 780):
        def _run_tk():
            try:
                import tkinter as tk
                from tkinter import scrolledtext

                root = tk.Tk()
                root.title(title)
                root.configure(bg="#18181B")

                # Geometry positioning (docked to the right of screen / main window)
                screen_w = root.winfo_screenwidth()
                screen_h = root.winfo_screenheight()
                x_pos = min(screen_w - width - 30, max(20, (screen_w // 2) + 60))
                y_pos = max(30, (screen_h - height) // 2)
                root.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

                # Header toolbar
                hdr_frame = tk.Frame(root, bg="#27272A", padx=14, pady=10)
                hdr_frame.pack(fill=tk.X, side=tk.TOP)

                theme_col = "#C084FC" if is_notes else "#38BDF8"
                hdr_lbl = tk.Label(
                    hdr_frame,
                    text=title,
                    font=("Segoe UI", 11, "bold"),
                    fg=theme_col,
                    bg="#27272A"
                )
                hdr_lbl.pack(side=tk.LEFT)

                copy_btn = tk.Button(
                    hdr_frame,
                    text="Copy All",
                    font=("Segoe UI", 9, "bold"),
                    bg="#374151",
                    fg="#FFFFFF",
                    activebackground="#4B5563",
                    activeforeground="#FFFFFF",
                    relief=tk.FLAT,
                    padx=12,
                    pady=2,
                    cursor="hand2",
                    command=lambda: [root.clipboard_clear(), root.clipboard_append(content)]
                )
                copy_btn.pack(side=tk.RIGHT, padx=4)

                # Scrollable Text Area with Bengali Font Support
                txt = scrolledtext.ScrolledText(
                    root,
                    font=("Nirmala UI", 11),
                    bg="#18181B",
                    fg="#F3F4F6",
                    insertbackground="#38BDF8",
                    selectbackground="#2563EB",
                    selectforeground="#FFFFFF",
                    wrap=tk.WORD,
                    padx=16,
                    pady=14,
                    relief=tk.FLAT
                )
                txt.pack(fill=tk.BOTH, expand=True)
                txt.insert(tk.END, content)
                txt.configure(state="disabled")

                # Bring to front once on spawn without locking
                root.attributes("-topmost", True)
                root.after(300, lambda: root.attributes("-topmost", False))
                root.mainloop()

            except Exception as e:
                print(f"[ERROR in _spawn_native_preview_window]: {e}\n{traceback.format_exc()}", file=sys.stderr)

        threading.Thread(target=_run_tk, daemon=True).start()

    # ==========================================================================
    # Main Window Embedded GDI Display
    # ==========================================================================

    def display_placeholder_message(self, text: str):
        try:
            self.transcript_display_layout.clear_widgets()
            render_w = max(600, int(self.display_scroll.width) if self.display_scroll.width > 100 else int(Window.width) - 40)
            tex, w, h = render_gdi_text_texture(
                text=text,
                font_size=15,
                text_color=(156, 163, 175),
                bg_color=(24, 24, 27),
                fixed_width=render_w,
                is_bold=False,
                margin_y=16
            )
            img = Image(texture=tex, size_hint=(1, None), height=h, fit_mode="fill")
            self.transcript_display_layout.add_widget(img)
        except Exception as e:
            print(f"\n[ERROR in display_placeholder_message]: {e}\n{traceback.format_exc()}", file=sys.stderr)

    def display_transcript_in_main_window(self, transcript_text: str, notes_text: str = ""):
        """
        Renders the complete transcript and meeting notes into native GDI textures and
        displays them directly in the scrollable view in the main window.
        Includes explicit diagnostic logging and error handling.
        """
        try:
            self.transcript_display_layout.clear_widgets()
            render_w = max(600, int(self.display_scroll.width) if self.display_scroll.width > 100 else int(Window.width) - 40)
            
            print(f"\n[GDI Display] Rendering transcript & notes in main window (target width: {render_w}px)...", flush=True)
            widget_count = 0

            # 1. Render Meeting Notes Header & Content if available
            if notes_text and notes_text.strip():
                # Section Header
                hdr_tex, _, hdr_h = render_gdi_text_texture(
                    text="MEETING MINUTES & ACTION ITEMS",
                    font_size=17,
                    text_color=(192, 132, 252),  # Light purple
                    bg_color=(30, 27, 46),       # Deep purple-tinted card
                    fixed_width=render_w,
                    is_bold=True,
                    margin_y=10
                )
                self.transcript_display_layout.add_widget(
                    Image(texture=hdr_tex, size_hint=(1, None), height=hdr_h, fit_mode="fill")
                )
                widget_count += 1

                # Metadata line
                meta_items = []
                if self.company_name:
                    meta_items.append(f"Company: {self.company_name}")
                if self.meeting_type:
                    meta_items.append(f"Type: {self.meeting_type}")
                if self.person_names:
                    meta_items.append(f"Participants: {', '.join(self.person_names)}")
                if self.current_topic_slug:
                    meta_items.append(f"Topic: {self.current_topic_slug}")

                if meta_items:
                    meta_str = " | ".join(meta_items)
                    m_tex, _, m_h = render_gdi_text_texture(
                        text=meta_str,
                        font_size=13,
                        text_color=(156, 163, 175),
                        bg_color=(24, 24, 27),
                        fixed_width=render_w,
                        is_bold=False,
                        margin_y=4
                    )
                    self.transcript_display_layout.add_widget(
                        Image(texture=m_tex, size_hint=(1, None), height=m_h, fit_mode="fill")
                    )
                    widget_count += 1

                # Notes paragraphs
                notes_paras = [p.strip() for p in notes_text.strip().split("\n") if p.strip()]
                for p in notes_paras:
                    is_heading = p.startswith("#")
                    clean_p = p.lstrip("#").strip() if is_heading else p
                    font_sz = 16 if is_heading else 15
                    t_col = (56, 189, 248) if is_heading else (243, 244, 246)
                    bg_col = (26, 26, 32) if is_heading else (20, 20, 24)

                    tex, _, h = render_gdi_text_texture(
                        text=clean_p,
                        font_size=font_sz,
                        text_color=t_col,
                        bg_color=bg_col,
                        fixed_width=render_w,
                        is_bold=is_heading,
                        margin_y=6 if is_heading else 4
                    )
                    self.transcript_display_layout.add_widget(
                        Image(texture=tex, size_hint=(1, None), height=h, fit_mode="fill")
                    )
                    widget_count += 1

                # Section Separator
                sep_tex, _, sep_h = render_gdi_text_texture(
                    text="FULL DIARIZED TRANSCRIPT",
                    font_size=17,
                    text_color=(56, 189, 248),  # Sky blue
                    bg_color=(20, 30, 46),      # Deep blue-tinted card
                    fixed_width=render_w,
                    is_bold=True,
                    margin_y=10
                )
                self.transcript_display_layout.add_widget(
                    Image(texture=sep_tex, size_hint=(1, None), height=sep_h, fit_mode="fill")
                )
                widget_count += 1

            # 2. Render Diarized Transcript Lines
            if transcript_text and transcript_text.strip():
                raw_lines = transcript_text.strip().split("\n")
                current_chunk = []

                for line in raw_lines:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    current_chunk.append(line_str)

                    # Batch lines for clean texture rendering
                    if len(current_chunk) >= 2 or len("\n".join(current_chunk)) > 400:
                        chunk_text = "\n".join(current_chunk)
                        tex, _, h = render_gdi_text_texture(
                            text=chunk_text,
                            font_size=15,
                            text_color=(228, 228, 231),
                            bg_color=(24, 24, 27),
                            fixed_width=render_w,
                            is_bold=False,
                            margin_y=5
                        )
                        self.transcript_display_layout.add_widget(
                            Image(texture=tex, size_hint=(1, None), height=h, fit_mode="fill")
                        )
                        widget_count += 1
                        current_chunk = []

                if current_chunk:
                    chunk_text = "\n".join(current_chunk)
                    tex, _, h = render_gdi_text_texture(
                        text=chunk_text,
                        font_size=15,
                        text_color=(228, 228, 231),
                        bg_color=(24, 24, 27),
                        fixed_width=render_w,
                        is_bold=False,
                        margin_y=5
                    )
                    self.transcript_display_layout.add_widget(
                        Image(texture=tex, size_hint=(1, None), height=h, fit_mode="fill")
                    )
                    widget_count += 1

            print(f"[GDI Display Success] Added {widget_count} image texture widgets to main window display.", flush=True)
            self.display_scroll.scroll_y = 1.0

        except Exception as e:
            print(f"\n[ERROR in display_transcript_in_main_window]: {e}\n{traceback.format_exc()}", file=sys.stderr)
            self.display_placeholder_message(f"=== Error displaying transcript ===\n{e}\nCheck terminal output for full traceback.")

    def new_session(self, instance=None):
        if self.is_processing:
            return

        # 1. Reset all metadata and identifiers
        self.current_title = "meeting_transcript"
        self.current_topic_slug = ""
        self.company_name = ""
        self.meeting_type = ""
        self.person_names = []
        self.recording_date = ""
        self.source_audio_path = ""
        self.source_audio_name = ""
        self.output_docx_path = None
        self.current_metadata = {}

        # 2. Reset transcript and notes text
        self.current_transcript = ""
        self.current_meeting_notes = ""

        # 3. Halt audio, recordings, and fully reset waveform clipper widget
        if self.is_recording:
            self._stop_recording()
        self.stop_all_sample_playback()
        self.main_waveform_widget.unload_audio()
        self.main_waveform_widget.peaks = []
        self.main_waveform_widget.audio_path = ""
        self.main_waveform_widget.total_duration = 0.0
        self.main_waveform_widget.playhead_sec = 0.0
        self.main_waveform_widget.sel_start_sec = 0.0
        self.main_waveform_widget.sel_end_sec = 0.0
        self.main_waveform_widget.redraw()

        self.main_wf_title_lbl.text = "Source Audio Waveform & Speaker Clipper"
        self.main_wf_sel_lbl.text = "Click & drag across waveform to clip a voice sample (< 8.0s)"
        self.main_wf_sel_lbl.color = get_color_from_hex("#38BDF8")
        self.main_save_region_btn.disabled = True
        self.main_save_region_btn.opacity = 0.4
        self.enhance_audio_btn.disabled = True
        self.enhance_audio_btn.opacity = 0.4
        self.main_play_btn.text = "> Play"
        self.main_play_btn.background_color = get_color_from_hex("#2563EB")

        # 4. Clear input field, progress bar, and status
        self.file_input.text = ""
        self.progress_bar.value = 0.0
        self.progress_bar.max = 1.0
        self.live_action_lbl.text = "Status: Ready"
        self.live_action_lbl.color = get_color_from_hex("#38BDF8")

        # 5. Reset display layout to initial welcome screen
        self.display_placeholder_message("=== Welcome to Bilingual Meeting Transcriber ===\nSelect an audio or video file above, or load an existing transcript to view here.")

        # 6. Uncheck all voice reference samples in Sample Library
        samples = load_voice_samples()
        if samples:
            for s in samples:
                s["include_in_transcription"] = False
            try:
                from transcribe import VOICE_SAMPLES_INDEX
                with open(VOICE_SAMPLES_INDEX, "w", encoding="utf-8") as f:
                    json.dump(samples, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[Error resetting samples index]: {e}", file=sys.stderr)
            self.render_sample_library_list()

        # 7. Update styling of post-processing action buttons
        self.notes_btn.text = "Generate Notes"
        self._enable_post_processing_buttons()

        # 8. Switch to main Transcribe view
        self.switch_to_transcribe_tab()

    def render_history_list(self):
        self.history_list_layout.clear_widgets()
        records = load_history()
        self.tab_btn_history.text = f"History ({len(records)})"
        if hasattr(self, "history_count_lbl"):
            self.history_count_lbl.text = f"Transcripts Indexed: {len(records)}"

        if not records:
            empty_lbl = Label(
                text="No saved transcript history yet.\nTranscribe an audio file or upload a transcript to save your first session!",
                color=get_color_from_hex("#71717A"),
                size_hint_y=None,
                height=120,
                halign="center",
                valign="middle"
            )
            self.history_list_layout.add_widget(empty_lbl)
            return

        for entry in records:
            card = SurfaceCard(orientation="horizontal", size_hint_y=None, height=68, padding=[12, 8, 12, 8], spacing=10)
            
            # Info Column
            info_col = BoxLayout(orientation="vertical", size_hint_x=0.72, spacing=2)
            name_lbl = Label(
                text=entry.get("display_name", "Untitled Document"),
                font_size="13sp",
                bold=True,
                color=get_color_from_hex("#F9FAFB"),
                halign="left",
                valign="middle",
                shorten=True,
                shorten_from="right"
            )
            name_lbl.bind(size=name_lbl.setter("text_size"))

            meta_row = BoxLayout(orientation="horizontal", spacing=8, size_hint_y=None, height=18)
            date_lbl = Label(
                text=f"Date: {entry.get('date', '2026-08-29')}",
                font_size="10sp",
                color=get_color_from_hex("#9CA3AF"),
                size_hint_x=None,
                width=85,
                halign="left",
                valign="middle"
            )
            date_lbl.bind(size=date_lbl.setter("text_size"))

            time_lbl = Label(
                text=f"{entry.get('time', '00:00:00')}",
                font_size="10sp",
                color=get_color_from_hex("#9CA3AF"),
                size_hint_x=None,
                width=75,
                halign="left",
                valign="middle"
            )
            time_lbl.bind(size=time_lbl.setter("text_size"))

            dur_lbl = Label(
                text=f"Dur: {entry.get('duration', 'N/A')}",
                font_size="10sp",
                color=get_color_from_hex("#9CA3AF"),
                size_hint_x=None,
                width=65,
                halign="left",
                valign="middle"
            )
            dur_lbl.bind(size=dur_lbl.setter("text_size"))

            src_lbl = Label(
                text=f"{entry.get('source_audio', 'N/A')}",
                font_size="10sp",
                color=get_color_from_hex("#9CA3AF"),
                size_hint_x=None,
                width=180,
                halign="left",
                valign="middle",
                shorten=True
            )
            src_lbl.bind(size=src_lbl.setter("text_size"))

            meta_row.add_widget(date_lbl)
            meta_row.add_widget(time_lbl)
            meta_row.add_widget(dur_lbl)
            meta_row.add_widget(src_lbl)

            if entry.get("has_notes"):
                notes_badge = Label(
                    text="Notes",
                    font_size="10sp",
                    bold=True,
                    color=get_color_from_hex("#10B981"),
                    size_hint_x=None,
                    width=65,
                    halign="left",
                    valign="middle"
                )
                notes_badge.bind(size=notes_badge.setter("text_size"))
                meta_row.add_widget(notes_badge)

            info_col.add_widget(name_lbl)
            info_col.add_widget(meta_row)
            card.add_widget(info_col)

            # Actions Column
            target_path = Path(entry.get("file_path", ""))
            entry_id = entry.get("id", "")
            
            open_btn = Button(
                text="Open Doc",
                size_hint_x=None,
                width=100,
                font_size="11sp",
                bold=True,
                background_normal="",
                background_color=get_color_from_hex("#1E2025"),
                color=get_color_from_hex("#38BDF8")
            )
            with open_btn.canvas.before:
                Color(*get_color_from_hex("#1E2025"))
                open_btn.op_bg = RoundedRectangle(pos=open_btn.pos, size=open_btn.size, radius=[8])
                Color(*get_color_from_hex("#3B82F6"))
                open_btn.op_line = Line(rounded_rectangle=(open_btn.x, open_btn.y, open_btn.width, open_btn.height, 8), width=1.0)
            open_btn.bind(
                pos=lambda s, p: (setattr(s.op_bg, "pos", p), setattr(s.op_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8))),
                size=lambda s, sz: (setattr(s.op_bg, "size", sz), setattr(s.op_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8)))
            )
            open_btn.bind(on_release=lambda x, p=target_path: self._open_external_file(p))

            more_btn = Button(
                text="...",
                size_hint_x=None,
                width=45,
                font_size="14sp",
                bold=True,
                background_normal="",
                background_color=get_color_from_hex("#1E2025"),
                color=get_color_from_hex("#D1D5DB")
            )
            with more_btn.canvas.before:
                Color(*get_color_from_hex("#1E2025"))
                more_btn.mb_bg = RoundedRectangle(pos=more_btn.pos, size=more_btn.size, radius=[8])
                Color(*get_color_from_hex("#2B2D35"))
                more_btn.mb_line = Line(rounded_rectangle=(more_btn.x, more_btn.y, more_btn.width, more_btn.height, 8), width=1.0)
            more_btn.bind(
                pos=lambda s, p: (setattr(s.mb_bg, "pos", p), setattr(s.mb_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8))),
                size=lambda s, sz: (setattr(s.mb_bg, "size", sz), setattr(s.mb_line, "rounded_rectangle", (s.x, s.y, s.width, s.height, 8)))
            )
            more_btn.bind(on_release=lambda x, eid=entry_id, fp=str(target_path): self._open_history_options_dialog(eid, fp))

            card.add_widget(open_btn)
            card.add_widget(more_btn)
            self.history_list_layout.add_widget(card)

    def _open_history_options_dialog(self, entry_id: str, file_path_str: str):
        target_path = Path(file_path_str) if file_path_str else None
        def _show_options():
            try:
                import tkinter as tk
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)

                win = tk.Toplevel(root)
                win.title("Document Options")
                win.geometry("380x250")
                win.configure(bg="#1E2025")
                win.resizable(False, False)
                win.attributes("-topmost", True)

                lbl = tk.Label(win, text=target_path.name if target_path else "Options", font=("Segoe UI", 11, "bold"), fg="#F9FAFB", bg="#1E2025", wraplength=350)
                lbl.pack(pady=10)

                def _open_folder():
                    win.destroy()
                    root.destroy()
                    if target_path:
                        self._open_external_folder(target_path.parent)

                def _load_app():
                    win.destroy()
                    root.destroy()
                    if target_path:
                        Clock.schedule_once(lambda dt: self._load_history_into_app(target_path))

                def _rename():
                    win.destroy()
                    root.destroy()
                    self.rename_history_entry(entry_id, file_path_str)

                def _delete():
                    win.destroy()
                    root.destroy()
                    self.delete_history_entry(entry_id, file_path_str)

                btn_frame = tk.Frame(win, bg="#1E2025")
                btn_frame.pack(fill="both", expand=True, padx=20, pady=5)

                b1 = tk.Button(btn_frame, text="Open Containing Folder", font=("Segoe UI", 10), bg="#2B2D35", fg="#F9FAFB", relief="flat", command=_open_folder)
                b1.pack(fill="x", pady=3)
                b2 = tk.Button(btn_frame, text=" Load into App Workspace", font=("Segoe UI", 10), bg="#2B2D35", fg="#F9FAFB", relief="flat", command=_load_app)
                b2.pack(fill="x", pady=3)
                b3 = tk.Button(btn_frame, text="Edit Rename Document", font=("Segoe UI", 10), bg="#2B2D35", fg="#F9FAFB", relief="flat", command=_rename)
                b3.pack(fill="x", pady=3)
                b4 = tk.Button(btn_frame, text="Delete from History / Disk", font=("Segoe UI", 10), bg="#7F1D1D", fg="#FCA5A5", relief="flat", command=_delete)
                b4.pack(fill="x", pady=3)

                win.mainloop()
            except Exception as e:
                print(f"[Error in history options]: {e}", file=sys.stderr)

        threading.Thread(target=_show_options, daemon=True).start()

    def rename_history_entry(self, entry_id: str, current_file_path_str: str):
        def _prompt_rename():
            try:
                import tkinter as tk
                from tkinter import simpledialog, messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)

                target_path = Path(current_file_path_str) if current_file_path_str else None
                current_filename = target_path.name if target_path else "document.docx"

                new_filename = simpledialog.askstring(
                    "Rename Document",
                    "Enter new filename (including .docx):",
                    initialvalue=current_filename,
                    parent=root
                )
                if not new_filename or not new_filename.strip() or new_filename.strip() == current_filename:
                    root.destroy()
                    return

                new_filename = new_filename.strip()
                if not new_filename.lower().endswith(".docx"):
                    new_filename += ".docx"

                parent_dir = target_path.parent if target_path and target_path.exists() else Path("output").resolve()
                new_target_path = parent_dir / new_filename

                paired_old_path = None
                paired_new_path = None
                cur_stem = target_path.stem if target_path else ""
                new_stem = new_target_path.stem

                if cur_stem.endswith("_Transcript"):
                    base_cur = cur_stem[:-len("_Transcript")]
                    candidate = parent_dir / f"{base_cur}_Notes.docx"
                    if candidate.exists() and candidate != target_path:
                        paired_old_path = candidate
                        if new_stem.endswith("_Transcript"):
                            base_new = new_stem[:-len("_Transcript")]
                            paired_new_path = parent_dir / f"{base_new}_Notes.docx"
                        else:
                            paired_new_path = parent_dir / f"{new_stem}_Notes.docx"

                elif cur_stem.endswith("_Notes"):
                    base_cur = cur_stem[:-len("_Notes")]
                    candidate = parent_dir / f"{base_cur}_Transcript.docx"
                    if candidate.exists() and candidate != target_path:
                        paired_old_path = candidate
                        if new_stem.endswith("_Notes"):
                            base_new = new_stem[:-len("_Notes")]
                            paired_new_path = parent_dir / f"{base_new}_Transcript.docx"
                        else:
                            paired_new_path = parent_dir / f"{new_stem}_Transcript.docx"

                rename_paired = False
                if paired_old_path and paired_new_path:
                    rename_paired = messagebox.askyesno(
                        "Matching Paired File Found",
                        f"A matching paired document was found:\n{paired_old_path.name}\n\nDo you want to rename both files together?\n\n. {current_filename} -> {new_filename}\n. {paired_old_path.name} -> {paired_new_path.name}",
                        parent=root
                    )

                root.destroy()

                # Perform rename on disk
                if target_path and target_path.exists():
                    try:
                        os.rename(str(target_path), str(new_target_path))
                    except Exception as e:
                        print(f"[Error renaming target file]: {e}", file=sys.stderr)

                if rename_paired and paired_old_path and paired_old_path.exists():
                    try:
                        os.rename(str(paired_old_path), str(paired_new_path))
                    except Exception as e:
                        print(f"[Error renaming paired file]: {e}", file=sys.stderr)

                # Update history.json
                history = load_history()
                for h in history:
                    if h.get("id") == entry_id or h.get("file_path") == str(target_path.resolve()):
                        h["display_name"] = new_filename
                        h["file_path"] = str(new_target_path.resolve())
                    elif rename_paired and paired_old_path and h.get("file_path") == str(paired_old_path.resolve()):
                        h["display_name"] = paired_new_path.name
                        h["file_path"] = str(paired_new_path.resolve())

                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(history, f, indent=2, ensure_ascii=False)

                Clock.schedule_once(lambda dt: self.render_history_list())
                Clock.schedule_once(lambda dt: self.update_live_status(f"Renamed: {new_filename}", 0, 1))

            except Exception as e:
                print(f"[Error during history rename]: {e}", file=sys.stderr)

        threading.Thread(target=_prompt_rename, daemon=True).start()

    def delete_history_entry(self, entry_id: str, file_path_str: str):
        def _prompt_delete():
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)

                confirm_hist = messagebox.askyesno(
                    "Delete History Entry",
                    "Remove this session from your history index?"
                )
                if not confirm_hist:
                    root.destroy()
                    return

                target_path = Path(file_path_str) if file_path_str else None
                delete_disk_file = False
                if target_path and target_path.exists():
                    delete_disk_file = messagebox.askyesno(
                        "Delete File on Disk?",
                        f"Do you also want to delete the saved file on disk?\n\n{target_path}"
                    )

                root.destroy()

                if delete_disk_file and target_path and target_path.exists():
                    try:
                        target_path.unlink()
                    except Exception as e:
                        print(f"Error deleting file from disk: {e}", file=sys.stderr)

                history = load_history()
                history = [h for h in history if h.get("id") != entry_id]
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(history, f, indent=2, ensure_ascii=False)

                Clock.schedule_once(lambda dt: self.render_history_list())
                Clock.schedule_once(lambda dt: self.update_live_status("History entry removed.", 0, 1))

            except Exception as e:
                print(f"[Error during history delete]: {e}", file=sys.stderr)

        threading.Thread(target=_prompt_delete, daemon=True).start()

    def _open_external_file(self, file_path: Path):
        if file_path.exists():
            try:
                os.startfile(str(file_path))
            except Exception:
                subprocess.Popen(["explorer", str(file_path)])

    def _open_external_folder(self, folder_path: Path):
        folder_path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(folder_path))
        except Exception:
            subprocess.Popen(["explorer", str(folder_path)])

    def _load_history_into_app(self, file_path: Path):
        self._load_existing_transcript(str(file_path))
        self.switch_to_transcribe_tab()

    def on_chunk_mode_change(self, checkbox, value):
        if self.chk_auto.active:
            self.chunk_input.disabled = True
            self.chunk_input.background_color = get_color_from_hex("#27272A")
            self.chunk_input.foreground_color = get_color_from_hex("#71717A")
        else:
            self.chunk_input.disabled = False
            self.chunk_input.background_color = get_color_from_hex("#1E1E24")
            self.chunk_input.foreground_color = get_color_from_hex("#F9FAFB")

    def open_audio_file_dialog(self, instance):
        if self.is_processing:
            return

        def _ask():
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                file_path = filedialog.askopenfilename(
                    title="Select Audio/Video File",
                    filetypes=[
                        ("Audio/Video Files", "*.mp3 *.m4a *.wav *.mp4 *.mkv *.aac *.flac *.ogg *.wma"),
                        ("All Files", "*.*")
                    ]
                )
                root.destroy()
                if file_path:
                    Clock.schedule_once(lambda dt: self._set_selected_audio(file_path))
            except Exception as e:
                print(f"[Error opening audio dialog]: {e}", file=sys.stderr)

        threading.Thread(target=_ask, daemon=True).start()

    # ==========================================================================
    # Audio Input Device Management & Gain Settings
    # ==========================================================================

    def refresh_audio_input_devices(self):
        """
        Queries all available audio input endpoints via sounddevice,
        populates the dropdown, and restores the user's previously selected microphone.
        """
        try:
            raw_devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            self.input_devices_map = []
            values = []

            for idx, d in enumerate(raw_devices):
                if d.get("max_input_channels", 0) > 0:
                    api_name = hostapis[d["hostapi"]]["name"]
                    dev_name = d["name"]
                    label = f"[{idx}] {dev_name} ({api_name})"
                    self.input_devices_map.append({
                        "index": idx,
                        "name": dev_name,
                        "api": api_name,
                        "label": label
                    })
                    values.append(label)

            self.input_device_spinner.values = values

            saved_cfg = load_recorder_config()
            saved_dev_name = saved_cfg.get("last_device_name", "")
            saved_gain = saved_cfg.get("manual_gain", "2.0x (Default)")

            # Set gain spinner
            if saved_gain in self.gain_spinner.values:
                self.gain_spinner.text = saved_gain
                self._on_gain_setting_changed(self.gain_spinner, saved_gain)

            # Match saved device or fall back to default input device
            matched_label = None
            if saved_dev_name:
                for item in self.input_devices_map:
                    if item["name"] == saved_dev_name or item["label"] == saved_dev_name:
                        matched_label = item["label"]
                        break

            if not matched_label:
                def_in_idx = sd.default.device[0]
                for item in self.input_devices_map:
                    if item["index"] == def_in_idx:
                        matched_label = item["label"]
                        break

            if not matched_label and self.input_devices_map:
                matched_label = self.input_devices_map[0]["label"]

            if matched_label:
                self.input_device_spinner.text = matched_label
                self._on_input_device_selected(self.input_device_spinner, matched_label)
            else:
                self.input_device_spinner.text = "No Input Device Found"

        except Exception as e:
            print(f"[Error refreshing audio input devices]: {e}", file=sys.stderr)
            self.input_device_spinner.text = "Default System Microphone"

    def _on_input_device_selected(self, spinner, text: str):
        for item in self.input_devices_map:
            if item["label"] == text:
                self.selected_input_device_idx = item["index"]
                self.selected_input_device_name = item["name"]
                cfg = load_recorder_config()
                cfg["last_device_name"] = item["name"]
                save_recorder_config(cfg)
                print(f"[Meeting Recorder] Selected input device: [{item['index']}] '{item['name']}' ({item['api']})", flush=True)
                break

    def _on_gain_setting_changed(self, spinner, text: str):
        mult = 2.0
        try:
            if "1.0x" in text:
                mult = 1.0
            elif "1.5x" in text:
                mult = 1.5
            elif "2.0x" in text:
                mult = 2.0
            elif "3.0x" in text:
                mult = 3.0
            elif "5.0x" in text:
                mult = 5.0
            elif "8.0x" in text:
                mult = 8.0
        except Exception:
            mult = 2.0
        self.manual_gain_multiplier = mult
        if hasattr(self, "gain_slider") and self.gain_slider:
            self.gain_slider.value = mult
        cfg = load_recorder_config()
        cfg["manual_gain"] = text
        save_recorder_config(cfg)
        print(f"[Meeting Recorder] Manual gain set to {self.manual_gain_multiplier:.1f}x", flush=True)

    def _on_gain_slider_changed(self, slider, val: float):
        mult = round(val, 1)
        self.manual_gain_multiplier = mult
        if hasattr(self, "gain_spinner") and self.gain_spinner:
            if mult >= 5.0:
                txt = "5.0x High"
            elif mult >= 3.0:
                txt = "3.0x (Boost)"
            elif mult >= 2.0:
                txt = "2.0x (Default)"
            elif mult >= 1.5:
                txt = "1.5x"
            else:
                txt = "1.0x (Raw)"
            if self.gain_spinner.text != txt:
                self.gain_spinner.text = txt
        cfg = load_recorder_config()
        cfg["manual_gain"] = f"{mult:.1f}x"
        save_recorder_config(cfg)

    def _on_model_spinner_changed(self, spinner, text: str):
        raw_model = text.replace("Model: ", "").strip()
        self.settings["default_model"] = raw_model
        save_app_settings(self.settings)
        if hasattr(self, "settings_model_spinner") and self.settings_model_spinner:
            self.settings_model_spinner.text = raw_model

    # ==========================================================================
    # Live Microphone Meeting Recorder & Live Meter
    # ==========================================================================

    def toggle_record_meeting(self, instance=None):
        if self.is_processing:
            return
        if not self.is_recording:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        try:
            self.stop_all_sample_playback()
            self.is_recording = True
            self.recording_frames = []
            self.recording_start_time = time.time()
            self.live_input_peak = 0.0

            # Determine device index
            dev_idx = self.selected_input_device_idx
            if dev_idx < 0:
                try:
                    dev_idx = sd.default.device[0]
                except Exception:
                    dev_idx = None

            dev_name = self.selected_input_device_name or "Microphone"

            # Update Record Button Styling to active recording state
            self.record_meeting_btn.text = "Stop Recording"
            self.record_meeting_btn.background_color = get_color_from_hex("#DC2626")

            # Lock conflicting controls during live recording
            self.start_btn.disabled = True
            self.start_btn.opacity = 0.5
            self.browse_audio_btn.disabled = True
            self.upload_doc_btn.disabled = True
            self.input_device_spinner.disabled = True

            def audio_callback(indata, frames, time_info, status):
                if status:
                    print(f"[Meeting Recorder Status]: {status}", file=sys.stderr)
                if self.is_recording:
                    self.recording_frames.append(indata.copy())
                    # Fast attack, smooth tracking for live level meter
                    chunk_peak = float(np.max(np.abs(indata)))
                    self.live_input_peak = max(chunk_peak, self.live_input_peak * 0.70)

            self.recording_stream = sd.InputStream(
                device=dev_idx if (dev_idx is not None and dev_idx >= 0) else None,
                samplerate=self.recording_sample_rate,
                channels=1,
                dtype="float32",
                callback=audio_callback
            )
            self.recording_stream.start()

            # Start real-time clock and live level visualizer interval (150ms)
            self.recording_clock = Clock.schedule_interval(self._update_recording_timer, 0.15)
            self.live_action_lbl.text = f"(*) RECORDING IN PROGRESS - 00:00 ({dev_name})"
            self.live_action_lbl.color = get_color_from_hex("#EF4444")
            print(f"[Meeting Recorder] Started recording at {self.recording_sample_rate}Hz mono from [{dev_idx}] '{dev_name}' (Gain: {self.manual_gain_multiplier:.1f}x)...", flush=True)

        except Exception as e:
            self.is_recording = False
            self.record_meeting_btn.text = "Record Meeting"
            self.record_meeting_btn.background_color = get_color_from_hex("#059669")
            self.start_btn.disabled = False
            self.start_btn.opacity = 1.0
            self.browse_audio_btn.disabled = False
            self.upload_doc_btn.disabled = False
            self.input_device_spinner.disabled = False
            print(f"[ERROR in _start_recording]: {e}\n{traceback.format_exc()}", file=sys.stderr)
            self.live_action_lbl.text = f"Recording Error: {e}"
            self.live_action_lbl.color = get_color_from_hex("#EF4444")

    def _update_recording_timer(self, dt):
        if not self.is_recording:
            return False
        elapsed = time.time() - self.recording_start_time
        time_str = format_timestamp(elapsed)

        # Compute live dB level from current peak
        peak_val = max(self.live_input_peak, 1e-4)
        db = 20.0 * np.log10(peak_val)
        
        # Map -48dB to 0dB to a 10-bar visual indicator
        norm_val = max(0.0, min(1.0, (db + 48.0) / 48.0))
        bars = int(round(norm_val * 10))
        meter_str = "|" * bars + "-" * (10 - bars)

        if db < -36.0:
            level_color = "#F59E0B"  # Amber: quiet/low
            status_hint = "Low"
        elif db <= -2.5:
            level_color = "#10B981"  # Emerald Green: healthy speech volume
            status_hint = "Good"
        else:
            level_color = "#EF4444"  # Red: loud / near peak
            status_hint = "Loud"

        self.live_action_lbl.text = f"(*) RECORDING - {time_str} | Level: [{meter_str}] {db:+.1f}dB"
        self.live_action_lbl.color = get_color_from_hex("#EF4444")
        self.record_meeting_btn.text = f"Stop ({time_str})"

        # Decay peak smoothly for next frame
        if hasattr(self, 'vu_meter_widget') and self.vu_meter_widget:
            self.vu_meter_widget.set_level(peak_val)
        self.live_input_peak *= 0.65
        return True

    def _boost_and_limit_audio(self, audio_data: np.ndarray, target_peak_db: float = -1.0, target_rms_db: float = -15.0) -> np.ndarray:
        """
        Combines user manual gain multiplier + healthy speech RMS normalization + soft-knee tanh limiting:
        1. Multiplies audio by user's chosen manual gain (e.g. 1.0x to 8.0x).
        2. Normalizes quiet speech towards standard listening level (-15dB RMS).
        3. Smoothly limits peaks exceeding 0.70 via hyperbolic tangent (tanh) to guarantee zero distortion/clipping.
        """
        if len(audio_data) == 0:
            return audio_data

        # 1. Remove DC hardware offset
        audio = audio_data.astype(np.float32) - float(np.mean(audio_data))

        # 2. Apply user's manual gain multiplier first
        user_gain = float(self.manual_gain_multiplier)
        audio = audio * user_gain

        abs_audio = np.abs(audio)
        raw_peak = float(np.max(abs_audio))
        if raw_peak < 1e-5:
            return audio

        raw_rms = float(np.sqrt(np.mean(audio**2)))
        target_rms = 10.0 ** (target_rms_db / 20.0)    # ~0.178 for -15 dBFS
        target_peak = 10.0 ** (target_peak_db / 20.0)  # ~0.891 for -1.0 dBFS

        rms_gain = target_rms / max(raw_rms, 1e-4)
        peak_gain = target_peak / max(raw_peak, 1e-4)

        # Dynamic gain capped at +36dB (63x)
        additional_gain = min(max(rms_gain, peak_gain), 63.0)
        boosted = audio * additional_gain

        # 3. Soft-Knee Limiter (Clipping Protection)
        knee_thresh = 0.70
        ceiling = 0.96
        over_idx = np.abs(boosted) > knee_thresh
        if np.any(over_idx):
            sign = np.sign(boosted[over_idx])
            excess = np.abs(boosted[over_idx]) - knee_thresh
            headroom = ceiling - knee_thresh
            compressed_excess = headroom * np.tanh(excess / headroom)
            boosted[over_idx] = sign * (knee_thresh + compressed_excess)

        # Final safety clamp
        boosted = np.clip(boosted, -0.99, 0.99)
        final_peak = float(np.max(np.abs(boosted)))
        final_rms = float(np.sqrt(np.mean(boosted**2)))
        total_applied_gain = user_gain * additional_gain
        print(f"[Meeting Recorder Audio Boost] Raw Peak: {raw_peak/user_gain:.4f} -> Boosted: {final_peak:.4f} | RMS: {raw_rms/user_gain:.4f} -> {final_rms:.4f} (Total gain: {total_applied_gain:.2f}x / {20*np.log10(total_applied_gain):+.1f}dB)", flush=True)
        return boosted

    def _stop_recording(self):
        if not self.is_recording:
            return

        self.is_recording = False
        if self.recording_clock:
            self.recording_clock.cancel()
            self.recording_clock = None

        if self.recording_stream:
            try:
                self.recording_stream.stop()
                self.recording_stream.close()
            except Exception as e:
                print(f"[Error closing recording stream]: {e}", file=sys.stderr)
            self.recording_stream = None

        # Reset button styling & unlock UI
        self.record_meeting_btn.text = "Record Meeting"
        self.record_meeting_btn.background_color = get_color_from_hex("#059669")
        self.start_btn.disabled = False
        self.start_btn.opacity = 1.0
        self.browse_audio_btn.disabled = False
        self.upload_doc_btn.disabled = False
        self.input_device_spinner.disabled = False

        if not self.recording_frames:
            self.live_action_lbl.text = "Recording stopped. No audio frames captured."
            self.live_action_lbl.color = get_color_from_hex("#F59E0B")
            return

        try:
            rec_dir = Path("recordings")
            rec_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            rec_path = (rec_dir / f"Meeting_Recording_{ts}.wav").resolve()

            raw_audio_data = np.concatenate(self.recording_frames, axis=0)

            # Apply manual gain + aggressive gain boost + soft-knee limiter
            processed_audio = self._boost_and_limit_audio(raw_audio_data)

            # Write standardized 16-bit PCM WAV
            sf.write(str(rec_path), processed_audio, self.recording_sample_rate, subtype="PCM_16")
            dur_sec = len(processed_audio) / float(self.recording_sample_rate)

            print(f"[Meeting Recorder] Saved {dur_sec:.1f}s recording to: {rec_path}", flush=True)

            # Automatically populate existing Audio/Video file field & waveform
            self._set_selected_audio(str(rec_path))
            self.live_action_lbl.text = f"Recorded & Boosted: '{rec_path.name}' ({format_timestamp(dur_sec)}). Ready to transcribe!"
            self.live_action_lbl.color = get_color_from_hex("#10B981")

        except Exception as e:
            print(f"[ERROR in saving recording]: {e}\n{traceback.format_exc()}", file=sys.stderr)
            self.live_action_lbl.text = f"Error saving recording: {e}"
            self.live_action_lbl.color = get_color_from_hex("#EF4444")

    # ==========================================================================
    # Post-Recording / Imported File Audio Enhancer (FFmpeg EBU R128 Loudnorm)
    # ==========================================================================

    def enhance_current_audio_file(self, instance=None):
        """
        Runs the currently loaded audio file through ffmpeg's EBU R128 loudnorm filter
        (proper broadcast-standard loudness normalization) to correct weak audio.
        """
        if not self.source_audio_path or not Path(self.source_audio_path).exists():
            self.live_action_lbl.text = "No audio file loaded to enhance. Please select or record an audio file first."
            self.live_action_lbl.color = get_color_from_hex("#EF4444")
            return
        if self.is_processing or self.is_recording:
            return

        def _run_enhance():
            try:
                Clock.schedule_once(lambda dt: self._set_enhancing_state(True))
                src = Path(self.source_audio_path)
                out_dir = Path("recordings")
                out_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_path = (out_dir / f"{src.stem}_enhanced_{ts}.wav").resolve()

                cmd = [
                    "ffmpeg", "-y",
                    "-i", str(src.resolve()),
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar", "16000",
                    str(out_path)
                ]
                print(f"[Audio Enhancer] Running EBU R128 loudnorm filter on '{src.name}'...", flush=True)
                proc = subprocess.run(cmd, capture_output=True, text=True)

                if proc.returncode != 0 or not out_path.exists():
                    raise RuntimeError(f"FFmpeg loudnorm failed with code {proc.returncode}: {proc.stderr[:300]}")

                print(f"[Audio Enhancer] Successfully created enhanced audio: {out_path}", flush=True)
                Clock.schedule_once(lambda dt: self._on_enhance_complete(str(out_path)))

            except Exception as e:
                print(f"[ERROR in enhance_current_audio_file]: {e}\n{traceback.format_exc()}", file=sys.stderr)
                Clock.schedule_once(lambda dt: self._on_enhance_error(str(e)))

        threading.Thread(target=_run_enhance, daemon=True).start()

    def _set_enhancing_state(self, is_enhancing: bool):
        if is_enhancing:
            self.enhance_audio_btn.text = "Dur: Enhancing Audio..."
            self.enhance_audio_btn.disabled = True
            self.enhance_audio_btn.opacity = 0.6
            self.live_action_lbl.text = "Applying EBU R128 broadcast loudness normalization (FFmpeg loudnorm)..."
            self.live_action_lbl.color = get_color_from_hex("#38BDF8")
        else:
            self.enhance_audio_btn.text = "Enhance Audio"
            self.enhance_audio_btn.disabled = False
            self.enhance_audio_btn.opacity = 1.0

    def _on_enhance_complete(self, out_path: str):
        self._set_enhancing_state(False)
        self._set_selected_audio(out_path)
        self.live_action_lbl.text = f"* Audio Enhanced (Loudnorm EBU R128): '{Path(out_path).name}'. Ready to transcribe!"
        self.live_action_lbl.color = get_color_from_hex("#10B981")

    def _on_enhance_error(self, err_msg: str):
        self._set_enhancing_state(False)
        self.live_action_lbl.text = f"Audio Enhancement Error: {err_msg}"
        self.live_action_lbl.color = get_color_from_hex("#EF4444")

    def _set_selected_audio(self, file_path: str):
        self.file_input.text = file_path
        self.source_audio_name = Path(file_path).name
        self.source_audio_path = file_path
        self.current_title = Path(file_path).stem
        self.current_topic_slug = ""
        self.company_name = ""
        self.meeting_type = ""
        self.person_names = []
        self.recording_date = get_file_recording_date(file_path)
        self.live_action_lbl.text = f"Selected: {self.source_audio_name} (Date: {self.recording_date})"
        self.live_action_lbl.color = get_color_from_hex("#38BDF8")

        # Enable Enhance Audio button
        self.enhance_audio_btn.disabled = False
        self.enhance_audio_btn.opacity = 1.0

        # Load into main screen waveform widget
        self.main_wf_title_lbl.text = f"Waveform & Clipper: {self.source_audio_name}"
        self.main_waveform_widget.load_audio(file_path, selection_callback=self._on_main_waveform_selection)

    def open_existing_transcript_dialog(self, instance):
        if self.is_processing:
            return

        def _ask():
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                file_path = filedialog.askopenfilename(
                    title="Select Existing Transcript (.docx or .txt)",
                    filetypes=[
                        ("Transcript Files", "*.docx *.txt"),
                        ("Word Documents (*.docx)", "*.docx"),
                        ("Text Files (*.txt)", "*.txt"),
                        ("All Files", "*.*")
                    ]
                )
                root.destroy()
                if file_path:
                    Clock.schedule_once(lambda dt: self._load_existing_transcript(file_path))
            except Exception as e:
                print(f"[Error opening transcript dialog]: {e}", file=sys.stderr)

        threading.Thread(target=_ask, daemon=True).start()

    def _load_existing_transcript(self, file_path_str: str):
        path = Path(file_path_str)
        try:
            text = extract_transcript_from_file(path)
            if not text.strip():
                self.live_action_lbl.text = "Selected file contains no readable text."
                self.live_action_lbl.color = get_color_from_hex("#EF4444")
                return

            self.current_title = path.stem
            self.source_audio_name = f"Imported: {path.name}"
            self.source_audio_path = file_path_str
            self.current_transcript = text
            self.current_meeting_notes = ""
            self.current_topic_slug = ""
            self.company_name = ""
            self.meeting_type = ""
            self.person_names = []
            self.recording_date = get_file_recording_date(file_path_str)
            self.current_metadata = {
                "source": path.name,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

            self.file_input.text = file_path_str
            self.live_action_lbl.text = f"Loaded transcript: {path.name} (Date: {self.recording_date})"
            self.live_action_lbl.color = get_color_from_hex("#10B981")
            self.progress_bar.max = 1.0
            self.progress_bar.value = 1.0

            self._enable_post_processing_buttons()
            self.display_transcript_in_main_window(self.current_transcript, self.current_meeting_notes)

        except Exception as e:
            print(f"\n[ERROR in _load_existing_transcript]: {e}\n{traceback.format_exc()}", file=sys.stderr)
            self.live_action_lbl.text = f"Failed to load transcript: {e}"
            self.live_action_lbl.color = get_color_from_hex("#EF4444")

    def update_live_status(self, action_text: str, current_step: int, total_steps: int):
        def _apply(dt):
            total = max(1, total_steps)
            curr = max(0, min(current_step, total))
            self.progress_bar.max = total
            self.progress_bar.value = curr
            percent = int((curr / float(total)) * 100) if total > 0 else 0
            self.live_action_lbl.text = f"Status: {action_text} ({percent}%)"
            self.live_action_lbl.color = get_color_from_hex("#38BDF8")
        Clock.schedule_once(_apply)

    def log_status(self, message: str):
        def _apply(dt):
            if not self.is_processing:
                return
            cleaned = message.strip().split("\n")[0]
            if len(cleaned) > 75:
                cleaned = cleaned[:72] + "..."
            self.live_action_lbl.text = f"Status: {cleaned}"
        Clock.schedule_once(_apply)

    def _enable_post_processing_buttons(self):
        has_transcript = bool(self.current_transcript and self.current_transcript.strip())
        has_notes = bool(self.current_meeting_notes and self.current_meeting_notes.strip())

        # Buttons are always clickable (disabled=False) with dynamic color styling
        self.preview_transcript_btn.disabled = False
        self.preview_transcript_btn.opacity = 1.0 if has_transcript else 0.65
        self.preview_transcript_btn.background_color = get_color_from_hex("#1E40AF") if has_transcript else get_color_from_hex("#374151")

        self.notes_btn.disabled = False
        self.notes_btn.opacity = 1.0 if has_transcript else 0.65
        self.notes_btn.background_color = get_color_from_hex("#7C3AED") if has_transcript else get_color_from_hex("#374151")

        self.save_transcript_btn.disabled = False
        self.save_transcript_btn.opacity = 1.0 if has_transcript else 0.65
        self.save_transcript_btn.background_color = get_color_from_hex("#059669") if has_transcript else get_color_from_hex("#374151")

        self.preview_notes_btn.disabled = False
        self.preview_notes_btn.opacity = 1.0 if has_notes else 0.65
        self.preview_notes_btn.background_color = get_color_from_hex("#5B21B6") if has_notes else get_color_from_hex("#374151")

        self.save_notes_btn.disabled = False
        self.save_notes_btn.opacity = 1.0 if has_notes else 0.65
        self.save_notes_btn.background_color = get_color_from_hex("#0D9488") if has_notes else get_color_from_hex("#374151")

    def check_and_prompt_draft_recovery(self):
        """Checks if an unfinalized transcript draft exists on startup and offers to recover it."""
        draft = load_draft_session()
        if not draft or not draft.get("transcript"):
            return

        def _ask_recover():
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)

                audio_name = draft.get("source_audio_name") or draft.get("title") or "Unnamed Recording"
                saved_time = draft.get("saved_at", "a previous session")
                
                recover = messagebox.askyesno(
                    "Recover Unsaved Transcript?",
                    f"An unfinalized transcript from a previous session was found:\n\n"
                    f". Source Audio: {audio_name}\n"
                    f". Auto-saved: {saved_time}\n\n"
                    f"Would you like to recover this draft transcript now?",
                    parent=root
                )
                root.destroy()

                if recover:
                    Clock.schedule_once(lambda dt: self._apply_recovered_draft(draft))
                else:
                    clear_draft_session()

            except Exception as e:
                print(f"[Error in draft recovery prompt]: {e}", file=sys.stderr)

        threading.Thread(target=_ask_recover, daemon=True).start()

    def _apply_recovered_draft(self, draft: dict):
        self.current_transcript = draft.get("transcript", "")
        self.current_meeting_notes = draft.get("meeting_notes", "")
        self.current_title = draft.get("title", "recovered_transcript")
        self.source_audio_path = draft.get("source_audio_path", "")
        self.source_audio_name = draft.get("source_audio_name", "")
        self.current_metadata = draft.get("metadata", {})
        if self.source_audio_path:
            self.file_input.text = self.source_audio_path

        self._enable_post_processing_buttons()
        self.display_transcript_in_main_window(self.current_transcript, self.current_meeting_notes)
        self.live_action_lbl.text = f"Recovered draft: '{self.current_title}'. Ready to preview or save."
        self.live_action_lbl.color = get_color_from_hex("#10B981")
        print(f"[Draft Recovery] Successfully restored session '{self.current_title}'", flush=True)

    def start_transcription(self, instance=None):
        if self.is_processing:
            self.cancel_transcription()
            return

        file_path_str = self.file_input.text.strip()
        if not file_path_str:
            self.live_action_lbl.text = "Please select an audio or video file first."
            self.live_action_lbl.color = get_color_from_hex("#EF4444")
            return

        audio_file = Path(file_path_str)
        if not audio_file.exists():
            self.live_action_lbl.text = f"File not found: {audio_file.name}"
            self.live_action_lbl.color = get_color_from_hex("#EF4444")
            return

        if self.chk_auto.active:
            chunk_arg = "auto"
        else:
            try:
                chunk_arg = float(self.chunk_input.text.strip() or "30")
            except ValueError:
                chunk_arg = 30.0

        model_name = self.model_spinner.text.strip() or DEFAULT_MODEL
        auto_rename_enabled = bool(self.auto_rename_chk.active)
        
        # Connect active voice samples from Sample Library
        selected_samples = self.get_active_reference_samples()

        # UI state locking & Cancel button activation
        self.is_processing = True
        self.cancel_event.clear()

        self.start_btn.disabled = False
        self.start_btn.text = "Stop / Cancel Transcription"
        self.start_btn.background_color = get_color_from_hex("#DC2626")

        self.browse_audio_btn.disabled = True
        self.upload_doc_btn.disabled = True
        self.record_meeting_btn.disabled = True
        
        self.preview_transcript_btn.disabled = True
        self.preview_transcript_btn.opacity = 0.4
        self.preview_notes_btn.disabled = True
        self.preview_notes_btn.opacity = 0.4
        self.notes_btn.disabled = True
        self.notes_btn.opacity = 0.4
        self.save_transcript_btn.disabled = True
        self.save_transcript_btn.opacity = 0.4
        self.save_notes_btn.disabled = True
        self.save_notes_btn.opacity = 0.4

        self.current_transcript = ""
        self.current_meeting_notes = ""
        self.current_topic_slug = ""
        self.company_name = ""
        self.meeting_type = ""
        self.person_names = []
        self.source_audio_name = audio_file.name
        self.source_audio_path = str(audio_file.resolve())
        self.current_title = audio_file.stem
        self.recording_date = get_file_recording_date(audio_file)
        self.progress_bar.value = 0.0
        self.progress_bar.max = 1.0

        mode_desc = " [Auto-Rename Active]" if auto_rename_enabled else ""
        ref_desc = f" [{len(selected_samples)} Voice Sample(s)]" if selected_samples else ""
        self.live_action_lbl.text = f"Processing '{audio_file.name}' with {model_name}...{mode_desc}{ref_desc}"
        self.live_action_lbl.color = get_color_from_hex("#38BDF8")
        self.display_placeholder_message(f"=== Processing Audio: {audio_file.name} ===\nSplitting audio chunks & sending to Gemini {model_name}...\nVoice samples active: {len(selected_samples)}\nClick 'Stop / Cancel' at any time to abort cleanly.\nLive progress is shown above.")

        threading.Thread(
            target=self._transcription_worker,
            args=(audio_file, model_name, chunk_arg, auto_rename_enabled, selected_samples),
            daemon=True
        ).start()

    def cancel_transcription(self):
        if not self.is_processing:
            return
        self.cancel_event.set()
        self.start_btn.disabled = True
        self.live_action_lbl.text = "Cancelling transcription... Halting background tasks..."
        self.live_action_lbl.color = get_color_from_hex("#F59E0B")
        print("[User Action] Transcription cancellation requested by user.", flush=True)

    def _on_transcription_cancelled(self):
        self.is_processing = False
        self.start_btn.disabled = False
        self.start_btn.text = "Start Transcription"
        self.start_btn.background_color = get_color_from_hex("#2563EB")
        self.browse_audio_btn.disabled = False
        self.upload_doc_btn.disabled = False
        self.record_meeting_btn.disabled = False
        self.progress_bar.value = 0.0
        self.live_action_lbl.text = "Transcription stopped / cancelled by user."
        self.live_action_lbl.color = get_color_from_hex("#F59E0B")
        self.display_placeholder_message("=== Transcription Cancelled ===\nProcess was cleanly interrupted by user.\nReady to start a new session.")
        print("[Transcription] Cancelled cleanly without errors.", flush=True)

    def _transcription_worker(self, audio_file: Path, model_name: str, chunk_arg, auto_rename: bool, reference_samples: list[dict]):
        try:
            res = run_transcription_pipeline(
                audio_path=audio_file,
                model=model_name,
                chunk_minutes=chunk_arg,
                auto_save=False,
                reference_samples=reference_samples,
                log_callback=self.log_status,
                status_callback=self.update_live_status,
                cancel_event=self.cancel_event,
                api_call_callback=lambda: Clock.schedule_once(lambda dt: self.on_api_call_increment())
            )

            if self.cancel_event.is_set():
                Clock.schedule_once(lambda dt: self._on_transcription_cancelled())
                return

            # Silent auto-save draft the moment raw transcript is produced!
            draft_data = {
                "source_audio_path": str(audio_file.resolve()),
                "source_audio_name": audio_file.name,
                "title": res.get("title", audio_file.stem),
                "transcript": res.get("transcript", ""),
                "meeting_notes": "",
                "metadata": res.get("metadata", {}),
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "completed": False
            }
            save_draft_session(draft_data)

            # Automatic cleanup on success only
            intermediate_files = res.get("intermediate_files", [])
            delete_intermediate_files(intermediate_files)

            # Auto Rename & Auto-Save flow if checked
            auto_save_info = None
            if auto_rename and res.get("transcript"):
                if self.cancel_event.is_set():
                    Clock.schedule_once(lambda dt: self._on_transcription_cancelled())
                    return
                self.update_live_status("Auto-generating meeting notes & AI file names...", 1, 1)
                try:
                    notes_res = generate_meeting_notes(
                        res["transcript"],
                        model=model_name,
                        cancel_event=self.cancel_event,
                        api_call_callback=lambda: Clock.schedule_once(lambda dt: self.on_api_call_increment())
                    )
                    notes_text = notes_res.get("notes", "")
                    topic_slug = notes_res.get("topic_slug", "")
                    company_name = notes_res.get("company_name", "")
                    meeting_type = notes_res.get("meeting_type", "")
                    person_names = notes_res.get("person_names", [])

                    # Update draft with notes
                    draft_data["meeting_notes"] = notes_text
                    save_draft_session(draft_data)

                    rec_date = get_file_recording_date(audio_file)
                    base_name = build_meeting_base_name(
                        date_str=rec_date,
                        company_name=company_name,
                        meeting_type=meeting_type,
                        person_names=person_names,
                        topic_slug=topic_slug,
                        fallback_title=audio_file.stem
                    )

                    out_setting = self.settings.get("default_output_folder", "").strip()
                    if out_setting and Path(out_setting).exists():
                        output_dir = Path(out_setting).resolve()
                    else:
                        output_dir = Path("output").resolve()
                    output_dir.mkdir(parents=True, exist_ok=True)
                    
                    transcript_path = output_dir / f"{base_name}_Transcript.docx"
                    save_transcript_docx(
                        output_path=transcript_path,
                        title=base_name,
                        merged_transcript=res["transcript"],
                        meeting_notes=None,
                        metadata=res["metadata"]
                    )

                    notes_path = output_dir / f"{base_name}_Notes.docx"
                    save_transcript_docx(
                        output_path=notes_path,
                        title=base_name,
                        merged_transcript=None,
                        meeting_notes=notes_text,
                        metadata=res["metadata"]
                    )

                    # Save both to history
                    hist_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    hist_t = {
                        "id": datetime.now().strftime("%Y%m%d_%H%M%S_1"),
                        "display_name": transcript_path.name,
                        "file_path": str(transcript_path.resolve()),
                        "date": hist_date,
                        "source_audio": audio_file.name,
                        "company_name": company_name or "N/A",
                        "meeting_type": meeting_type or "N/A",
                        "person_names": person_names or [],
                        "topic_slug": topic_slug or "N/A",
                        "duration": res["metadata"].get("duration", "N/A"),
                        "has_notes": False
                    }
                    hist_n = {
                        "id": datetime.now().strftime("%Y%m%d_%H%M%S_2"),
                        "display_name": notes_path.name,
                        "file_path": str(notes_path.resolve()),
                        "date": hist_date,
                        "source_audio": audio_file.name,
                        "company_name": company_name or "N/A",
                        "meeting_type": meeting_type or "N/A",
                        "person_names": person_names or [],
                        "topic_slug": topic_slug or "N/A",
                        "duration": res["metadata"].get("duration", "N/A"),
                        "has_notes": True
                    }
                    save_history_entry(hist_n)
                    save_history_entry(hist_t)

                    if self.drive_chk.active:
                        upload_to_google_drive(transcript_path)
                        upload_to_google_drive(notes_path)

                    auto_save_info = {
                        "notes": notes_text,
                        "topic_slug": topic_slug,
                        "company_name": company_name,
                        "meeting_type": meeting_type,
                        "person_names": person_names,
                        "transcript_path": transcript_path,
                        "notes_path": notes_path
                    }
                except Exception as auto_err:
                    print(f"\n[Auto-Rename Warning]: Failed auto-naming: {auto_err}\n{traceback.format_exc()}", file=sys.stderr)
                    log_error_to_file(f"Auto-naming error: {auto_err}", traceback.format_exc())

            Clock.schedule_once(lambda dt: self._on_transcription_success(res, auto_save_info))
        except TranscriptionCancelledException:
            Clock.schedule_once(lambda dt: self._on_transcription_cancelled())
        except Exception as exc:
            err_trace = traceback.format_exc()
            print(f"\n[ERROR in _transcription_worker]: {exc}\n{err_trace}", file=sys.stderr)
            log_error_to_file(str(exc), err_trace)
            Clock.schedule_once(lambda dt: self._on_transcription_error(str(exc)))

    def _on_transcription_success(self, res: dict, auto_save_info: dict | None = None):
        try:
            self.is_processing = False
            self.start_btn.disabled = False
            self.start_btn.text = "Start Transcription"
            self.start_btn.background_color = get_color_from_hex("#2563EB")
            self.browse_audio_btn.disabled = False
            self.upload_doc_btn.disabled = False
            self.record_meeting_btn.disabled = False
            self.progress_bar.value = self.progress_bar.max

            self.current_transcript = res["transcript"]
            self.current_title = res["title"]
            self.current_metadata = res["metadata"]

            if auto_save_info:
                self.current_meeting_notes = auto_save_info["notes"]
                self.current_topic_slug = auto_save_info["topic_slug"]
                self.company_name = auto_save_info["company_name"]
                self.meeting_type = auto_save_info["meeting_type"]
                self.person_names = auto_save_info["person_names"]
                
                t_name = auto_save_info["transcript_path"].name
                self.live_action_lbl.text = f"Auto-saved: {t_name}"
                self.live_action_lbl.color = get_color_from_hex("#10B981")
                self.tab_btn_history.text = f"History ({len(load_history())})"
            else:
                self.live_action_lbl.text = "Transcription complete. Preview, generate notes, or save below."
                self.live_action_lbl.color = get_color_from_hex("#10B981")

            self._enable_post_processing_buttons()
            # Render natively in main window
            self.display_transcript_in_main_window(self.current_transcript, self.current_meeting_notes)

        except Exception as e:
            print(f"\n[ERROR in _on_transcription_success]: {e}\n{traceback.format_exc()}", file=sys.stderr)
            log_error_to_file(str(e), traceback.format_exc())

    def _on_transcription_error(self, err_msg: str):
        self.is_processing = False
        self.start_btn.disabled = False
        self.start_btn.text = "Start Transcription"
        self.start_btn.background_color = get_color_from_hex("#2563EB")
        self.browse_audio_btn.disabled = False
        self.upload_doc_btn.disabled = False
        self.record_meeting_btn.disabled = False
        self.progress_bar.value = 0.0

        # Plain, friendly user message (never raw Python traceback)
        clean_first_line = err_msg.strip().split("\n")[0][:120]
        self.live_action_lbl.text = f"Something went wrong - see log for details. (Error: {clean_first_line})"
        self.live_action_lbl.color = get_color_from_hex("#EF4444")
        self.display_placeholder_message(
            f"=== Transcription Notice ===\n"
            f"Something went wrong during transcription.\n"
            f"A technical error report has been written to the log.\n\n"
            f"Error summary: {clean_first_line}\n\n"
            f"To view full error details: Go to Settings -> 'View Error Log'."
        )

    def on_generate_notes_clicked(self, instance=None):
        if self.is_processing:
            return
        if not self.current_transcript or not self.current_transcript.strip():
            self.live_action_lbl.text = "No transcript available. Please transcribe an audio file or upload a transcript first."
            self.live_action_lbl.color = get_color_from_hex("#EF4444")
            return

        model_name = self.model_spinner.text.strip() or "gemini-3.5-flash-lite"
        self.notes_btn.text = "Generating..."
        self.notes_btn.background_color = get_color_from_hex("#D97706")
        self.live_action_lbl.text = f"Analyzing transcript & generating notes with {model_name}..."
        self.live_action_lbl.color = get_color_from_hex("#38BDF8")

        threading.Thread(
            target=self._notes_worker,
            args=(self.current_transcript, model_name),
            daemon=True
        ).start()

    def _notes_worker(self, transcript_text: str, model_name: str):
        try:
            res = generate_meeting_notes(
                transcript_text,
                model=model_name,
                api_call_callback=lambda: Clock.schedule_once(lambda dt: self.on_api_call_increment())
            )
            notes = res.get("notes", "")
            topic_slug = res.get("topic_slug", "")
            company_name = res.get("company_name", "")
            meeting_type = res.get("meeting_type", "")
            person_names = res.get("person_names", [])

            # Update draft with newly generated notes
            draft = load_draft_session() or {}
            draft["meeting_notes"] = notes
            save_draft_session(draft)

            Clock.schedule_once(lambda dt: self._on_notes_success(notes, topic_slug, company_name, meeting_type, person_names))
        except Exception as exc:
            err_trace = traceback.format_exc()
            print(f"\n[ERROR in _notes_worker]: {exc}\n{err_trace}", file=sys.stderr)
            log_error_to_file(str(exc), err_trace)
            Clock.schedule_once(lambda dt: self._on_notes_error(str(exc)))

    def _on_notes_success(self, notes: str, topic_slug: str, company_name: str, meeting_type: str, person_names: list[str]):
        try:
            self.current_meeting_notes = notes
            self.current_topic_slug = topic_slug
            self.company_name = company_name
            self.meeting_type = meeting_type
            self.person_names = person_names
            self.notes_btn.text = "Regenerate Notes"
            self._enable_post_processing_buttons()

            self.live_action_lbl.text = f"Notes generated! Review above, preview, or save below."
            self.live_action_lbl.color = get_color_from_hex("#10B981")
            # Update main window display with both meeting notes and transcript
            self.display_transcript_in_main_window(self.current_transcript, self.current_meeting_notes)
        except Exception as e:
            print(f"\n[ERROR in _on_notes_success]: {e}\n{traceback.format_exc()}", file=sys.stderr)
            log_error_to_file(str(e), traceback.format_exc())

    def _on_notes_error(self, err_msg: str):
        self.notes_btn.text = "Generate Notes"
        self._enable_post_processing_buttons()
        clean_first_line = err_msg.strip().split("\n")[0][:120]
        self.live_action_lbl.text = f"Something went wrong - see log for details. (Error: {clean_first_line})"
        self.live_action_lbl.color = get_color_from_hex("#EF4444")

    def save_as_dialog(self, mode: str = "transcript"):
        """
        Independent Save-As dialogs with unified paired auto-suggested filenames:
        Priority (Max 2 elements after Date):
        1. Company name
        2. Meeting type (e.g. Sales-Pitch, Client-Review)
        3. Person name(s) (if no company)
        4. Topic slug (fallback)

        Both Transcript and Notes share the exact same base name, differing only by suffix:
        - {base_name}_Transcript.docx
        - {base_name}_Notes.docx
        """
        if mode == "notes" and (not self.current_meeting_notes or not self.current_meeting_notes.strip()):
            self.live_action_lbl.text = "No meeting notes available to save. Click 'Generate Notes' first."
            self.live_action_lbl.color = get_color_from_hex("#EF4444")
            return
        if mode == "transcript" and (not self.current_transcript or not self.current_transcript.strip()):
            self.live_action_lbl.text = "No transcript available to save. Please transcribe or upload a transcript first."
            self.live_action_lbl.color = get_color_from_hex("#EF4444")
            return

        rec_date = self.recording_date or datetime.now().strftime("%Y-%m-%d")
        base_name = build_meeting_base_name(
            date_str=rec_date,
            company_name=self.company_name,
            meeting_type=self.meeting_type,
            person_names=self.person_names,
            topic_slug=self.current_topic_slug,
            fallback_title=self.current_title
        )

        if mode == "notes":
            default_fn = f"{base_name}_Notes.docx"
            dialog_title = "Save Meeting Notes As..."
        else:
            default_fn = f"{base_name}_Transcript.docx"
            dialog_title = "Save Diarized Transcript As..."

        def _ask_save():
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                root.update_idletasks()
                
                out_setting = self.settings.get("default_output_folder", "").strip()
                if out_setting and Path(out_setting).exists():
                    output_default_dir = Path(out_setting).resolve()
                else:
                    output_default_dir = Path("output").resolve()
                output_default_dir.mkdir(parents=True, exist_ok=True)
                
                save_file_path = filedialog.asksaveasfilename(
                    parent=root,
                    title=dialog_title,
                    initialdir=str(output_default_dir),
                    initialfile=default_fn,
                    defaultextension=".docx",
                    filetypes=[
                        ("Word Documents (*.docx)", "*.docx"),
                        ("All Files", "*.*")
                    ]
                )
                root.destroy()
                
                if save_file_path:
                    target_path = Path(save_file_path)
                    Clock.schedule_once(lambda dt: self._perform_save(target_path, mode))
            except Exception as e:
                print(f"[Error opening save dialog]: {e}", file=sys.stderr)

        threading.Thread(target=_ask_save, daemon=True).start()

    def _perform_save(self, target_path: Path, mode: str):
        try:
            transcript_to_save = self.current_transcript if mode == "transcript" else None
            notes_to_save = self.current_meeting_notes if mode == "notes" else None

            save_transcript_docx(
                output_path=target_path,
                title=target_path.stem,
                merged_transcript=transcript_to_save,
                meeting_notes=notes_to_save,
                metadata=self.current_metadata
            )
            self.output_docx_path = target_path

            # Record in history index
            history_entry = {
                "id": datetime.now().strftime("%Y%m%d_%H%M%S"),
                "display_name": target_path.name,
                "file_path": str(target_path.resolve()),
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_audio": self.source_audio_name or target_path.stem,
                "company_name": self.company_name or "N/A",
                "meeting_type": self.meeting_type or "N/A",
                "person_names": self.person_names or [],
                "topic_slug": self.current_topic_slug or "N/A",
                "duration": self.current_metadata.get("duration", "N/A"),
                "has_notes": bool(mode == "notes" or self.current_meeting_notes)
            }
            save_history_entry(history_entry)

            # Auto-upload to Google Drive if checked
            drive_msg = ""
            if self.drive_chk.active:
                upload_to_google_drive(target_path)
                drive_msg = " | Drive upload requested."

            self.tab_btn_history.text = f"History ({len(load_history())})"
            self.live_action_lbl.text = f"Saved: {target_path.name}{drive_msg}"
            self.live_action_lbl.color = get_color_from_hex("#10B981")

        except Exception as e:
            print(f"\n[ERROR in _perform_save]: {e}\n{traceback.format_exc()}", file=sys.stderr)
            self.live_action_lbl.text = f"Error saving document: {e}"
            self.live_action_lbl.color = get_color_from_hex("#EF4444")

class TranscriberApp(App):
    def build(self):
        self.title = "Bilingual Meeting Transcriber - Gemini Pipeline"
        return TranscriberGUI()

if __name__ == "__main__":
    TranscriberApp().run()
