import os
import sys
# Set env var so we can run headless or we can just let it render an empty window quickly
os.environ['KIVY_NO_ARGS'] = '1'
from gui import TranscriberApp
from kivy.clock import Clock

def simulate(dt):
    app = TranscriberApp.get_running_app()
    # Mock settings
    app.root.settings = {"gemini_api_key": ""}
    # Trigger start transcription which triggers the missing dialog
    app.root.start_transcription()
    
    def check_dlg(dt2):
        if hasattr(app.root, 'missing_api_dialog') and app.root.missing_api_dialog:
            print("SUCCESS: Dialog opened without crashing!")
        else:
            print("FAILED: Dialog not opened.")
        app.stop()
        
    Clock.schedule_once(check_dlg, 1.0)

if __name__ == '__main__':
    app = TranscriberApp()
    Clock.schedule_once(simulate, 1.0)
    app.run()
