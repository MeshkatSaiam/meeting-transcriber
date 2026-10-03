"""
Pure-Python Gemini REST Client
Provides a zero-dependency, pure-Python client for Google Gemini API and Gemini File API.
Works on Android (python-for-android), Linux, Windows, macOS without requiring
C-extensions, Rust toolchain, or compiled pydantic-core binaries.
"""

import os
import json
import time
import mimetypes
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Iterator, List, Optional, Union

API_BASE_URL = "https://generativelanguage.googleapis.com"
UPLOAD_BASE_URL = "https://generativelanguage.googleapis.com/upload"

class UploadedFileState:
    def __init__(self, name: str = "ACTIVE"):
        self.name = name

class UploadedFile:
    def __init__(self, name: str, uri: str, mime_type: str = "audio/mp3", state: str = "ACTIVE", display_name: str = ""):
        self.name = name
        self.uri = uri
        self.mime_type = mime_type
        self.state = UploadedFileState(state)
        self.display_name = display_name
        self.error = None

    def __repr__(self):
        return f"<UploadedFile name={self.name} uri={self.uri} state={self.state.name}>"

class GenerateContentResponse:
    def __init__(self, text: str, raw_response: Optional[dict] = None):
        self.text = text
        self.raw_response = raw_response or {}

    def __repr__(self):
        snippet = self.text[:50].replace("\n", " ") if self.text else ""
        return f"<GenerateContentResponse text='{snippet}...'>"

class RestFilesAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def upload(self, file: Union[str, Path], mime_type: Optional[str] = None) -> UploadedFile:
        p = Path(file)
        if not p.exists():
            raise FileNotFoundError(f"File not found for upload: {file}")

        file_size = p.stat().st_size
        if not mime_type:
            guessed_type, _ = mimetypes.guess_type(str(p))
            mime_type = guessed_type or "audio/mp3"

        display_name = p.name

        # 1. Start Resumable Upload
        init_url = f"{UPLOAD_BASE_URL}/v1beta/files?uploadType=resumable&key={self.api_key}"
        metadata = json.dumps({"file": {"display_name": display_name}}).encode("utf-8")
        headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
            "User-Agent": "MeetingTranscriber-Mobile/1.0"
        }

        req = urllib.request.Request(init_url, data=metadata, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                upload_url = resp.headers.get("x-goog-upload-url") or resp.headers.get("upload-url")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Failed to initiate file upload (HTTP {e.code}): {err_body}")

        if not upload_url:
            raise RuntimeError("Gemini File API did not return an upload URL.")

        # 2. Upload Binary File Content
        with open(p, "rb") as f:
            file_bytes = f.read()

        upload_headers = {
            "Content-Length": str(len(file_bytes)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
            "User-Agent": "MeetingTranscriber-Mobile/1.0"
        }

        req2 = urllib.request.Request(upload_url, data=file_bytes, headers=upload_headers, method="POST")
        try:
            with urllib.request.urlopen(req2, timeout=120) as resp2:
                result = json.loads(resp2.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Failed to upload file content (HTTP {e.code}): {err_body}")

        f_data = result.get("file", {})
        name = f_data.get("name", "")
        uri = f_data.get("uri", "")
        state = f_data.get("state", "ACTIVE")
        return UploadedFile(name=name, uri=uri, mime_type=mime_type, state=state, display_name=display_name)

    def get(self, name: str) -> UploadedFile:
        clean_name = name.lstrip("/")
        url = f"{API_BASE_URL}/v1beta/{clean_name}?key={self.api_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "MeetingTranscriber-Mobile/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Failed to get file status (HTTP {e.code}): {err_body}")

        return UploadedFile(
            name=data.get("name", name),
            uri=data.get("uri", ""),
            mime_type=data.get("mimeType", "audio/mp3"),
            state=data.get("state", "ACTIVE"),
            display_name=data.get("displayName", "")
        )

    def delete(self, name: str) -> bool:
        clean_name = name.lstrip("/")
        url = f"{API_BASE_URL}/v1beta/{clean_name}?key={self.api_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "MeetingTranscriber-Mobile/1.0"}, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

class RestModelsAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _normalize_model_name(self, model: str) -> str:
        if not model.startswith("models/"):
            return f"models/{model}"
        return model

    def _build_payload(self, contents: Union[List[Any], str, Any], config: Optional[dict] = None) -> dict:
        parts = []
        if isinstance(contents, list):
            items = contents
        else:
            items = [contents]

        for item in items:
            if isinstance(item, str):
                parts.append({"text": item})
            elif isinstance(item, UploadedFile):
                parts.append({
                    "fileData": {
                        "mimeType": item.mime_type or "audio/mp3",
                        "fileUri": item.uri or f"{API_BASE_URL}/v1beta/{item.name}"
                    }
                })
            elif hasattr(item, "uri") and hasattr(item, "mime_type"):
                parts.append({
                    "fileData": {
                        "mimeType": getattr(item, "mime_type", "audio/mp3"),
                        "fileUri": getattr(item, "uri")
                    }
                })
            elif isinstance(item, dict):
                parts.append(item)
            else:
                parts.append({"text": str(item)})

        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": parts
                }
            ]
        }

        if config:
            gen_config = {}
            if "response_mime_type" in config:
                gen_config["responseMimeType"] = config["response_mime_type"]
            if "temperature" in config:
                gen_config["temperature"] = config["temperature"]
            if gen_config:
                payload["generationConfig"] = gen_config

        return payload

    def generate_content(self, model: str, contents: Union[List[Any], str], config: Optional[dict] = None) -> GenerateContentResponse:
        norm_model = self._normalize_model_name(model)
        url = f"{API_BASE_URL}/v1beta/{norm_model}:generateContent?key={self.api_key}"
        payload = self._build_payload(contents, config)
        data_bytes = json.dumps(payload).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "MeetingTranscriber-Mobile/1.0"
        }

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gemini API Error (HTTP {e.code}): {err_body}")

        # Extract text from candidate parts
        extracted_text = ""
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for p in parts:
                if "text" in p:
                    extracted_text += p["text"]

        return GenerateContentResponse(text=extracted_text, raw_response=data)

    def generate_content_stream(self, model: str, contents: Union[List[Any], str], config: Optional[dict] = None) -> Iterator[GenerateContentResponse]:
        """
        Yields chunk responses. For robustness, executes generate_content and yields the full response chunk.
        """
        resp = self.generate_content(model=model, contents=contents, config=config)
        yield resp

class GeminiRestClient:
    """Drop-in pure-Python replacement for google.genai.Client."""
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("GEMINI_API_KEY must be provided.")
        self.api_key = api_key
        self.files = RestFilesAPI(api_key)
        self.models = RestModelsAPI(api_key)
