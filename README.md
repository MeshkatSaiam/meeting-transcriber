# Meeting Transcriber & Note Generator

A standalone Windows application that records meetings, automatically transcribes them using the Gemini API, and generates structured meeting notes in a beautifully styled KivyMD user interface.

## 🚀 How to Run the App (No Installation Required!)

This application is fully **portable**. You do not need to install Python, configure environments, or run setup wizards!

1. Download the `MeetingTranscriber.zip` file.
2. **Extract/Unzip** the folder to your computer (e.g., to your Desktop or Documents folder).
   * *Important:* Do not just open the `.zip` and double-click the `.exe` from inside the zip viewer. You must extract the folder first.
3. Open the extracted folder and double-click `MeetingTranscriber.exe`.
4. On the first launch, go to the **Settings** tab and enter your **Gemini API Key**.
5. Start recording!

> **Note:** A black command-prompt window will appear in the background when the app launches. This is normal and acts as a diagnostic log. Just leave it open while using the app!

## ✨ Key Features
- **One-Click Recording:** Directly records system/microphone audio into a clean `.wav` file.
- **Smart Transcription:** Leverages Google's Gemini Flash/Pro models to accurately transcribe meetings.
- **Notes Generation:** Automatically summarizes the transcript into organized Meeting Notes.
- **Bilingual Support:** Handles English and Bengali (Bangla) transcriptions seamlessly.
- **Audio Pre-processing:** Automatically normalizes audio volume for clearer speech recognition using built-in FFmpeg.
- **Export to DOCX:** Save your notes directly to Microsoft Word format.

## ⚙️ Configuration & Files
All of your data stays private and local on your machine:
- `app_settings.json`: Stores your API key and preferences locally.
- `database.db`: A local SQLite database keeping track of your meeting history.
- `recordings/`: The folder where all your raw `.wav` audio files and `.docx` notes are securely saved.

## 🛠️ For Developers (Building from Source)
If you want to run the python script directly or build a new `.exe`:
1. Ensure Python 3.11+ is installed.
2. Install dependencies: `kivy`, `kivymd`, `sounddevice`, `soundfile`, `google-generativeai`, `python-docx`, `ffmpeg-python` (Ensure FFmpeg is installed system-wide).
3. Run directly: `python gui.py`
4. Build the executable: `python -m PyInstaller --noconfirm MeetingTranscriber.spec`
