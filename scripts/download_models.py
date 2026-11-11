"""likesuno - YuE2 GGUF 가중치 다운로더 (huggingface_hub + hf_transfer)

- hf_transfer: Rust 기반 멀티커넥션 다운로더 (파일당 8연결 청크 병렬)
- 재시도: 네트워크 오류 시 무한 재시도(느린 인터넷 대응), 완료 파일은 스킵
- 대상: audio-cpp/Yue2-3B-GGUF (Q8_0 4.26GB / Q4_0 2.67GB / VAE F16+F32 / sidecars)
"""
import os
import sys
import time
from pathlib import Path

os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
os.environ["HF_HUB_DISABLE_XET"] = "1"   # xet 백엔드 비활성 -> 고전 CDN 경로 (hf_transfer 호환)

from huggingface_hub import snapshot_download  # noqa: E402

REPO = "audio-cpp/Yue2-3B-GGUF"
DEST = (os.environ.get("LIKESUNO_MODELS")
        or str(Path(__file__).resolve().parents[1] / "models" / "Yue2-3B-GGUF"))
PATTERNS = [
    "yue2-3b-q8_0.gguf",
    "yue2-3b-q4_0.gguf",
    "yue2-vae-f16.gguf",
    "yue2-vae-f32.gguf",
    "sidecars/*",
]

# 간단한 옵션: skipq4 / q4only
argv = [a.lower() for a in sys.argv[1:]]
if "skipq4" in argv and "q4only" not in argv:
    PATTERNS.remove("yue2-3b-q4_0.gguf")
elif "q4only" in argv:
    PATTERNS = ["yue2-3b-q4_0.gguf", "sidecars/*"]

print("대상:", PATTERNS, flush=True)

attempt = 0
while True:
    attempt += 1
    try:
        print(f"[{time.strftime('%H:%M:%S')}] 시도 #{attempt}: {REPO} -> {DEST}", flush=True)
        path = snapshot_download(
            repo_id=REPO,
            local_dir=DEST,
            allow_patterns=PATTERNS,
            max_workers=2,          # 동시 파일 수 (파일당 내부 병렬은 hf_transfer가 담당)
        )
        print(f"[{time.strftime('%H:%M:%S')}] DONE: {path}", flush=True)
        break
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[{time.strftime('%H:%M:%S')}] 오류: {type(exc).__name__}: {exc}", flush=True)
        print("60초 후 재시도… (느린 인터넷 대응 무한 재시도)", flush=True)
        time.sleep(60)
