"""
Meeting Transcriber — Unified Entry Point
Automatically chooses Mobile UI on Android and Desktop UI on PC/Mac.
Pass `--mobile` on desktop to simulate mobile phone view.
"""

import sys
import os
from kivy.utils import platform

def request_android_permissions():
    """Request runtime permissions required for reading audio files on Android."""
    if platform == "android":
        try:
            from android.permissions import request_permissions, Permission
            request_permissions([
                Permission.READ_EXTERNAL_STORAGE,
                Permission.WRITE_EXTERNAL_STORAGE,
                Permission.INTERNET,
            ])
            print("[Android Permissions]: Requested storage and internet permissions.", flush=True)
        except Exception as e:
            print(f"[Android Permissions Warning]: {e}", file=sys.stderr, flush=True)

def setup_ota_path():
    """Ensure any downloaded OTA updates from GitHub are prioritized over bundled code."""
    from pathlib import Path
    try:
        possible_dirs = [
            Path.home() / ".meeting_transcriber" / "ota_updates",
            Path("/data/data/org.meshkat.meetingtranscriber/files/ota_updates"),
            Path("/data/user/0/org.meshkat.meetingtranscriber/files/ota_updates"),
        ]
        for d in possible_dirs:
            if d.exists() and (d / "mobile_gui.py").exists():
                sys.path.insert(0, str(d))
                print(f"[OTA Sync]: Prioritizing updated scripts from {d}", flush=True)
                break
    except Exception as e:
        print(f"[OTA Warning]: {e}", file=sys.stderr, flush=True)

def main():
    setup_ota_path()
    is_mobile_target = (platform == "android") or ("--mobile" in sys.argv)

    if is_mobile_target:
        # If simulating mobile on desktop, configure a phone aspect ratio window
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

if __name__ == "__main__":
    main()
