[app]

# (str) Title of your application
title = Meeting Transcriber

# (str) Package name
package.name = meetingtranscriber

# (str) Package domain (needed for android/ios packaging)
package.domain = org.meshkat

# (str) Source code where the main.py lives
source.dir = .

# (list) Source files to include (let empty to include all the files)
source.include_exts = py,png,jpg,kv,atlas,json,txt

# (list) List of directory to exclude
source.exclude_dirs = tests, bin, venv, .venv, .git, .github, recordings, converted, output, Audio file, Manual test, voice_samples

# (list) List of exclusions using pattern matching
source.exclude_patterns = license,*.pyc,*.pyo,*.spec

# (str) Application versioning
version = 1.0.0

# (list) Application requirements
# comma separated e.g. requirements = sqlite3,kivy
requirements = python3,kivy==2.3.1,google-genai,python-docx,python-dotenv,urllib3,certifi,chardet,idna,requests,typing-extensions,pydantic

# (str) Supported orientation (one of landscape, sensorLandscape, portrait or all)
orientation = portrait

# (list) Permissions
android.permissions = INTERNET,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,READ_MEDIA_AUDIO

# (int) Target Android API, should be as high as possible.
android.api = 34

# (int) Minimum API your APK / AAB will support.
android.minapi = 26

# (str) Android NDK version to use
android.ndk = 25b

# (bool) If True, then skip trying to update the Android sdk
# This can be useful to avoid excess Internet downloads or save time
# when an update is due and you just want to test/build your package
android.skip_update = False

# (bool) If True, then automatically accept SDK license
# agreements. This is intended for automation only.
android.accept_sdk_license = True

# (str) The Android arch to build for, choices: armeabi-v7a, arm64-v8a, x86, x86_64
# In modern Android devices, arm64-v8a is the standard.
android.archs = arm64-v8a, armeabi-v7a

# (bool) Allow backup
android.allow_backup = True

# (str) Android logcat filters to use
android.logcat_filters = *:S python:D

[buildozer]

# (int) Log level (0 = error only, 1 = info, 2 = debug (with command output))
log_level = 2

# (int) Display warning if buildozer is run as root (0 = False, 1 = True)
warn_on_root = 1
