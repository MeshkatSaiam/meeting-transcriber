import pyaudiowpatch as pyaudio
import numpy as np
import time

p = pyaudio.PyAudio()
wasapi_info = p.get_default_wasapi_loopback()

frames = []

def pa_callback(in_data, frame_count, time_info, status):
    if in_data:
        frames.append(in_data)
    return (None, pyaudio.paContinue)

stream = p.open(format=pyaudio.paFloat32,
    channels=wasapi_info['maxInputChannels'],
    rate=int(wasapi_info['defaultSampleRate']),
    input=True,
    input_device_index=wasapi_info['index'],
    stream_callback=pa_callback)

print("Recording loopback for 3 seconds...")
stream.start_stream()
time.sleep(3.0)
stream.stop_stream()
stream.close()
p.terminate()

if len(frames) == 0:
    print("No frames captured!")
else:
    raw_data = b"".join(frames)
    audio = np.frombuffer(raw_data, dtype=np.float32)
    channels = wasapi_info['maxInputChannels']
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    
    raw_peak = float(np.max(np.abs(audio)))
    raw_rms = float(np.sqrt(np.mean(audio**2)))
    print(f"Captured {len(audio)} samples.")
    print(f"[Loopback Diagnostic] Raw Peak: {raw_peak:.4f} | RMS: {raw_rms:.4f}")
