# Third-Party Notices & Licenses

likesuno uses and interfaces with several open-source components and models:

---

### 1. audio.cpp
- **Repository**: [https://github.com/ShugoAI/audio.cpp](https://github.com/ShugoAI/audio.cpp)
- **Tested Commit**: `456c8e780211132ac480dce25b54962baa70c523`
- **Copyright**: © 2026 ShugoAI LLC
- **License**: Apache License 2.0
- **Note**: Not bundled in this repository. Checked out and built locally via `scripts/build_runtime.ps1`.

---

### 2. YuE2 Weights (GGUF)
- **Repository**: [audio-cpp/Yue2-3B-GGUF](https://huggingface.co/audio-cpp/Yue2-3B-GGUF)
- **License**: Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)
- **Note**: Model weights are strictly for non-commercial research and personal use. Not bundled in this repository. Downloaded via `scripts/download_models.ps1` or `scripts/download_models.py`.

---

### 3. faster-whisper / OpenAI Whisper
- **Repository**: [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- **License**: MIT License
- **Note**: Used for vocal segmentation and lyrics alignment. Model weights are automatically downloaded from Hugging Face upon first vocal analysis request.

---

### 4. FFmpeg
- **Software**: FFmpeg (used for audio encoding, MP3 conversion, and MP4 vertical video generation)
- **License**: LGPL 2.1+ / GPL 2.0+ (depending on build configuration)
- **Note**: External system dependency. Expected to be installed and available on system PATH.
