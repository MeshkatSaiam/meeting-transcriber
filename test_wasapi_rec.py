import sounddevice as sd
wasapi_info = next((a for a in sd.query_hostapis() if 'WASAPI' in a['name'] or 'Windows Audio Session API' in a['name']), None)
if wasapi_info:
    out_idx = wasapi_info['default_output_device']
    print('Trying loopback on', out_idx)
    try:
        with sd.InputStream(device=out_idx, channels=1, samplerate=48000, extra_settings=sd.WasapiSettings(loopback=True)) as stream:
            print('Success 1ch!')
    except Exception as e:
        print('1ch failed:', e)
        try:
            with sd.InputStream(device=out_idx, channels=2, samplerate=48000, extra_settings=sd.WasapiSettings(loopback=True)) as stream:
                print('Success 2ch!')
        except Exception as e2:
            print('2ch failed:', e2)

