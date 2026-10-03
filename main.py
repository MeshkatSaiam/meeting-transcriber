"""
Meeting Transcriber — Unified Entry Point
Automatically chooses Mobile UI on Android and Desktop UI on PC/Mac.
Pass `--mobile` on desktop to simulate mobile phone view.
"""

import sys
import os

# Check for --mobile flag before Kivy parses sys.argv
IS_MOBILE_REQUESTED = "--mobile" in sys.argv
if IS_MOBILE_REQUESTED:
    sys.argv.remove("--mobile")
os.environ["KIVY_NO_ARGS"] = "1"

import traceback
from kivy.app import App
from kivy.utils import platform

def request_android_permissions():
    """Request runtime permissions required for reading audio files on Android."""
    if platform == "android":
        try:
            from android.permissions import request_permissions, Permission
            perms = [
                Permission.INTERNET,
                Permission.READ_EXTERNAL_STORAGE,
                Permission.WRITE_EXTERNAL_STORAGE,
            ]
            if hasattr(Permission, "READ_MEDIA_AUDIO"):
                perms.append(Permission.READ_MEDIA_AUDIO)
            else:
                perms.append("android.permission.READ_MEDIA_AUDIO")

            def on_permissions_callback(permissions, grant_results):
                print(f"[Android Permissions]: {permissions} -> {grant_results}", flush=True)

            request_permissions(perms, on_permissions_callback)
            print("[Android Permissions]: Storage and media permissions requested.", flush=True)
        except Exception as e:
            print(f"[Android Permissions Warning]: {e}", file=sys.stderr, flush=True)

def setup_ota_path():
    """Ensure any downloaded OTA updates from GitHub are prioritized over bundled code."""
    from pathlib import Path
    try:
        possible_dirs = []
        if platform == "android":
            possible_dirs.extend([
                Path("/data/data/org.meshkat.meetingtranscriber/files/ota_updates"),
                Path("/data/user/0/org.meshkat.meetingtranscriber/files/ota_updates"),
            ])
            env_private = os.environ.get("ANDROID_PRIVATE")
            if env_private:
                possible_dirs.insert(0, Path(env_private) / "ota_updates")
        else:
            possible_dirs.append(Path.home() / ".meeting_transcriber" / "ota_updates")

        for d in possible_dirs:
            if d.exists() and (d / "mobile_gui.py").exists():
                sys.path.insert(0, str(d))
                print(f"[OTA Sync]: Prioritizing updated scripts from {d}", flush=True)
                break
    except Exception as e:
        print(f"[OTA Warning]: {e}", file=sys.stderr, flush=True)

class ErrorApp(App):
    """Fallback UI showing readable stack traces on the phone screen instead of silent crashes."""
    def __init__(self, error_message: str, **kwargs):
        super().__init__(**kwargs)
        self.error_message = error_message

    def build(self):
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.label import Label
        from kivy.uix.textinput import TextInput
        from kivy.uix.button import Button
        from kivy.core.clipboard import Clipboard
        from kivy.utils import get_color_from_hex

        layout = BoxLayout(orientation="vertical", padding=16, spacing=10)

        title = Label(
            text="Meeting Transcriber — Startup Error",
            font_size="17sp",
            bold=True,
            color=get_color_from_hex("#EF4444"),
            size_hint_y=None,
            height=32
        )
        layout.add_widget(title)

        desc = Label(
            text="The app encountered an error. Traceback details:",
            font_size="12sp",
            color=get_color_from_hex("#94A3B8"),
            size_hint_y=None,
            height=24
        )
        layout.add_widget(desc)

        txt = TextInput(
            text=self.error_message,
            readonly=True,
            font_size="11sp",
            background_color=get_color_from_hex("#0F172A"),
            foreground_color=get_color_from_hex("#F8FAFC")
        )
        layout.add_widget(txt)

        btn_box = BoxLayout(orientation="horizontal", size_hint_y=None, height=46, spacing=10)
        btn_copy = Button(text="Copy Error", bold=True, background_color=get_color_from_hex("#2563EB"))
        btn_copy.bind(on_release=lambda *a: Clipboard.copy(self.error_message))
        btn_box.add_widget(btn_copy)

        btn_close = Button(text="Exit App", bold=True, background_color=get_color_from_hex("#475569"))
        btn_close.bind(on_release=lambda *a: sys.exit(1))
        btn_box.add_widget(btn_close)

        layout.add_widget(btn_box)
        return layout

def main():
    try:
        setup_ota_path()
        is_mobile_target = (platform == "android") or IS_MOBILE_REQUESTED

        if is_mobile_target:
            if platform != "android":
                from kivy.config import Config
                Config.set("graphics", "width", "400")
                Config.set("graphics", "height", "750")
                Config.set("graphics", "resizable", "1")

            request_android_permissions()

            from mobile_gui import MobileTranscriberApp
            MobileTranscriberApp().run()
        else:
            from gui import TranscriberApp
            TranscriberApp().run()
    except Exception as e:
        err_msg = traceback.format_exc()
        print(f"[FATAL APPLICATION ERROR]:\n{err_msg}", file=sys.stderr, flush=True)
        if platform == "android" or "--mobile" in sys.argv:
            try:
                ErrorApp(err_msg).run()
            except Exception:
                sys.exit(1)
        else:
            raise

if __name__ == "__main__":
    main()
