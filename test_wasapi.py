import sounddevice as sd
print('Default devices:', sd.default.device)
for i, api in enumerate(sd.query_hostapis()):
    print(i, api['name'])
wasapi_info = next((a for a in sd.query_hostapis() if 'WASAPI' in a['name'] or 'Windows Audio Session API' in a['name']), None)
if wasapi_info:
    out_idx = wasapi_info['default_output_device']
    print('WASAPI Default Output:', out_idx)
    if out_idx >= 0: print(sd.query_devices(out_idx)['name'])

