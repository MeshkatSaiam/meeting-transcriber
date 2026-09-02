import os
import sys
import threading
import time
import winsound
os.environ['KIVY_NO_ARGS'] = '1'

from gui import TranscriberApp
from kivy.clock import Clock

def simulate_recording(dt):
    app = TranscriberApp.get_running_app()
    gui = app.root
    
    # 1. Select the loopback device
    gui.refresh_audio_input_devices()
    loopback_id = None
    for dev in gui.input_devices_map:
        if dev.get("backend") == "pyaudiowpatch":
            loopback_id = dev["id"]
            break
            
    if not loopback_id:
        print("No loopback found for test!")
        app.stop()
        return
        
    gui.selected_capture_devices = {loopback_id}
    print(f"Selected device: {loopback_id}")
    
    # 2. Start recording
    gui.toggle_record_meeting()
    
    # 3. Play audio
    def play_sound():
        print("Playing beep...")
        winsound.Beep(440, 2000)
    
    threading.Thread(target=play_sound).start()
    
    # 4. Stop recording after 3 seconds
    def stop_rec(dt2):
        gui.toggle_record_meeting()
        app.stop()
        
    Clock.schedule_once(stop_rec, 3.0)

if __name__ == '__main__':
    app = TranscriberApp()
    Clock.schedule_once(simulate_recording, 1.0)
    app.run()
