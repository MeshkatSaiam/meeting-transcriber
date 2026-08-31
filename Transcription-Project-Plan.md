# Bilingual Meeting Transcription Pipeline — FINAL PLAN

## The core problem this solves
Bangla/English meeting transcription with reliable speaker diarization,
without manual chunk-merging, output-token limits, or label drift across
long recordings — accessible from both Windows and Android, from one
codebase.

---

## LOCKED ARCHITECTURE

| Piece | Choice | Why |
|---|---|---|
| Client (Windows + Android) | **Kivy** (Python) → PyInstaller (.exe) + Buildozer (.apk) | One codebase, stays in Python, no new language to learn under time pressure |
| Android recording | **Fossify Voice Recorder** (existing app, unmodified) | Already solves widget + reliable background recording + call-interruption — the hardest problem in the whole project, for free |
| Windows recording | **Built into the Kivy app** | Mic direct; system/meeting audio (Zoom/Meet) via VB-Cable loopback |
| Processing | **Client-side, no server, for v1** | Both apps call Gemini directly (ffmpeg + API calls on-device). No GPU/heavy compute needed, so no server is actually required — this was only ever needed to host local diarization, which we dropped |
| Diarization | **Test Gemini-only first.** Fallback: Speechmatics API (480 min/month free) | Your "চলনসই" comment suggests Gemini alone may already be good enough — don't add Speechmatics complexity unless testing proves it's needed |
| Transcription | **Gemini API**, chunked, parallel calls | Proven to work well for you already |
| Output | **python-docx** — transcript, meeting minutes, notes, action items, draft email | Matches your existing doc conventions |

### Deferred, not dropped: backend server
**Oracle Cloud Always Free was evaluated and set aside for now** — signup
is fast, but the free VM's provisioning is a known source of
unpredictable delay (capacity errors, sometimes requiring retries over
hours/days), which fails the "easy and fast" bar. It only exists to
solve one specific problem: Android can't reliably run a 15-30 min job
in the background without the app staying open. Revisit this **only if**
that limitation actually proves annoying in real use — don't build it
preemptively for a problem not yet confirmed.

### v1 accepted limitation
- **Android:** app must stay open/foreground during processing (no
  server to hand the job off to). Acceptable trade for skipping all
  server/hosting complexity in v1.
- **Windows:** no such limitation — a laptop can run the job in the
  background fine without a server.
- **API key lives on-device** (both apps) instead of on a server. Fine
  for personal devices; worth revisiting only if the app is ever shared
  beyond you and your wife.
- **No auto-synced history across devices** — each device keeps its own
  local transcript history unless synced via the shared Drive folder
  (which is being built anyway for output upload).

### Explicitly dropped / deferred
- **pyannote / local GPU diarization** — not needed now that the backend
  has no GPU (Oracle free tier is CPU-only), and Gemini/Speechmatics
  cover this via API instead
- **Native Android recording in Kivy** — Fossify already solves this;
  rebuilding it in Kivy would reintroduce the exact call-interruption
  risk we specifically avoided
- **Home server + Tailscale** — replaced by Oracle Cloud hosting
  (more reliable than depending on home power/internet uptime)
- **WhatsApp integration** — not mandatory; genuinely risky (no clean
  personal-account API). Telegram bot is the easy equivalent if wanted
  later.
- **Home-screen widget** — needs native Kotlin regardless of framework;
  not needed anyway since Fossify already has one

---

## ENVIRONMENT & TOOLS

### Coding agent
**Google Antigravity** (free, Gemini 3.5-based agent-first IDE) — used
in place of Claude Code, which isn't available. Capable of the same
core job: reading/editing files across the project, running terminal
commands, iterating on real builds. Two known caveats to expect, not
blockers: reported stability issues on long sessions (context drift,
occasional freezes) and no current MCP support (not needed for this
stack anyway). Download: antigravity.google

### Local toolchain (install before Phase 0 build session)
- Python 3.10 or 3.11
- Git
- ffmpeg (already installed from earlier work)
- Kivy + Buildozer (Buildozer needs WSL on Windows)
- PyInstaller (for the Windows .exe build)
- VB-Cable (for Windows system-audio/meeting capture — only needed
  once Phase 2's Windows recorder is built)

### Accounts / keys needed
- Gemini API key (AI Studio, free tier)
- Google Cloud project with Drive API enabled + OAuth credentials
  (needed for the Drive auto-upload feature)
- Speechmatics account (only if Phase 1 testing shows Gemini-only
  diarization isn't sufficient)
- Oracle Cloud — not needed for v1, see Phase 4

---

## APP FEATURE SPEC

### Inputs
- Local file import (from disk)
- Windows: built-in recorder (mic + optional Zoom/Meet system-audio
  capture via VB-Cable)
- Android: receive file from Fossify (manual share/upload for v1;
  Share-to integration is a nice-to-have, not required for v1)

### Engine settings (saved, switchable)
- Diarization: Gemini-only (default, test first) / Speechmatics
  (fallback if Gemini's isn't accurate enough)
- Custom vocabulary/glossary field (CNRD, BIRDI, SICIP, PID, etc.) fed
  into the Gemini prompt so technical terms transcribe correctly

### Voice sample matching
- Library of reference clips (you, your wife, regular attendees),
  reusable across meetings, checkbox per meeting
- Manual override: mark a transcript region as a specific speaker,
  feeds back as a correction
- After a manual correction, offer to save that clip as a new reusable
  reference

### Processing
- Gemini chunk transcription: parallel calls, not sequential
- Diarization runs once on the full unsplit audio (no chunking needed
  for this step)
- Merge by timestamp — deterministic script logic, not AI
- Resume on failure — a dead job at minute 40 doesn't restart from zero

### Outputs
- Full diarized transcript (.docx)
- Meeting minutes (attendees, agenda, decisions)
- Notes / key findings summary
- Action items with owners
- Draft email to attendees (generated, not auto-sent)
- Local transcript history/library (searchable)
- **Auto file naming** — output filename generated from the Phase-2
  summary (short topic string, e.g. "CNRD-Gun-Control-Review") plus the
  recording date, e.g. `2026-08-28_CNRD-Gun-Control-Review.docx`.
  Recording date source: audio file metadata if present, else file
  creation date, else prompt once at upload time.
- **Google Drive auto-upload** — after the docx is generated, push it
  to a designated Drive folder automatically (Drive API + OAuth,
  one-time auth per device). Applies to both Windows and Android
  clients, not just Android.

---

## BUILD PLAN

### Phase 0 — Before you start (setup, no building yet)
- [ ] Python 3.10 or 3.11 installed
- [ ] Kivy + Buildozer installed (Buildozer needs WSL on Windows — set
      this up now, it's the most likely early friction point)
- [ ] Gemini API key (free tier, from AI Studio)
- [ ] Oracle Cloud account created, Always Free VM provisioned
      (retry if you hit an "out of capacity" error — known, temporary)
- [ ] 2-3 real meeting recordings ready, including your toughest
      diarization case so far
- [ ] Git set up for version control from day one

### Phase 1 — Core pipeline (script, no UI yet)
- [ ] Validate Gemini-only diarization against real audio — decide if
      Speechmatics is actually needed based on evidence, not assumption
- [ ] ffmpeg convert -> chunk -> parallel Gemini transcription -> merge ->
      docx, running as a plain script first, calling Gemini directly
      (no backend — confirmed not needed for v1)

### Phase 2 — Kivy client
- [ ] Windows: file picker, engine settings, recorder (mic first, VB-Cable
      capture after), progress view, results view
- [ ] Package Windows build with PyInstaller
- [ ] Android: same screens minus recording (Fossify owns that), file
      upload flow instead. Accept v1 limitation: app must stay open
      during processing (no server to offload to)
- [ ] Package Android build with Buildozer, sideload and test on your
      actual phone
- [ ] Auto file naming (topic + recording date) on every generated doc
- [ ] Google Drive auto-upload of finished docs (both clients)

### Phase 3 — Polish (later sessions)
- [ ] Voice-sample library + manual correction flow
- [ ] Custom vocabulary field
- [ ] Meeting-minutes/notes/email output formatting
- [ ] Transcript history/library
- [ ] Fossify "Share to" integration (nice-to-have)
- [ ] Telegram bot (nice-to-have, if wanted)

### Phase 4 — Only if needed
- [ ] Revisit backend (FastAPI + hosting) **only if** Android's
      stay-open-while-processing limitation proves genuinely annoying
      in real use. Re-evaluate Oracle Cloud vs. alternatives at that
      point rather than assuming Oracle now.

---

## Open Questions
1. Windows recorder: build VB-Cable/system-audio capture into Phase 2,
   or start mic-only and add it later? (Doesn't block anything else.)
2. Confirm diarization engine choice after Phase 1's real-audio test.
