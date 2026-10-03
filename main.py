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

def main():
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
