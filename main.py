"""
Meeting Transcriber — Unified Entry Point
Automatically chooses Mobile UI on Android and Desktop UI on PC/Mac.
Pass `--mobile` on desktop to simulate mobile phone view.
"""

import sys
import os

# Check for --mobile flag before Kivy initializes
IS_MOBILE_REQUESTED = "--mobile" in sys.argv
if IS_MOBILE_REQUESTED:
    sys.argv.remove("--mobile")
    os.environ["KIVY_NO_ARGS"] = "1"

from kivy.utils import platform

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

def main():
    setup_ota_path()
    is_mobile_target = (platform == "android") or IS_MOBILE_REQUESTED

    if is_mobile_target:
        if platform != "android":
            from kivy.config import Config
            Config.set("graphics", "width", "400")
            Config.set("graphics", "height", "750")
            Config.set("graphics", "resizable", "1")

        from mobile_gui import MobileTranscriberApp
        MobileTranscriberApp().run()
    else:
        from gui import TranscriberApp
        TranscriberApp().run()

if __name__ == "__main__":
    main()
