"""likesuno - YuE2 기반 로컬 음악 생성 백엔드 (FastAPI)

Suno처럼 스타일+가사를 넣으면 audio.cpp(양자화 GGUF, CUDA)로 곡을 생성한다.
torch 미사용 - 추론은 C++ 네이티브 audio.cpp 가 담당 (torch venv 와 분리).
"""
from __future__ import annotations

import base64
import html
import json
import unicodedata
from difflib import SequenceMatcher
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]  # D:\likesuno
CONFIG_PATH = ROOT / "config" / "likesuno.json"

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

# drawtext 한글 폰트 (ffmpeg 필터용 이스케이프: C\:/Windows/... + 따옴표)
def _find_font() -> str | None:
    for p in (Path(r"C:\Windows\Fonts\malgun.ttf"),
              Path(r"C:\Windows\Fonts\NanumGothic.ttf"),
              Path(r"C:\Windows\Fonts\gulim.ttc")):
        if p.exists():
            return f"'C\\:/Windows/Fonts/{p.name}'"
    return None

_FONT = _find_font()

with open(CONFIG_PATH, encoding="utf-8") as f:
    CONFIG: dict[str, Any] = json.load(f)

JOBS_DIR = ROOT / CONFIG["jobs_dir"]
LOG_DIR = ROOT / CONFIG["logs_dir"]
MODEL_DIR = (ROOT / CONFIG["model_dir"]).resolve()
LEGACY_OUT_DIR = ROOT / "output"  # 구버전 저장 위치 (하위 호환 서빙용)


def _resolve_dir(value: str, base: Path) -> Path:
    p = Path(str(value))
    return p if p.is_absolute() else base / p


# 생성물·입력은 data_root(E:\likesuno), 프로그램·기록은 ROOT(D:\likesuno)에 저장
DATA_ROOT = Path(CONFIG.get("data_root") or ROOT)
IN_DIR = _resolve_dir(CONFIG.get("input_dir", "input"), DATA_ROOT)
OUT_DIR = _resolve_dir(CONFIG.get("output_dir", "output"), DATA_ROOT)
OUT_WAV = OUT_DIR / "wav"
OUT_TAG = OUT_DIR / "wav_tag"
OUT_MP3 = OUT_DIR / "mp3"
OUT_MP4 = OUT_DIR / "mp4"
OUT_IMG = OUT_DIR / "images"
TMP_DIR = DATA_ROOT / "_tmp"


def _init_dirs() -> None:
    for d in (IN_DIR, OUT_WAV, OUT_TAG, OUT_MP3, OUT_MP4, OUT_IMG,
              TMP_DIR, JOBS_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


try:
    _init_dirs()
except Exception as _exc:  # noqa: BLE001  (E: 없음 등 → D: 로 폴백)
    print(f"[likesuno] 저장 폴더 생성 실패({_exc}) → D:\\likesuno 로 폴백")
    DATA_ROOT = ROOT
    IN_DIR = ROOT / "input"
    OUT_DIR = ROOT / "output"
    OUT_WAV = OUT_DIR / "wav"
    OUT_TAG = OUT_DIR / "wav_tag"
    OUT_MP3 = OUT_DIR / "mp3"
    OUT_MP4 = OUT_DIR / "mp4"
    OUT_IMG = OUT_DIR / "images"
    TMP_DIR = DATA_ROOT / "_tmp"
    _init_dirs()

# ---- 배경 이미지 생성 (SD 3.5 Large Turbo Q4 GGUF, 별도 torch venv 서브프로세스) ----
# torch/diffusers 는 본앱 venv(무torch) 에 없으므로, torch+diffusers 가 설치된
# 별도 파이썬으로 sd_worker.py 를 서브프로세스 호출한다 (격리 원칙 유지).
# config/likesuno.json 의 sd_venv_python / sd_model 을 채워야 켜진다.
# 비워두면 이미지 생성만 꺼지고 음악/MP4/태그 등 나머지는 그대로 동작한다.
_sd_venv_raw = str(CONFIG.get("sd_venv_python") or "").strip()
SD_VENV_PY: Path | None = Path(_sd_venv_raw) if _sd_venv_raw else None
_sd_model_raw = str(CONFIG.get("sd_model") or "").strip()
SD_MODEL = (Path(_sd_model_raw) if _sd_model_raw
            else ROOT / "models" / "sd3.5_large_turbo-q4_k_4_0.gguf")
SD_STEPS = int(CONFIG.get("sd_steps") or 8)
SD_WORKER = Path(__file__).resolve().parent / "sd_worker.py"

# ---------------------------------------------------------------- 파일명 및 텍스트 도우미
def _normalize_text(s: str) -> str:
    """따옴표 및 특수문자 정규화: 스마트 쿼트(둥근 따옴표)를 일반 ASCII 문자로 통일."""
    if not s:
        return ""
    # 스마트 따옴표 및 악센트 -> 일반 ASCII 따옴표로 통일
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'").replace("´", "'")
    s = s.replace("“", '"').replace("”", '"').replace("„", '"')
    return s


def _safe_fname(title: str) -> str:
    """제목 → 안전한 파일명 (윈도우 금지문자만 _ 로 치환, 공백은 유지)."""
    s = _normalize_text(str(title or ""))
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s)
    s = re.sub(r"\s+", " ", s).strip().strip(" .")
    if len(s) > 80:
        s = s[:80].rstrip(" .")
    return s or "untitled"


def _unique_fname(title: str) -> str:
    """같은 제목이 이미 있으면 _2, _3 ... 을 붙여 중복을 피한다."""
    base = _safe_fname(title)
    cand, n = base, 1
    while n < 200:
        taken = ((IN_DIR / f"{cand}.txt").exists()
                 or (OUT_WAV / f"{cand}.wav").exists()
                 or (OUT_TAG / f"{cand}.wav").exists()
                 or (OUT_MP3 / f"{cand}.mp3").exists()
                 or (OUT_MP4 / f"{cand}.mp4").exists())
        if not taken:
            return cand
        n += 1
        cand = f"{base}_{n}"
    return f"{base}_{int(time.time()) % 100000}"


def _fname(job: dict[str, Any]) -> str:
    """작업의 파일 기준 이름 (제목 기반, 없으면 job_id)."""
    return str(job.get("fname") or job.get("id") or "untitled")


def _media_url(kind: str, name: str) -> str:
    return f"/media/{kind}/{quote(name)}"


def _wav_original(job: dict[str, Any]) -> Path:
    return OUT_WAV / f"{_fname(job)}.wav"


def _wav_tagged(job: dict[str, Any]) -> Path:
    return OUT_TAG / f"{_fname(job)}.wav"


def _wav_current(job: dict[str, Any]) -> Path:
    """재생/변환에 쓸 현재 wav (태그본이 있으면 태그본)."""
    t = _wav_tagged(job)
    return t if t.exists() else _wav_original(job)


def _write_input_file(job: dict[str, Any]) -> None:
    """곡별 입력 기록(제목/스타일/가사)을 E:\likesuno\input\{이름}.txt 로 저장."""
    try:
        opts = job.get("options", {}) or {}
        text = (f"제목: {job.get('title', '')}\n\n"
                f"[스타일]\n{opts.get('style', '')}\n\n"
                f"[가사]\n{opts.get('lyrics', '')}\n")
        (IN_DIR / f"{_fname(job)}.txt").write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


_BIN_DIR: Optional[str] = None
_bi = ROOT / "config" / "build_info.txt"
if _bi.exists():
    for line in _bi.read_text().splitlines():
        if line.startswith("BIN_DIR="):
            _BIN_DIR = line.split("=", 1)[1].strip()
if not _BIN_DIR:
    cands = list((ROOT / "runtime" / "audio.cpp" / "build").glob("*/audiocpp_cli.exe"))
    _BIN_DIR = str(cands[0].parent) if cands else None

CLI = Path(_BIN_DIR) / "audiocpp_cli.exe" if _BIN_DIR else None

app = FastAPI(title="likesuno")


@app.middleware("http")
async def _no_cache_html(request, call_next):
    """HTML/JS/CSS는 항상 최신본을 받도록 캐시 금지
    (UI 업데이트가 안 보이는 문제 방지 — 코드 수정 후 서버 재시작만 하면 바로 반영)."""
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if ct.startswith(("text/html", "application/javascript", "text/css")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# やめる.bat 이 읽는 PID 파일 (서버 종료 도우미)
try:
    _PID_DIR = ROOT / "tmp"
    _PID_DIR.mkdir(exist_ok=True)
    (_PID_DIR / "likesuno.pid").write_text(str(os.getpid()), encoding="ascii")
except Exception:  # noqa: BLE001
    pass

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_QUEUE: list[str] = []
_WORKER_STARTED = False


def _job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _save_job(job: dict[str, Any]) -> None:
    _job_path(job["id"]).write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_persisted_jobs() -> None:
    for p in sorted(JOBS_DIR.glob("*.json")):
        if p.name.endswith(".vocal.json"):
            continue
        try:
            job = json.loads(p.read_text(encoding="utf-8-sig"))
            if not isinstance(job, dict) or "id" not in job:
                continue
            # 구버전 /audio/ 링크를 /media/ 로 이전 (Range 스트리밍)
            for k in ("wav", "mp3", "mp4", "mp4_landscape"):
                if job.get(k, "").startswith("/audio/"):
                    job[k] = job[k].replace("/audio/", "/media/", 1)
            # 디스크에 wav가 이미 잘 생성되어 있는데 error로 잘못 표기된 경우 자동 복구
            if job.get("status") == "error":
                fname = _fname(job)
                w1 = OUT_WAV / f"{fname}.wav"
                w2 = OUT_WAV / f"{_normalize_text(fname)}.wav"
                if w1.exists() or w2.exists():
                    job["status"] = "done"
                    if not w1.exists() and w2.exists():
                        job["fname"] = _normalize_text(fname)
                    job["wav"] = _media_url("wav", f"{_fname(job)}.wav")
                    job.pop("error", None)
                    _save_job(job)
            _JOBS[job["id"]] = job
        except Exception:
            continue


def _build_command(job: dict[str, Any]) -> list[str]:
    opts = job["options"]
    q_file = CONFIG["quantization_map"].get(
        opts.get("quantization", "q8_0"), "yue2-3b-q8_0.gguf")
    cmd: list[str] = [
        str(CLI),
        "--task", "gen",
        "--family", "yue2",
        "--model", str(MODEL_DIR),
        "--backend", CONFIG["backend"],
        "--threads", str(CONFIG["threads"]),
        "--seed", str(opts["seed"]),
        "--out", str(OUT_WAV / f"{_fname(job)}.wav"),
        "--out-dir", str(JOBS_DIR / job["id"]),
        "--log",
        "--lyrics", opts["lyrics"],
        "--request-option", f"style={opts['style']}",
        "--request-option", f"cot={opts['cot']}",
        "--request-option", f"num_inference_steps={opts['num_inference_steps']}",
        "--session-option", f"yue2.model_gguf={q_file}",
        "--session-option", f"yue2.vae_gguf={CONFIG['defaults']['vae_gguf']}",
    ]
    gs = opts.get("guidance_scale")
    if gs is not None and str(gs).strip() != "":
        cmd += ["--request-option", f"guidance_scale={gs}"]
    for k, v in CONFIG.get("session_options", {}).items():
        cmd += ["--session-option", f"{k}={v}"]
    return cmd


def _tail_log(path: Path, n: int | None = None) -> str:
    if not path.exists():
        return ""
    n = n or CONFIG.get("max_log_tail", 60)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def _worker() -> None:
    while True:
        with _JOBS_LOCK:
            job_id = _QUEUE[0] if _QUEUE else None
        if job_id is None:
            time.sleep(0.5)
            continue
        with _JOBS_LOCK:
            job = _JOBS[job_id]
        log_path = LOG_DIR / f"{job_id}.log"
        try:
            job["status"] = "running"
            job["started_at"] = time.time()
            _save_job(job)
            with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
                proc = subprocess.run(
                    _build_command(job), stdout=lf, stderr=subprocess.STDOUT,
                    cwd=str(CLI.parent), creationflags=subprocess.CREATE_NO_WINDOW,
                )
            job["returncode"] = proc.returncode
            wav = OUT_WAV / f"{_fname(job)}.wav"
            if not wav.exists():
                # 윈도우 파일시스템이 따옴표를 아스키 따옴표 등으로 치환했을 경우 스마트 감지
                alt_wav = OUT_WAV / f"{_normalize_text(_fname(job))}.wav"
                if alt_wav.exists():
                    job["fname"] = _normalize_text(_fname(job))
                    wav = alt_wav
            if proc.returncode == 0 and wav.exists():
                job["status"] = "done"
                job["wav"] = _media_url("wav", f"{_fname(job)}.wav")
                # MP3 자동 변환 (ffmpeg, 192k)
                try:
                    mp3 = OUT_MP3 / f"{_fname(job)}.mp3"
                    r = subprocess.run(
                        [FFMPEG, "-y", "-i", str(wav),
                         "-codec:a", "libmp3lame", "-b:a", "192k",
                         str(mp3)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=300, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if r.returncode == 0 and mp3.exists():
                        job["mp3"] = _media_url("mp3", f"{_fname(job)}.mp3")
                    else:
                        job["mp3_error"] = f"ffmpeg exit {r.returncode}"
                except Exception as exc2:  # noqa: BLE001
                    job["mp3_error"] = str(exc2)
                abc = JOBS_DIR / job_id / "score.abc"
                if abc.exists():
                    job["score"] = f"/jobs/{job_id}/score.abc"
            else:
                job["status"] = "error"
                job["error"] = _tail_log(log_path) or f"exit code {proc.returncode}"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)
        job["finished_at"] = time.time()
        _save_job(job)
        with _JOBS_LOCK:
            if _QUEUE and _QUEUE[0] == job_id:
                _QUEUE.pop(0)


def _ensure_worker() -> None:
    global _WORKER_STARTED
    if not _WORKER_STARTED and CLI and CLI.exists():
        threading.Thread(target=_worker, daemon=True).start()
        _WORKER_STARTED = True


# ---------------------------------------------------------------- API
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "cli": str(CLI) if CLI else None,
        "cli_exists": bool(CLI and CLI.exists()),
        "model_dir": str(MODEL_DIR),
        "model_ready": MODEL_DIR.exists() and any(MODEL_DIR.glob("*.gguf")),
        "queue": len(_QUEUE),
    }


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return CONFIG


@app.get("/api/gpu")
def gpu_info() -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.strip()
        name, total, used = [x.strip() for x in out.splitlines()[0].split(",")]
        return {"name": name, "vram_total": total, "vram_used": used}
    except Exception:
        return {}


def _title_from_lyrics(lyrics: str) -> str:
    for line in lyrics.splitlines():
        line = line.strip()
        if line and not re.match(r"^\[.*\]$", line):
            return line[:40]
    return "Untitled"


@app.post("/api/generate")
async def generate(payload: dict[str, Any]) -> JSONResponse:
    _ensure_worker()
    if not CLI or not CLI.exists():
        raise HTTPException(
            503, "audio.cpp 미빌드. scripts\\build_runtime.ps1 실행 후 재시작하세요.")
    lyrics = _normalize_text((payload.get("lyrics") or "").strip())
    style = _normalize_text((payload.get("style") or "").strip())
    if not lyrics:
        raise HTTPException(400, "가사를 입력하세요.")
    if not style:
        raise HTTPException(400, "스타일을 입력하세요.")
    if not any(MODEL_DIR.glob("*.gguf")):
        raise HTTPException(
            503, "모델이 없습니다. scripts\\download_models.ps1 을 먼저 실행하세요.")

    defaults = CONFIG["defaults"]
    cot = payload.get("cot", defaults["cot"])
    if cot not in ("off", "melody", "full"):
        raise HTTPException(400, "cot 는 off|melody|full 중 하나여야 합니다.")
    try:
        steps = int(payload.get("num_inference_steps",
                                defaults["num_inference_steps"]))
        assert steps > 0
    except Exception:
        raise HTTPException(400, "num_inference_steps 는 양의 정수여야 합니다.")
    quant = payload.get("quantization", defaults["quantization"])
    if quant not in CONFIG["quantization_map"]:
        raise HTTPException(
            400, f"quantization 은 {list(CONFIG['quantization_map'])} 중 하나여야 합니다.")
    seed_raw = payload.get("seed")
    seed = (int(seed_raw) if seed_raw not in (None, "", "random")
            else int.from_bytes(os.urandom(6), "big") % (2**31))
    gs = payload.get("guidance_scale")

    raw_title = (payload.get("title") or "").strip() or _title_from_lyrics(lyrics)
    norm_title = _normalize_text(raw_title)

    job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    job: dict[str, Any] = {
        "id": job_id, "status": "queued",
        "created_at": time.time(),
        "options": {
            "lyrics": lyrics, "style": style, "cot": cot,
            "num_inference_steps": steps, "quantization": quant,
            "seed": seed, "guidance_scale": gs,
        },
        "title": norm_title,
    }
    job["fname"] = _unique_fname(job["title"])
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        _QUEUE.append(job_id)
    _save_job(job)
    _write_input_file(job)
    return {"id": job_id, "seed": seed, "status": "queued"}


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return sorted(_JOBS.values(), key=lambda j: j["created_at"], reverse=True)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    return job


@app.get("/api/jobs/{job_id}/log")
def job_log(job_id: str) -> dict[str, str]:
    return {"tail": _tail_log(LOG_DIR / f"{job_id}.log")}


@app.get("/lyrics/{job_id}")
def lyrics_page(job_id: str) -> HTMLResponse:
    """가사창: 새 탭에서 곡 제목 + 가사를 보여주는 페이지."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    title = html.escape(str(job.get("title") or "(제목 없음)"))
    lyrics = html.escape(str((job.get("options") or {}).get("lyrics") or ""))
    page = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — 가사</title>
<style>
  body {{ background:#14141f; color:#e8e8f2; font-family:"Malgun Gothic","Segoe UI",sans-serif;
         max-width:760px; margin:0 auto; padding:36px 22px 60px; line-height:1.9; }}
  h1 {{ font-size:22px; color:#fff; border-bottom:1px solid #2a2a3d; padding-bottom:14px; margin-bottom:6px; }}
  .sub {{ color:#8a8aa3; font-size:12px; margin-bottom:22px; }}
  pre {{ white-space:pre-wrap; font-family:inherit; font-size:16px; color:#dcdcea; }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="sub">likesuno · 가사</div>
  <pre>{lyrics}</pre>
</body>
</html>"""
    return HTMLResponse(page)


@app.post("/api/jobs/{job_id}/rename")
async def rename_job(job_id: str, request: Request) -> dict[str, Any]:
    """곡 제목 변경: 제목 + 파일명(wav·wav_tag·mp3·mp4·미리듣기) + input 기록 + 재생 URL 갱신."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    if job.get("status") != "done":
        raise HTTPException(400, "완성된 곡만 제목을 바꿀 수 있습니다.")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    new_title = _normalize_text(" ".join(str(body.get("title") or "").split()))
    if not new_title:
        raise HTTPException(400, "새 제목을 입력하세요.")
    if len(new_title) > 80:
        new_title = new_title[:80]
    old_fname = _fname(job)
    if new_title == str(job.get("title") or ""):
        return {"ok": True, "title": new_title, "fname": old_fname}
    new_fname = _safe_fname(new_title)
    others = {_fname(j) for jid, j in _JOBS.items() if jid != job_id}
    n = 1
    while new_fname in others and n < 100:
        n += 1
        new_fname = f"{_safe_fname(new_title)}_{n}"
    moves: list[tuple[Path, Path]] = []

    def _plan(src: Path, dst: Path) -> None:
        if src.exists() and src != dst and not dst.exists():
            moves.append((src, dst))

    _plan(OUT_WAV / f"{old_fname}.wav", OUT_WAV / f"{new_fname}.wav")
    _plan(OUT_TAG / f"{old_fname}.wav", OUT_TAG / f"{new_fname}.wav")
    _plan(OUT_TAG / f"{old_fname}.preview.wav",
          OUT_TAG / f"{new_fname}.preview.wav")
    _plan(OUT_MP3 / f"{old_fname}.mp3", OUT_MP3 / f"{new_fname}.mp3")
    _plan(OUT_MP4 / f"{old_fname}.mp4", OUT_MP4 / f"{new_fname}.mp4")
    _plan(OUT_MP4 / f"{old_fname}_landscape.mp4",
          OUT_MP4 / f"{new_fname}_landscape.mp4")
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            shutil.move(str(src), str(dst))
            done.append((src, dst))
    except Exception as exc:  # noqa: BLE001
        for src, dst in reversed(done):  # 실패 시 원상 복구
            try:
                shutil.move(str(dst), str(src))
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(500, f"파일 이름 변경 실패: {exc}")
    job["title"] = new_title
    job["fname"] = new_fname
    _write_input_file(job)  # input\{새이름}.txt 생성
    old_txt = IN_DIR / f"{old_fname}.txt"
    if old_txt.exists() and old_txt != (IN_DIR / f"{new_fname}.txt"):
        try:
            old_txt.unlink()
        except Exception:  # noqa: BLE001
            pass
    if (OUT_TAG / f"{new_fname}.wav").exists():
        job["wav"] = _media_url("wav_tag", f"{new_fname}.wav")
    elif (OUT_WAV / f"{new_fname}.wav").exists():
        job["wav"] = _media_url("wav", f"{new_fname}.wav")
    else:
        job.pop("wav", None)
    if (OUT_MP3 / f"{new_fname}.mp3").exists():
        job["mp3"] = _media_url("mp3", f"{new_fname}.mp3")
    else:
        job.pop("mp3", None)
    if (OUT_MP4 / f"{new_fname}.mp4").exists():
        job["mp4"] = _media_url("mp4", f"{new_fname}.mp4")
    else:
        job.pop("mp4", None)
    if (OUT_MP4 / f"{new_fname}_landscape.mp4").exists():
        job["mp4_landscape"] = _media_url("mp4", f"{new_fname}_landscape.mp4")
    else:
        job.pop("mp4_landscape", None)
    _save_job(job)
    return {"ok": True, "title": new_title, "fname": new_fname}


_MEDIA_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg",
                ".mp4": "video/mp4", ".png": "image/png",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp"}
_MEDIA_DIRS = {"wav": OUT_WAV, "wav_tag": OUT_TAG,
               "mp3": OUT_MP3, "mp4": OUT_MP4, "image": OUT_IMG}


def _stream_file(path: Path, request: Request) -> StreamingResponse:
    """Range 스트리밍 (1초 멈춤 해결: 브라우저 seek 지원)."""
    size = path.stat().st_size
    ctype = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    range_h = request.headers.get("range")

    def _iter(start: int, end: int, chunk: int = 1 << 20):
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                data = f.read(min(chunk, left))
                if not data:
                    break
                yield data
                left -= len(data)

    if range_h:
        m = re.match(r"bytes=(\d*)-(\d*)", range_h)
        if m:
            s0, s1 = m.groups()
            start = int(s0) if s0 else max(size - (int(s1) if s1 else 0), 0)
            end = int(s1) if s1 else size - 1
            end = min(end, size - 1)
            if start > end:
                raise HTTPException(416, "잘못된 Range")
            headers = {"Content-Range": f"bytes {start}-{end}/{size}",
                       "Accept-Ranges": "bytes",
                       "Content-Length": str(end - start + 1)}
            return StreamingResponse(_iter(start, end), status_code=206,
                                     media_type=ctype, headers=headers)
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(size)}
    return StreamingResponse(_iter(0, size - 1), media_type=ctype,
                             headers=headers)


@app.get("/media/{kind}/{name:path}")
def media(kind: str, name: str, request: Request) -> StreamingResponse:
    """곡 파일 서빙: /media/wav|wav_tag|mp3|mp4|image/{파일명} (Range 스트리밍).

    image 는 배경 이미지용으로 job_id/타임스탬프/파일명 하위경로를 허용한다.
    """
    base = _MEDIA_DIRS.get(kind)
    if base is None:
        raise HTTPException(404, "지원하지 않는 형식")
    # 경로 탈출/숨김파일 방지 (image 외 kind 는 하위경로 불가)
    if "\\" in name or ".." in name or name.startswith("."):
        raise HTTPException(404, "잘못된 경로")
    if "/" in name and kind != "image":
        raise HTTPException(404, "잘못된 경로")
    path = base / name
    if not path.exists():
        raise HTTPException(404, "파일 없음")
    return _stream_file(path, request)


@app.get("/media/{legacy}")
def media_legacy(legacy: str, request: Request) -> StreamingResponse:
    """구버전 경로 호환: D:\\likesuno\\output 에 남아있는 파일 서빙."""
    if "/" in legacy or "\\" in legacy or ".." in legacy:
        raise HTTPException(404, "잘못된 경로")
    if Path(legacy).suffix.lower() not in _MEDIA_TYPES:
        raise HTTPException(404, "지원하지 않는 형식")
    path = LEGACY_OUT_DIR / legacy
    if not path.exists():
        raise HTTPException(404, "파일 없음")
    return _stream_file(path, request)


def _fix_mojibake(s: str) -> str:
    """UTF-8이 latin-1/cp1252로 잘못 디코딩된 깨진 문자(예: â€œ)를 원래 문자로 복구 시도.
    한글 등 cp1252에 없는 문자는 그대로 두고, 깨진 문자가 연속된 구간만 골라 복구한다."""

    def _fix_run(run: str) -> str:
        if len(run) < 2 or not any(ord(c) >= 0x80 for c in run):
            return run
        try:
            b = run.encode("cp1252")
        except UnicodeEncodeError:
            return run
        try:
            fixed = b.decode("utf-8")
        except UnicodeDecodeError:
            # 2차 시도: 끊긴 시퀀스는 버리더라도 읽을 수 있는 부분 복구.
            # 다국어(한자/한글 영역)가 실제로 나왔을 때만 채택 → 실제 라틴 문자(café 등) 보호
            fixed = b.decode("utf-8", errors="ignore")
            if not any(ord(c) >= 0x2E80 for c in fixed):
                return run
        if fixed != run and not any(ord(c) < 32 for c in fixed):
            return fixed
        return run

    out: list[str] = []
    buf: list[str] = []

    def _flush() -> None:
        if buf:
            out.append(_fix_run("".join(buf)))
            buf.clear()

    for ch in s:
        try:
            ch.encode("cp1252")
            buf.append(ch)  # cp1252 인코딩 가능 → 깨진 구간 후보
        except UnicodeEncodeError:
            _flush()        # 한글 등 → 후보 구간 마감 후 그대로 출력
            out.append(ch)
    _flush()
    return "".join(out)


# 자막에 남겨둘 기호 (♪ ♩ ♫ 등 음악 기호)
_KEEP_SYMBOL_RANGES = ((0x2669, 0x266F),)


def _clean_lyric(s: str) -> str:
    """자막용 문자 정리: 깨진 문자 복구 + 따옴표 정규화 + 이모지/기호/서식문자 제거.
    한글·영문·숫자·문장부호·♪ 같은 음악 기호는 유지."""
    s = _normalize_text(_fix_mojibake(str(s or "")))
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if ch in ("\n", " "):
            out.append(ch)
            continue
        if o < 0x20:  # 제어문자
            continue
        if 0x200B <= o <= 0x200F or 0x202A <= o <= 0x202E or o == 0xFEFF:  # 제로폭/방향 서식
            continue
        if 0xFE00 <= o <= 0xFE0F:  # 이모지 서식 선택자(피부톤 등)
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Co", "Cs", "Cn", "So", "Sk"):
            if not any(a <= o <= b for a, b in _KEEP_SYMBOL_RANGES):
                continue
        out.append(ch)
    return "".join(out).strip()


def _ff_esc(s: str) -> str:
    # drawtext용 이스케이프. ' 는 이스케이프 대신 아포스트로피(')로 치환
    # (ffmpeg 필터 파서가 \' 이스케이프를 제대로 못 읽어 force_style 파싱이 깨지는 버그 방지)
    s = _clean_lyric(s)
    s = "".join(c for c in s if ord(c) <= 0xFFFF)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return (s.replace("\\", "\\\\").replace(":", "\\:")
             .replace("'", "\u2019").replace("\n", " ").strip())


def _lyric_segments(lines: list[str], synced: list[dict[str, Any]],
                    dur: float) -> list[dict[str, Any]]:
    """자막 세그먼트 생성. 싱크(Whisper) 있으면 실측 타이밍, 없으면 균등 분할."""
    if synced:
        return [{"start": float(s["start"]), "end": float(s["end"]),
                 "text": _clean_lyric(str(s["text"])) or "♪"}
                for s in synced[:300]]
    n = min(len(lines), 300)
    per = dur / n if n else dur
    segs: list[dict[str, Any]] = []
    for i, ln in enumerate(lines[:n]):
        t = _clean_lyric(ln) or "♪"
        segs.append({"start": i * per,
                     "end": (i + 1) * per if i < n - 1 else dur,
                     "text": t})
    return segs


def _ass_ts(sec: float) -> str:
    sec = max(sec, 0.0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


# ---- 자막 자동 줄바꿈/크기 맞춤 (긴 가사가 좌우로 잘리지 않게) ----
_ASS_MARGIN = 50                       # 좌우 여백(px)
_ASS_MAXW = 1080 - _ASS_MARGIN * 2     # 자막 최대 폭 = 980px
_FONT_FILE = Path(r"C:\Windows\Fonts\malgun.ttf")
_PIL_FONTS: dict[int, Any] = {}


def _pil_font(size: int) -> Any:
    """폭/높이 측정용 폰트(PIL). 없으면 None → 근사 계산으로 대체."""
    if size not in _PIL_FONTS:
        try:
            from PIL import ImageFont
            _PIL_FONTS[size] = ImageFont.truetype(str(_FONT_FILE), size)
        except Exception:  # noqa: BLE001
            _PIL_FONTS[size] = None
    return _PIL_FONTS[size]


def _text_w(text: str, size: int) -> float:
    """자막 문자열의 렌더 폭(px)."""
    f = _pil_font(size)
    if f is not None:
        return float(f.getlength(text))
    return len(text) * size * 0.55


def _line_h(size: int) -> float:
    """자막 한 줄 높이(px) = ascent + descent (≈ 1.35 × 글자크기)."""
    f = _pil_font(size)
    if f is not None:
        try:
            a, d = f.getmetrics()
            return float(a + d)
        except Exception:  # noqa: BLE001
            pass
    return size * 1.35


def _wrap_text(text: str, size: int, max_w: float) -> list[str]:
    """자막 최대 폭(max_w) 안에 들어가도록 단어 단위로 줄바꿈."""
    t = " ".join(str(text).split())
    if not t:
        return [""]
    if _text_w(t, size) <= max_w:
        return [t]
    out: list[str] = []
    cur = ""
    for w in t.split(" "):
        probe = (cur + " " + w).strip()
        if cur and _text_w(probe, size) > max_w:
            out.append(cur)
            cur = w
        else:
            cur = probe
    if cur:
        out.append(cur)
    return out


def _fit_text(text: str, base: int, max_w: float) -> tuple[int, list[str]]:
    """폭에 맞는 (글자 크기, 줄 목록) 결정. 2줄까지, 넘치면 크기를 줄인다."""
    size = base
    for step in (0, 8, 16, 24, 34):
        size = max(base - step, 48)
        lines = _wrap_text(text, size, max_w)
        if len(lines) <= 2:
            return size, lines
    return size, _wrap_text(text, size, max_w)


def _write_ass(path: Path, segs: list[dict[str, Any]], dur: float,
               width: int = 1080, height: int = 1920) -> None:
    """5줄 가사 창 자막(ASS): 가운데(현재) 줄 노란색, 위2줄=이전 / 아래2줄=다음.

    세로(1080x1920)·가로(1920x1080) 공용. 가사창이 화면에서 사라지지 않도록
    창과 창 사이 빈 시간은 앞 창이 앞 절반 / 다음 창이 뒤 절반을 나눠
    채운다(항상 표시).
    """
    land = width > height
    # 가로(높이 1080)는 세로보다 낮아서 5줄 창이 다 안 들어간다.
    # → 3줄 창(이전/현재/다음)으로 표시, 글자도 조금 줄인다.
    # 세로는 기존 5줄 창 유지.
    win = 1 if land else 2
    cur_base, pre_base = (72, 60) if land else (84, 68)
    mid_y, gap = (int(height * 0.45), 14) if land else (940, 16)
    max_w = width - _ASS_MARGIN * 2      # 자막 최대 폭
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Cur = 현재 줄(노란색 굵게), Pre = 이전/다음 줄(반투명 흰색)
        "Style: Cur,Malgun Gothic,84,&H0000FFFF,&H000000FF,&H80000000,&H00000000,"
        "1,0,0,0,100,100,0,0,1,5,1,5,0,0,0,1\n"
        "Style: Pre,Malgun Gothic,68,&H58FFFFFF,&H000000FF,&H80000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,4,1,5,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n")
    n = len(segs)
    # 줄 i의 표시 구간: 앞 줄과의 빈 시간 절반 ~ 다음 줄과의 빈 시간 절반
    span: list[tuple[float, float]] = []
    for i in range(n):
        a = 0.0 if i == 0 else \
            (float(segs[i - 1]["end"]) + float(segs[i]["start"])) / 2.0
        b = dur if i == n - 1 else \
            (float(segs[i]["end"]) + float(segs[i + 1]["start"])) / 2.0
        a = max(a, 0.0)
        if b < a:
            a, b = b, a
        span.append((a, min(max(b, a + 0.1), dur)))
    ev: list[str] = []
    for i, seg in enumerate(segs):
        t0, t1 = span[i]
        if t1 <= t0:  # 0길이 방지(깜빡임 방지)
            t1 = min(t0 + 0.1, dur)
        # 창에 보이는 줄들(가로=이전/현재/다음 3줄, 세로=위2/현재/아래2 5줄)
        idx = list(range(max(0, i - win), min(n, i + win + 1)))
        ci = idx.index(i)
        blocks: list[tuple[int, str, list[str], float]] = []
        for j in idx:
            base = cur_base if j == i else pre_base
            size, wl = _fit_text(segs[j]["text"], base, max_w)
            blocks.append((j, size, wl, len(wl) * _line_h(size)))
        # 현재 블록을 가운데 두고 위/아래로 겹치지 않게 쌓는다
        cy: dict[int, float] = {ci: mid_y}
        y = mid_y - blocks[ci][3] / 2.0
        for k in range(ci - 1, -1, -1):
            y -= gap + blocks[k][3] / 2.0
            cy[k] = y
            y -= blocks[k][3] / 2.0
        y = mid_y + blocks[ci][3] / 2.0
        for k in range(ci + 1, len(blocks)):
            y += gap + blocks[k][3] / 2.0
            cy[k] = y
            y += blocks[k][3] / 2.0
        for k, (j, size, wl, _h) in enumerate(blocks):
            base = cur_base if j == i else pre_base
            fs = f"\\fs{size}" if size != base else ""
            txt = "\\N".join(w.replace("{", "(").replace("}", ")") for w in wl)
            ev.append(
                f"Dialogue: 0,{_ass_ts(t0)},{_ass_ts(t1)},"
                f"{'Cur' if j == i else 'Pre'},,0,0,0,,"
                f"{{\\an5{fs}\\pos({width // 2},{cy[k]:.0f})}}{txt}\n")
    path.write_text(header + "".join(ev), encoding="utf-8")


@app.post("/api/jobs/{job_id}/mp4")
def make_mp4(job_id: str) -> dict[str, Any]:
    """완성된 곡을 쇼츠/릴스용 세로 MP4(1080x1920, 가사 자막 + 파형)로 변환."""
    return _render_mp4(job_id, landscape=False)


@app.post("/api/jobs/{job_id}/mp4-landscape")
def make_mp4_landscape(job_id: str) -> dict[str, Any]:
    """완성된 곡을 가로 MP4(1920x1080, 가사 자막 + 파형)로 변환."""
    return _render_mp4(job_id, landscape=True)


def _render_mp4(job_id: str, landscape: bool = False) -> dict[str, Any]:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    if job.get("status") != "done":
        raise HTTPException(400, "아직 완성되지 않은 곡입니다.")
    wav = _wav_current(job)
    if not wav.exists():
        raise HTTPException(404, "wav 없음")
    suffix = "_landscape" if landscape else ""
    mp4 = OUT_MP4 / f"{_fname(job)}{suffix}.mp4"
    try:
        # 곡 길이 측정
        probe = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(wav)],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        dur = max(float(probe.stdout.strip()), 5.0)
    except Exception:
        dur = 180.0
    opts = job.get("options", {})
    title = job.get("title") or "Untitled"
    # 가사 줄 정리 ([태그] 제거, 빈줄 제거)
    lines = [ln.strip() for ln in str(opts.get("lyrics", "")).splitlines()]
    lines = [ln for ln in lines
             if ln and not re.match(r"^\[.*\]$", ln)]
    # 가사가 모지바케(이중 인코딩)면 복구 → 자막·싱크 모두 정상 텍스트 사용
    lines = [_clean_lyric(ln) for ln in lines]
    lines = [ln for ln in lines if ln]
    if not lines:
        lines = [title]
    n = len(lines)
    # Whisper 보컬 시간표가 있으면 실제 노래 타이밍에 맞춰 싱크, 없으면
    # 분석을 자동 실행(가사-음성 최대 일치) 후 싱크, 실패 시 균등 분할
    synced: list[dict[str, Any]] = []
    vdata = _load_vocal_map(job_id)
    if vdata:
        try:
            synced = _sync_lyrics_to_vocals(lines, vdata.get("vocals", []),
                                            dur, vdata.get("words"))
        except Exception:  # noqa: BLE001
            synced = []
    if not synced:
        try:
            vd = build_vocal_map(job_id)
            synced = _sync_lyrics_to_vocals(lines, vd.get("vocals", []),
                                            dur, vd.get("words"))
        except Exception:  # noqa: BLE001
            synced = []
    # 자막 세그먼트 생성 (싱크 있으면 실측 타이밍, 없으면 균등 분할)
    if synced:
        dur = max(synced[-1]["end"] + 2.0, dur)
    segs = _lyric_segments(lines, synced, dur)
    vw, vh = (1920, 1080) if landscape else (1080, 1920)
    wave_w, wave_y = vw, vh - 340      # 파형 폭=화면 폭, 아래에서 340px 위
    ass = TMP_DIR / f"{job_id}{suffix}.ass"
    _write_ass(ass, segs, dur, vw, vh)
    ass_esc = str(ass).replace("\\", "/").replace(":", "\\:")
    # 헤더 drawtext 2개만 사용 (긴 필터체인은 크래시 유발)
    # 2번째 줄: 작사·작곡 크레딧처럼 항상 "Made by Raccoon Story" 고정 표기
    title_y, credit_y = (110, 182) if landscape else (200, 300)
    title_fs = 64 if landscape else 60
    fopt = f"fontfile={_FONT}:" if _FONT else ""
    head = (f"drawtext={fopt}text='{_ff_esc(title)}':fontsize={title_fs}:fontcolor=white:"
            f"x=(w-text_w)/2:y={title_y},"
            f"drawtext={fopt}text='Made by Raccoon Story':"
            f"fontsize=28:fontcolor=gray:x=(w-text_w)/2:y={credit_y}")
    # ---- 배경: 선택 이미지가 있으면 이미지, 없으면 단색 ----
    bg_imgs = [p for p in (job.get("bg_images") or []) if Path(p).exists()]
    img_inputs: list[str] = []
    if len(bg_imgs) == 1:
        # 1장: cover 스케일+크롭 → 반복 재생
        bg = (f"[2:v]scale={vw}:{vh}:force_original_aspect_ratio=increase,"
              f"crop={vw}:{vh},setsar=1,fps=30,format=yuv420p[bgi];")
        base_layer = "bgi"
        img_inputs = ["-loop", "1", "-i", bg_imgs[0]]
    elif len(bg_imgs) > 1:
        # 여러 장: 각각 같은 길이(dur/n)로 잘라 이어붙임
        n = len(bg_imgs)
        seg = dur / n
        parts = []
        for k, p in enumerate(bg_imgs):
            parts.append(
                f"[{k+2}:v]scale={vw}:{vh}:force_original_aspect_ratio=increase,"
                f"crop={vw}:{vh},setsar=1,fps=30,trim=duration={seg:.2f},"
                f"setpts=PTS-STARTPTS,format=yuv420p[s{k}];")
        concat_in = "".join(f"[s{k}]" for k in range(n))
        parts.append(f"{concat_in}concat=n={n}:v=1:a=0[bgi];")
        bg = "".join(parts)
        base_layer = "bgi"
        for p in bg_imgs:
            img_inputs += ["-loop", "1", "-i", p]
    else:
        bg = (f"color=s={vw}x{vh}:c=#14141f:d={dur:.1f}[bg];")
        base_layer = "bg"
    vf = (bg
          + f"[1:a]showwaves=s={wave_w}x240:mode=line:rate=30:colors=#7c5cff,"
            "format=yuv420p[v];"
            f"[{base_layer}][v]overlay=0:{wave_y}," + head +
            f",subtitles=filename='{ass_esc}':fontsdir='C\\:/Windows/Fonts':"
            "force_style='FontName=Malgun Gothic'")
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-i", str(wav), "-i", str(wav)] + img_inputs +
            ["-filter_complex", vf, "-t", f"{dur:.1f}", "-map", "0:a",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-shortest", str(mp4)],
            capture_output=True, text=True, timeout=900, encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"MP4 변환 실패: {exc}")
    if r.returncode != 0 or not mp4.exists():
        raise HTTPException(
            500, "MP4 변환 실패: " + (r.stderr or "")[-1500:])
    url = _media_url("mp4", f"{_fname(job)}{suffix}.mp4")
    if landscape:
        # 세로본 URL을 덮어쓰지 않고 가로본을 별도 키에 보관
        job["mp4_landscape"] = url
    else:
        job["mp4"] = url
    _save_job(job)
    return {"mp4": url}


# ---------------------------------------------------------------- background image (SD)
def _img_dir(job_id: str) -> Path:
    """곡별 배경 이미지 폴더 (output/images/{job_id}/)."""
    d = OUT_IMG / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list_images(job_id: str) -> list[dict[str, Any]]:
    """곡의 배경 이미지 목록 (URL + 파일명, 하위 타임스탬프 폴더 포함)."""
    d = _img_dir(job_id)
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*/bg_*.png")):
        out.append({"name": f"{p.parent.name}/{p.name}",
                    "url": _media_url("image",
                                      f"{job_id}/{p.parent.name}/{p.name}")})
    return out


def _auto_prompt(job: dict[str, Any]) -> str:
    """노래(제목+스타일+가사 일부)로부터 배경 이미지 프롬프트를 만든다."""
    opts = job.get("options", {}) or {}
    title = str(job.get("title") or "").strip()
    style = str(opts.get("style") or "").strip()
    lyrics = str(opts.get("lyrics") or "")
    ly_lines = [ln.strip() for ln in lyrics.splitlines()
                if ln.strip() and not re.match(r"^\[.*\]$", ln.strip())]
    mood_src = " ".join(ly_lines[:3])[:200]
    parts: list[str] = []
    if style:
        parts.append(style)
    parts.append("abstract atmospheric background illustration, cinematic, "
                 "high quality, no people, no text")
    if title:
        parts.append(f"evoking the feeling of '{title}'")
    if mood_src:
        parts.append(f"mood: {mood_src}")
    full = ", ".join(parts)
    words = full.split()
    if len(words) > 60:  # SD 3.5 turbo 는 짧고 선명한 프롬프트가 잘 맞음
        full = " ".join(words[:60])
    return full


def _run_sd_worker(prompt: str, out_dir: Path, count: int,
                   orient: str, timeout: int = 1200) -> dict[str, Any]:
    """torch venv python 으로 sd_worker.py 실행 → {"ok", "images"} JSON 반환."""
    if SD_VENV_PY is None:
        return {"ok": False,
                "error": "배경 이미지 생성이 꺼져 있습니다: config/likesuno.json 의 "
                         "sd_venv_python(torch+diffusers 가 설치된 파이썬 경로)을 지정하세요."}
    if not SD_VENV_PY.exists():
        return {"ok": False, "error": f"sd_venv_python 경로 없음: {SD_VENV_PY}"}
    if not SD_WORKER.exists():
        return {"ok": False, "error": f"sd_worker.py 없음: {SD_WORKER}"}
    cmd = [str(SD_VENV_PY), str(SD_WORKER),
           "--prompt", prompt, "--out-dir", str(out_dir),
           "--count", str(count), "--orient", orient,
           "--steps", str(SD_STEPS), "--model", str(SD_MODEL)]
    # Windows cp949(charmap) 기본 인코딩에서 한글 print 가 깨지는 것 방지:
    # 자식 프로세스를 UTF-8 모드로 강제 실행.
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace",
                           env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"이미지 생성 시간 초과({timeout}초)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"SD 워커 실행 실패: {exc}"}
    out_line = ""
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            out_line = ln
    if not out_line:
        return {"ok": False,
                "error": "SD 워커 출력 해석 실패: "
                         + (r.stderr or r.stdout or "")[-500:]}
    try:
        return json.loads(out_line)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "SD 워커 JSON 파싱 실패"}


@app.get("/api/jobs/{job_id}/images")
def list_bg_images(job_id: str) -> dict[str, Any]:
    """곡의 배경 이미지 목록."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    return {"images": _list_images(job_id),
            "selected": job.get("bg_image", "")}


@app.post("/api/jobs/{job_id}/bgimage")
async def make_bg_image(job_id: str, request: Request) -> dict[str, Any]:
    """배경 이미지 생성 (SD 3.5 Large Turbo).

    body: {"mode": "auto"|"manual", "prompt": str(수동), "count": 1~6,
           "orient": "portrait"|"landscape"}
    """
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    if job.get("status") != "done":
        raise HTTPException(400, "아직 완성되지 않은 곡입니다.")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    mode = str(body.get("mode") or "auto").lower()
    orient = str(body.get("orient") or "portrait").lower()
    if orient not in ("portrait", "landscape"):
        orient = "portrait"
    try:
        count = int(body.get("count") or 3)
    except Exception:  # noqa: BLE001
        count = 3
    count = max(1, min(count, 6))

    if mode == "manual":
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "직접 입력 프롬프트가 비어 있습니다.")
    else:
        prompt = _auto_prompt(job)

    stamp = time.strftime("%Y%m%d_%H%M%S")  # 기존 이미지 덮어쓰기 방지
    out_dir = _img_dir(job_id) / stamp
    res = _run_sd_worker(prompt, out_dir, count, orient)
    if not res.get("ok"):
        raise HTTPException(500, "이미지 생성 실패: " + str(res.get("error")))
    new_imgs: list[dict[str, Any]] = []
    for p in sorted(out_dir.glob("bg_*.png")):
        new_imgs.append({"name": f"{stamp}/{p.name}",
                         "url": _media_url("image",
                                           f"{job_id}/{stamp}/{p.name}")})
    return {"images": new_imgs, "prompt": prompt, "all": _list_images(job_id)}


@app.post("/api/jobs/{job_id}/bgimage-select")
async def select_bg_image(job_id: str, request: Request) -> dict[str, Any]:
    """MP4 배경으로 쓸 이미지 선택. 여러 장이면 구간별 번갈아 사용."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    names = body.get("names") or []
    if isinstance(names, str):
        names = [names]
    base = _img_dir(job_id)
    chosen: list[str] = []
    for nm in names:
        cand = base / str(nm)
        if cand.exists() and cand.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            chosen.append(str(cand))
    job["bg_images"] = chosen
    job["bg_image"] = chosen[0] if chosen else ""
    _save_job(job)
    return {"ok": True, "count": len(chosen)}


# ---------------------------------------------------------------- producer tag
TAG_DIR = ROOT / "my voice"
TAG_DEFAULT_NAME = "made by raccoon story"  # 기본 태그 (my voice 폴더 파일명과 동일)
_TAG_EXTS = (".m4a", ".wav", ".mp3", ".webm", ".ogg", ".opus")


def _list_tags() -> dict[str, Path]:
    """my voice 폴더의 모든 오디오 파일을 태그 후보로 수집 (파일명=태그 이름)."""
    tags: dict[str, Path] = {}
    if TAG_DIR.exists():
        for p in sorted(TAG_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in _TAG_EXTS:
                tags[p.stem] = p
    d = tags.pop(TAG_DEFAULT_NAME, None)
    if d is not None:
        tags = {TAG_DEFAULT_NAME: d, **tags}  # 기본 태그를 맨 앞으로
    return tags


def _tag_path(key: str) -> Optional[Path]:
    """태그 이름(파일명)으로 실제 오디오 파일 경로 찾기."""
    key = str(key or "").strip()
    if not key:
        return None
    p = TAG_DIR / key
    if p.is_file():
        return p
    for ext in _TAG_EXTS:
        c = TAG_DIR / f"{key}{ext}"
        if c.exists():
            return c
    return None

# 장르별 태그 프리셋: 피치(반음) / 템포 배율 / 믹스 레벨 / 이펙트
_TAG_PRESETS: dict[str, dict[str, Any]] = {
    "hiphop":  {"pitch": -3.0, "tempo": 0.90, "level_db": -8.0,
                "fx": "aecho=0.8:0.7:120:0.25"},
    "trap":    {"pitch": -3.0, "tempo": 0.90, "level_db": -8.0,
                "fx": "aecho=0.8:0.7:120:0.25"},
    "rap":     {"pitch": -3.0, "tempo": 0.90, "level_db": -8.0,
                "fx": "aecho=0.8:0.7:120:0.25"},
    "rock":    {"pitch": -1.0, "tempo": 1.00, "level_db": -8.0,
                "fx": "acompressor=threshold=-18dB:ratio=3"},
    "metal":   {"pitch": -1.0, "tempo": 1.00, "level_db": -8.0,
                "fx": "acompressor=threshold=-18dB:ratio=3"},
    "ballad":  {"pitch": 0.0, "tempo": 1.00, "level_db": -8.0,
                "fx": "aecho=0.6:0.5:300:0.2"},
    "piano":   {"pitch": 0.0, "tempo": 1.00, "level_db": -8.0,
                "fx": "aecho=0.6:0.5:300:0.2"},
    "pop":     {"pitch": 2.0, "tempo": 1.02, "level_db": -8.0,
                "fx": "aecho=0.8:0.6:180:0.25,adelay=300|300:all=1"},
    "dance":   {"pitch": 2.0, "tempo": 1.02, "level_db": -8.0,
                "fx": "aecho=0.8:0.6:180:0.25,adelay=300|300:all=1"},
    "synth":   {"pitch": 2.0, "tempo": 1.02, "level_db": -8.0,
                "fx": "aecho=0.8:0.6:180:0.25,adelay=300|300:all=1"},
    "city":    {"pitch": 1.0, "tempo": 1.00, "level_db": -8.0,
                "fx": "chorus=0.7:0.9:55:0.4:0.25:2"},
    "funk":    {"pitch": 1.0, "tempo": 1.00, "level_db": -8.0,
                "fx": "chorus=0.7:0.9:55:0.4:0.25:2"},
    "lofi":    {"pitch": -2.0, "tempo": 0.92, "level_db": -8.0,
                "fx": "vibrato=f=4:d=0.4,aecho=0.7:0.5:250:0.2"},
    "jazz":    {"pitch": -1.0, "tempo": 0.95, "level_db": -8.0,
                "fx": "vibrato=f=4:d=0.3"},
}
_TAG_DEFAULT_PRESET = {"pitch": 0.0, "tempo": 1.00, "level_db": -8.0, "fx": ""}


def _detect_preset(style: str) -> tuple[str, dict[str, Any]]:
    s = (style or "").lower()
    for key, preset in _TAG_PRESETS.items():
        if key in s:
            return key, preset
    return "default", _TAG_DEFAULT_PRESET


def _probe_dur(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return float(r.stdout.strip())


def _scan_loudness(src: Path, song_dur: float,
                   win: float = 5.0, step: float = 2.5) -> list[tuple[float, float]]:
    """곡 전체를 win초 창으로 스캔해 [(시작점, mean_dB)] 리스트 반환."""
    out: list[tuple[float, float]] = []
    t = 0.0
    while t < max(song_dur - win, 0.0) + 0.01:
        try:
            r = subprocess.run(
                [FFMPEG, "-v", "info", "-ss", f"{t:.1f}", "-t", f"{win:.1f}",
                 "-i", str(src), "-af", "volumedetect", "-f", "null", "NUL:"],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB",
                          r.stderr or "")
            if m:
                out.append((round(t, 1), float(m.group(1))))
        except Exception:  # noqa: BLE001
            pass
        t += step
    return out


def _pick_positions(scans: list[tuple[float, float]], song_dur: float,
                    target: float) -> list[float]:
    """시작/중간/끝 3개 구간에서 각각 가장 조용한 위치를 고른다.
    구간: [0, 30초] / [30%, 70%] / [끝-30초, 끝]."""
    zones = [(0.0, min(30.0, song_dur - target)),
             (song_dur * 0.3, song_dur * 0.7),
             (max(song_dur - 30.0, 0.0), max(song_dur - target - 0.5, 0.0))]
    pos: list[float] = []
    for lo, hi in zones:
        cands = [(t, db) for t, db in scans if lo <= t <= hi + 0.01]
        if cands:
            cands.sort(key=lambda x: x[1])
            pos.append(cands[0][0])
        else:
            pos.append(round((lo + hi) / 2.0, 1))
    # 겹침 방지: 최소 target+1초 간격
    for i in (1, 2):
        if pos[i] < pos[i - 1] + target + 1.0:
            pos[i] = round(pos[i - 1] + target + 1.0, 1)
    if pos[2] > song_dur - target - 0.3:
        pos[2] = round(max(song_dur - target - 0.5, pos[1] + target + 1.0), 1)
    return pos


def _find_quiet_spot(src: Path, song_dur: float,
                     lo: float, hi: float, win: float = 5.0) -> float:
    """하위 호환용: [lo, hi] 범위에서 5초 창의 평균 볼륨이 가장 낮은 시작점.
    실패하면 중앙값을 반환."""
    scans = _scan_loudness(src, song_dur, win=win, step=2.5)
    cands = [(t, db) for t, db in scans if lo <= t <= hi + 0.01]
    if cands:
        cands.sort(key=lambda x: x[1])
        return cands[0][0]
    return song_dur / 2.0


@app.post("/api/jobs/{job_id}/tag")
async def apply_tag(job_id: str, request: Request) -> dict[str, Any]:
    """Producer Tag 박아넣기: Made by kkk 음성을 장르 프리셋으로 변형해
    시작/조용한 중간 구간/끝 3곳 중 선택한 위치에 믹싱하고 원본 WAV에 덮어쓴다.
    (원본은 output/wav/ 에 보존되고, 태그본은 output/wav_tag/ 에 저장됨)
    body: {"tag": "태그 이름(my voice 파일명, 기본 made by raccoon story)", "positions": [t0, t1, t2](초, 선택),
           "level_db": float(선택), "place": "all"|"1"|"2"|"3"(선택, 기본 all),
           "preview": bool}
      - positions 지정 시 수동 위치에 박음 (자동 탐색 건너뜀)
      - place="all" 은 3곳 모두, "1"/"2"/"3" 은 해당 위치 한 곳에만 박음
      - preview=true 시 저장 없이 미리듣기용 파일만 생성 (output/wav_tag/{이름}.preview.wav)"""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    if job.get("status") != "done":
        raise HTTPException(400, "아직 완성되지 않은 곡입니다.")
    orig = OUT_WAV / f"{_fname(job)}.wav"
    tagged = OUT_TAG / f"{_fname(job)}.wav"
    # 항상 원본(wav/)에서 시작 -> 태그 중복 누적 방지, 원본은 그대로 보존
    src = orig if orig.exists() else tagged
    if not src.exists():
        raise HTTPException(404, "wav 없음")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    tag_key = str(body.get("tag") or TAG_DEFAULT_NAME).strip()
    tag_src = _tag_path(tag_key)
    if tag_src is None:
        # 구버전 키(kkk2 등) 호환: 첫 번째 태그로 대체
        all_tags = _list_tags()
        if all_tags:
            tag_key = next(iter(all_tags))
            tag_src = all_tags[tag_key]
    if tag_src is None or not tag_src.exists():
        raise HTTPException(404, f"태그 음성 없음: my voice 폴더 확인 ({TAG_DIR})")
    opts = job.get("options", {})
    pname, preset = _detect_preset(str(opts.get("style", "")))
    # 수동 볼륨 지정 시 프리셋 레벨 덮어쓰기
    try:
        lv = body.get("level_db")
        if lv is not None and str(lv).strip() != "":
            preset = {**preset, "level_db": float(lv)}
    except Exception:  # noqa: BLE001
        pass
    try:
        song_dur = _probe_dur(src)
        tag_dur = _probe_dur(tag_src)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"길이 측정 실패: {exc}")
    target = min(max(tag_dur / preset["tempo"], 1.5), 4.0)
    pitch_factor = 2.0 ** (preset["pitch"] / 12.0)
    # 태그 원본 자체가 작게 녹음됨(mean -28dB) -> 먼저 loudnorm으로 정규화 후 레벨 적용
    fx = (f"loudnorm=I=-16:TP=-1.5:LRA=11,"
          f"rubberband=pitch={pitch_factor}:tempo={preset['tempo']},"
          "aresample=44100,aformat=channel_layouts=stereo")
    if preset["fx"]:
        fx += f",{preset['fx']}"
    fx += f",volume={preset['level_db']}dB,atrim=0:{target:.2f}"
    fx += ",asetpts=PTS-STARTPTS"
    scans: list[tuple[float, float]] = []
    pos: list[float] = []
    auto_mode = "auto"
    # 수동 위치 지정 시 자동 탐색 건너뜀
    manual = body.get("positions")
    if isinstance(manual, list) and len(manual) == 3:
        try:
            pos = [round(max(min(float(t), song_dur - target - 0.2), 0.0), 1)
                   for t in manual]
            auto_mode = "manual"
        except Exception:  # noqa: BLE001
            pos = []
    if not pos:
        # 시작/중간/끝 각 구간에서 가장 조용한(가사 없을 확률 높은) 위치 탐색
        # Whisper 보컬 시간표가 있으면 gaps(무보컬 구간) 우선 사용
        vdata = _load_vocal_map(job_id)
        gaps: list[dict[str, float]] = (vdata or {}).get("gaps", [])
        if gaps:
            def _gap_pick(lo: float, hi: float, fallback: float) -> float:
                best: float | None = None
                best_len = -1.0
                for g in gaps:
                    s = max(g["start"], lo)
                    e = min(g["end"] - target, hi)
                    if e >= s and (g["end"] - g["start"]) > best_len:
                        best_len = g["end"] - g["start"]
                        best = round(min(max(s, lo), e), 1)
                return best if best is not None else fallback
            scans = _scan_loudness(src, song_dur)
            fb = _pick_positions(scans, song_dur, target)
            pos = [_gap_pick(0.0, min(30.0, song_dur - target), fb[0]),
                   _gap_pick(song_dur * 0.3, song_dur * 0.7, fb[1]),
                   _gap_pick(max(song_dur - 30.0, 0.0),
                             max(song_dur - target - 0.5, 0.0), fb[2])]
        else:
            scans = _scan_loudness(src, song_dur)
            pos = _pick_positions(scans, song_dur, target)
    # 태그 넣을 위치 개수 선택:
    #  "all"  = 1번·2번·3번 모두  (기존 방식)
    #  "1"/"2"/"3" = 해당 위치만 한 군데에
    sel: str = str(body.get("place") or "all").lower()
    if sel not in ("all", "1", "2", "3"):
        sel = "all"
    branches = ""
    mix_inputs: list[str] = ["[0:a]"]
    applied: list[float] = []
    n = 0
    for i, pos_t in zip((1, 2, 3), pos):
        if sel != "all" and str(i) != sel:
            continue
        n += 1
        applied.append(round(pos_t, 1))
        branches += (
            f"[{i}:a]{fx},adelay={int(pos_t * 1000)}|{int(pos_t * 1000)}[t{i - 1}];"
        )
        mix_inputs.append(f"[t{i - 1}]")
    if n == 0:
        raise HTTPException(400, "태그를 넣을 위치가 없습니다.")
    fc = branches + "".join(mix_inputs) + \
         f"amix=inputs={n + 1}:normalize=0[mixout]"
    tmp = TMP_DIR / f"{job_id}.tagged.tmp.wav"
    preview_mode = bool(body.get("preview"))
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-i", str(src),
             "-i", str(tag_src), "-i", str(tag_src),
             "-i", str(tag_src),
             "-filter_complex", fc, "-map", "[mixout]",
             "-c:a", "pcm_s16le", str(tmp)],
            capture_output=True, text=True, timeout=600, encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"태그 믹싱 실패: {exc}")
    if r.returncode != 0 or not tmp.exists():
        raise HTTPException(500, "태그 믹싱 실패: " + (r.stderr or "")[-1500:])
    if preview_mode:
        preview = OUT_TAG / f"{_fname(job)}.preview.wav"
        shutil.move(str(tmp), str(preview))
        return {"ok": True,
                "preview": _media_url("wav_tag", f"{_fname(job)}.preview.wav"),
                "tag": {"name": tag_key, "preset": pname, "mode": auto_mode,
                        "positions": [round(t, 1) for t in pos],
                        "place": sel, "applied": applied,
                        "level_db": preset["level_db"], "at": time.time()}}
    shutil.move(str(tmp), str(tagged))
    mp3 = OUT_MP3 / f"{_fname(job)}.mp3"
    try:
        r2 = subprocess.run(
            [FFMPEG, "-y", "-i", str(tagged),
             "-codec:a", "libmp3lame", "-b:a", "192k", str(mp3)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=300, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r2.returncode == 0 and mp3.exists():
            job["mp3"] = _media_url("mp3", f"{_fname(job)}.mp3")
    except Exception:  # noqa: BLE001
        pass
    for stale_name in (f"{_fname(job)}.mp4", f"{_fname(job)}_landscape.mp4"):
        stale = OUT_MP4 / stale_name
        if stale.exists():
            stale.unlink()
    job.pop("mp4", None)
    job.pop("mp4_landscape", None)
    job["tag"] = {"name": tag_key, "preset": pname, "mode": auto_mode,
                  "positions": [round(t, 1) for t in pos],
                  "place": sel, "applied": applied,
                  "quiet_db": [db for t in pos
                               for tt, db in scans if abs(tt - t) < 0.01],
                  "level_db": preset["level_db"],
                  "at": time.time()}
    job["wav"] = _media_url("wav_tag", f"{_fname(job)}.wav")
    _save_job(job)
    return {"ok": True, "tag": job["tag"]}


# ---------------------------------------------------------------- 태그 녹음 (my voice)
@app.get("/api/tags")
async def list_tags() -> dict[str, Any]:
    """my voice 폴더의 태그 목록 (파일명=태그 이름)."""
    tags = [{"name": k, "file": p.name} for k, p in _list_tags().items()]
    return {"ok": True, "tags": tags, "default": TAG_DEFAULT_NAME}


@app.post("/api/tags")
async def save_tag(request: Request) -> dict[str, Any]:
    """마이크 녹음 저장: body {"name": "태그 이름", "data": "data:audio/webm;base64,..."}
    → my voice/{이름}.webm 저장 (태그 적용 시 ffmpeg가 자동 변환 처리)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "잘못된 요청")
    name = _safe_fname(str(body.get("name") or "")).strip()
    if not name or name == "untitled":
        raise HTTPException(400, "태그 이름을 입력하세요.")
    data_url = str(body.get("data") or "")
    m = re.match(r"^data:audio/[^;]+;base64,(.+)$", data_url, re.S)
    if not m:
        raise HTTPException(400, "녹음 데이터(audio base64)가 없습니다.")
    try:
        blob = base64.b64decode(m.group(1))
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "녹음 데이터 디코딩 실패")
    if len(blob) < 1000:
        raise HTTPException(400, "녹음이 너무 짧습니다. 다시 녹음해 주세요.")
    TAG_DIR.mkdir(parents=True, exist_ok=True)
    dst = TAG_DIR / f"{name}.webm"
    if dst.exists():  # 같은 이름이 있으면 _2, _3 ... 붙임
        n = 1
        while (TAG_DIR / f"{name}_{n}.webm").exists() and n < 100:
            n += 1
        dst = TAG_DIR / f"{name}_{n}.webm"
        name = dst.stem
    dst.write_bytes(blob)
    dur: float | None = None
    try:
        dur = round(_probe_dur(dst), 2)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "name": name, "file": dst.name, "duration": dur}


# ---------------------------------------------------------------- vocal map (Whisper)
_WHISPER_MODEL = None


def _get_whisper():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        _WHISPER_MODEL = WhisperModel(
            "tiny", device="cpu", compute_type="int8",
            download_root=str(ROOT / "models" / "whisper"))
    return _WHISPER_MODEL


def _vocal_map_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.vocal.json"


# 보컬맵 캐시 버전 (분석 방식이 바뀌면 올림 → 오래된 캐시 자동 폐기)
# v1: 15초 청크 / v2: 5초 청크 + 텍스트 매칭 싱크 / v3: 3초 청크
# v4: 3초 청크 + 가사 프롬프트(초기 힌트)로 인식 정확도 향상
# v5: 조각 밖 환각 타임스탬프 차단 + DP 전역 정렬 싱크
# v6: stride 0.5 + 단어레벨 매칭 개선 → 세그먼트 대량 소실로 폐기 (app.py.v6draft)
# v7: 3초 청크를 1초 간격으로 겹쳐 관측 + 청크 중앙 관측 우선 채택
# v8: word_timestamps 로 단어 단위 시각 수집 → 가사 줄을 '첫 단어 시작점'에 앵커
# v9: 병합은 v5식(겹치면 잘라내기)으로 복귀해 커버율 회복 + 단어는 같은 단어끼리
#     0.4초 이내 묶어 중앙값으로 중복 제거 + 내용어 가중 DP + 적중율로 단어/조각 선택
VOCAL_CACHE_VERSION = 9


def _load_vocal_map(job_id: str) -> Optional[dict[str, Any]]:
    """보컬맵 캐시 로드. 버전이 맞지 않으면 None (재분석 유도)."""
    cache = _vocal_map_path(job_id)
    if not cache.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if data.get("vc") != VOCAL_CACHE_VERSION:
        return None
    return data


def _merge_observations(vocals: list[dict[str, Any]],
                        song_dur: float) -> list[dict[str, Any]]:
    """겹쳐 관측된 조각 병합 (v5식: 겹치면 잘라내기).

    3초 청크를 1초 간격으로 겹치게(0~3, 1~4, 2~5 …) 관측하면 같은 구간이
    여러 청크에서 들어온다. v7/v8의 margin 우선 채택은 겹치면 하나만 남겨
    조각 수·커버율이 떨어졌고(89개/95.8% → 65개/83.3%) 매칭 재료가 줄었다.
    v5처럼 시간순 정렬 후 앞 조각과 겹치는 만큼만 잘라내면 관측을 최대한
    살리면서 1초 단위로 잘게 쪼개진다 → 빠른 곡(랩) 경계 분산 효과 유지.
    """
    vocals = sorted(vocals, key=lambda v: (v["start"], v["end"]))
    # 찌꺼기 조각 버리기: 영문/한글/숫자 알맹이가 없으면 시간 기준으로 못 쓴다
    vocals = [v for v in vocals
              if _tokens(str(v.get("text", "") or ""))]
    dedup: list[dict[str, Any]] = []
    for v in vocals:
        if dedup and v["start"] < dedup[-1]["end"]:
            v = {**v, "start": dedup[-1]["end"]}
            if v["start"] >= v["end"]:
                continue
        dedup.append(v)
    vocals = dedup
    for v in vocals:
        v.pop("margin", None)
    # 곡 길이를 넘는 조각 제거 (환각 타임스탬프 방어)
    vocals = [v for v in vocals if v["start"] < song_dur]
    for v in vocals:
        v["end"] = min(v["end"], round(song_dur, 1))
    return [v for v in vocals if v["end"] > v["start"]]


def _word_ok(w: str) -> bool:
    """Whisper 찌꺼기 단어 판정: 영문/한글/숫자 주성분이 아니면 버림.

    word_timestamps를 켜면 'φåáδ¥╝µ¥Ä', 'lectI', '-I' 같은 조각 파편이
    단어로 들어온다. 소문자 토큰에 영문/한글/숫자가 하나도 없으면 버린다.
    """
    t = str(w or "").strip().lower()
    if not t:
        return False
    core = re.sub(r"^[^0-9a-z가-힣]+|[^0-9a-z가-힣]+$", "", t)
    if not core or not re.search(r"[0-9a-z가-힣]", core):
        return False
    if len(core) == 1 and core not in "ai":
        return False
    return True


def _cluster_words(raw_words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """원시 단어 관측 중복 제거: 같은 단어·0.4초 이내 묶음 → 중앙값 채택.

    3초 청크를 1초 간격으로 겹치면 같은 단어가 최대 3개 청크에서 관측되고
    tiny 단어 시각 오차(±0.5초)로 서로 어긋난다. 같은 정규화 단어끼리
    시작점 간격 0.4초 이내인 묶음의 중앙값을 쓰면 중복 없이 안정적인
    시각 하나가 남는다. (v8 소유-격자는 주인이 바뀌어 중복이 남았다)
    """
    by_tok: dict[str, list[dict[str, Any]]] = {}
    for w in raw_words:
        try:
            a, b = float(w["a"]), float(w["b"])
        except (KeyError, TypeError, ValueError):
            continue
        if b <= a:
            continue
        toks = _tokens(str(w.get("w", "") or ""))
        if not toks:
            continue
        raw_txt = str(w.get("w", "") or "").strip()
        if not _word_ok(raw_txt):
            continue                      # 찌꺼기 파편 단어 → 버림
        by_tok.setdefault(toks[0], []).append(
            {"a": a, "b": b, "w": raw_txt})
    out: list[dict[str, Any]] = []
    for ws in by_tok.values():
        ws.sort(key=lambda x: (x["a"], x["b"]))
        cl: list[dict[str, Any]] = [ws[0]]
        for w in ws[1:]:
            if w["a"] - cl[-1]["a"] <= 0.4:
                cl.append(w)
            else:
                cl.sort(key=lambda x: x["a"])
                out.append(cl[len(cl) // 2])
                cl = [w]
        cl.sort(key=lambda x: x["a"])
        out.append(cl[len(cl) // 2])
    return sorted(out, key=lambda x: (x["a"], x["b"]))


def build_vocal_map(job_id: str) -> dict[str, Any]:
    """Whisper tiny로 보컬 구간 시간표 생성. 3초씩 잘라 분석 후 합침. 캐시 있으면 재사용."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job 없음")
    src = _wav_original(job)
    if not src.exists():
        src = _wav_tagged(job)
    if not src.exists():
        raise HTTPException(404, "wav 없음")
    cached = _load_vocal_map(job_id)
    if cached is not None:
        return cached
    model = _get_whisper()
    # 긴 곡은 통째로 넣으면 앞부분이 뭉개지므로 잘라 분석 후 합침.
    # Whisper는 조각이 길면 인식 오류로 무보컬로 오판하므로 3초 조각을 쓰되,
    # 조각을 1초 간격으로 겹쳐서(0~3초, 1~4초, 2~5초 …) 빠른 구간(랩/속도곡)의
    # 가사 경계를 1초 단위로 더 잘게 관측한다. 겹친 관측은 아래에서
    # 청크 가장자리가 아니라 중앙에서 본 것을 우선 채택한다.
    song_dur = _probe_dur(src)
    chunk, stride = 3.0, 1.0
    # 가사를 초기 프롬프트로 주면 Whisper가 노래 가사를 훨씬 정확히 인식한다
    # (실측: 유사도 0.3 → 0.98). 인식 정확도가 곧 싱크 정확도.
    _lrc = [_clean_lyric(x.strip())
            for x in str(job.get("options", {}).get("lyrics", "")).splitlines()
            if x.strip() and not re.match(r"^\[.*\]$", x.strip())]
    prompt = "\n".join(_lrc)[:600]
    vocals: list[dict[str, Any]] = []
    raw_words: list[dict[str, Any]] = []   # 단어 단위 시각 (원시 관측)
    lang = "en"
    t0 = 0.0
    ci = 0
    while t0 < song_dur:
        clip = TMP_DIR / f"_{job_id}_chunk{ci}.wav"
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-ss", f"{t0:.1f}",
             "-t", f"{chunk:.1f}", "-i", str(src),
             "-c:a", "pcm_s16le", str(clip)],
            timeout=120, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            segs, info = model.transcribe(
                str(clip), beam_size=1, vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=prompt or None,
                word_timestamps=True)          # ← 단어 단위 시작/끝 시각 확보
            lang = info.language or lang
            for s in segs:
                txt = s.text.strip()
                if not txt:
                    continue
                st, en = float(s.start), float(s.end)
                if st >= chunk:  # 조각 밖 타임스탬프(환각) → 버림
                    continue
                en = min(en, chunk)  # 조각 끝을 넘지 않게 자름
                if en <= st:
                    continue
                vocals.append({"start": round(t0 + st, 1),
                               "end": round(t0 + en, 1), "text": txt})
                # 단어 단위 시각 (아래 1초 격자에서 소유 청크를골라 채택)
                for w in (s.words or []):
                    try:
                        ws, we = float(w.start), float(w.end)
                    except (TypeError, ValueError):
                        continue
                    if ws >= chunk or we <= 0.0:
                        continue                # 청크 밖 단어(환각) → 버림
                    we = min(we, chunk)
                    if we <= ws:
                        continue
                    wt = str(getattr(w, "word", "") or "").strip()
                    if not wt:
                        continue
                    raw_words.append({"a": round(t0 + ws, 2),
                                      "b": round(t0 + we, 2),
                                      "w": wt, "t0": t0})
        finally:
            clip.unlink(missing_ok=True)
        t0 += stride
        ci += 1
    # 겹친 관측 병합 (v5식 중복 제거 → 커버율 회복 + 1초 단위 분산)
    vocals = _merge_observations(vocals, song_dur)
    # ---- 단어 단위 시각: 같은 단어·0.4초 이내 묶음 → 중앙값 채택 ----
    # 3개 청크가 같은 단어를 3번 관측하면 tiny 시각 오차(±0.5초)로 흩어진다.
    # 소유-격자 대신 '같은 단어끼리 0.4초 이내 묶음의 중앙값'을 쓰면 중복 없이
    # 가장 안정적인 시각 하나가 남는다.
    words = _cluster_words(raw_words)
    words = [w for w in words if w["a"] < song_dur]
    for w in words:
        w["b"] = min(w["b"], round(song_dur, 2))
    words = sorted((w for w in words if w["b"] > w["a"]),
                   key=lambda x: (x["a"], x["b"]))
    gaps: list[dict[str, float]] = []
    prev = 0.0
    for v in vocals:
        if v["start"] - prev >= 4.0:
            gaps.append({"start": round(prev, 1), "end": round(v["start"], 1)})
        prev = max(prev, v["end"])
    if song_dur - prev >= 4.0:
        gaps.append({"start": round(prev, 1), "end": round(song_dur, 1)})
    result = {"lang": lang, "song_dur": round(song_dur, 1),
              "vocals": vocals, "gaps": gaps,
              "words": [{"a": w["a"], "b": w["b"], "w": w["w"]}
                        for w in words],
              "at": time.time(),
              "vc": VOCAL_CACHE_VERSION}
    _vocal_map_path(job_id).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    job["vocal_map"] = {"lang": lang, "nseg": len(vocals),
                        "ngaps": len(gaps), "nwords": len(words),
                        "at": time.time()}
    _save_job(job)
    return result


@app.get("/api/jobs/{job_id}/vocal-map")
def get_vocal_map(job_id: str) -> dict[str, Any]:
    if not _JOBS.get(job_id):
        raise HTTPException(404, "job 없음")
    data = _load_vocal_map(job_id)
    if data is not None:
        return data
    return build_vocal_map(job_id)


@app.post("/api/jobs/{job_id}/vocal-map")
def post_vocal_map(job_id: str) -> dict[str, Any]:
    return build_vocal_map(job_id)


def _norm_match_text(s: str) -> str:
    """텍스트 매칭용 정규화: 영문 소문자+숫자+한글만 남김."""
    return re.sub(r"[^0-9a-z가-힣]", "", str(s or "").lower())


def _match_score(t: str, v: str) -> float:
    """가사 줄(t)과 Whisper 조각(v)의 유사도.

    Whisper 조각은 가사 줄보다 짧게 끊기는 경우가 많으므로, 조각이 충분히
    길고(8자 이상) 그 대부분이 가사 줄 안에 그대로 들어 있으면(=원본과 일치)
    확실한 일치로 인정해 점수를 올린다.
    """
    if not t or not v:
        return 0.0
    r = SequenceMatcher(None, t, v).ratio()
    ml = SequenceMatcher(None, t, v).find_longest_match(0, len(t),
                                                       0, len(v)).size
    if len(v) >= 8 and ml >= 0.8 * len(v):
        r = max(r, 0.8)
    if len(t) >= 8 and ml >= 0.8 * len(t):
        # word_timestamps를 켜면 조각이 여러 줄+찌꺼기를 합쳐 길어진다.
        # 가사 줄 전체가 조각 안에 들어 있으면 일치로 인정 (반대 방향 보정).
        r = max(r, 0.8)
    return r


def _weighted_assign(lines: list[str],
                     segs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """가사 줄을 세그먼트에 글자 수 비례로 배분 (세그먼트 부족 시 줄 수 균등)."""
    out: list[dict[str, Any]] = []
    n, m = len(lines), len(segs)
    if m == 0:
        return out
    if m <= n:
        for i, ln in enumerate(lines):
            a = int(i * m / n)
            b = max(int((i + 1) * m / n), a + 1)
            chunk = segs[a:b]
            out.append({"text": ln.strip(),
                        "start": round(chunk[0]["start"], 1),
                        "end": round(chunk[-1]["end"], 1)})
        return out
    weights = [max(len(ln.strip()), 1) for ln in lines]
    total = sum(weights)
    a = 0
    for i, ln in enumerate(lines):
        if i == n - 1:
            b = m
        else:
            share = max(1, int(round(weights[i] * m / total)))
            b = min(m - (n - 1 - i), a + share)  # 남은 줄에 최소 1개씩 보장
        chunk = segs[a:b]
        out.append({"text": ln.strip(),
                    "start": round(chunk[0]["start"], 1),
                    "end": round(chunk[-1]["end"], 1)})
        a = b
    return out


def _normalize_sync(segs: list[dict[str, Any]], dur: float,
                    min_dur: float = 0.5) -> list[dict[str, Any]]:
    """자막이 0초로 사라지지 않게 시작/끝을 단조 증가로 보정(가사 누락 방지)."""
    out: list[dict[str, Any]] = []
    prev_end = 0.0
    for s in segs:
        st = max(float(s["start"]), prev_end)
        en = max(float(s["end"]), st + min_dur)
        if en > dur:  # 곡 길이를 넘으면 안쪽으로 당김
            en = dur
            st = max(min(st, en - min_dur), 0.0)
        if en <= st:
            st, en = max(dur - min_dur, 0.0), max(dur, st + min_dur)
        out.append({"text": s["text"], "start": round(st, 2),
                    "end": round(en, 2)})
        prev_end = en
    return out


_TOKEN_RE = re.compile(r"[0-9a-z가-힣']+")


def _tokens(s: str) -> list[str]:
    """매칭용 단어 토큰 (소문자, 영문/숫자/한글만)."""
    return _TOKEN_RE.findall(str(s or "").lower())


def _tok_equal(a: str, b: str) -> bool:
    """단어 매칭 판정. 어간/복수형 차이는 허용(call/calls), 나머지는 엄격."""
    if a == b:
        return True
    if len(a) >= 3 and len(b) >= 3:
        return a.startswith(b) or b.startswith(a)
    return False


_WORD_STOP = set("""the a an and or but if of to in on at for with is are was were be
been i you he she it we they my your his her its our their this that these
those so as not no do does did don't doesn't didn't what when where who how
all any can could would should will just about into over out up down""".split())


def _word_weight(tok: str) -> float:
    """DP 매칭 보상 가중치: 내용어는 1.0, 조사·관사·짧은 토큰은 0.2."""
    t = str(tok or "").lower()
    if len(t) <= 2 or t in _WORD_STOP:
        return 0.2
    return 1.0


def _fill_lines(lines: list[str], i0: int, i1: int,
                t_start: float, t_end: float) -> list[dict[str, Any]]:
    """lines[i0:i1]을 [t_start, t_end] 안에 넣는다.

    각 줄 최소 0.5초를 먼저 깔고 남는 시간을 글자 수 비례로 나눈다.
    (줄 길이 합이 정확히 구간과 같아야 _normalize_sync 가 다시 0.5초로
     늘려 뒤쪽 타임라인을 밀어내는 연쇄가 발생하지 않는다)
    """
    out: list[dict[str, Any]] = []
    cnt = i1 - i0
    if cnt <= 0:
        return out
    span = max(t_end - t_start, 0.0)
    base = 0.5 if span >= cnt * 0.5 else (span / cnt if cnt else 0.0)
    extra = max(span - base * cnt, 0.0)
    wts = [max(len(lines[i].strip()), 1) for i in range(i0, i1)]
    tot = sum(wts) or 1
    a = t_start
    for k in range(cnt):
        d = base + (extra * wts[k] / tot if extra > 0 else 0.0)
        b = t_end if k == cnt - 1 else min(a + d, t_end)
        out.append({"text": lines[i0 + k].strip(),
                    "start": round(a, 2), "end": round(b, 2)})
        a = b
    return out


def _anchor_hit_rate(out: list[dict[str, Any]],
                     vocals: list[dict[str, Any]]) -> float:
    """타임라인 적중율: 각 줄 시간에 겹치는 조각 텍스트에 줄의 내용어가 있으면 적중."""
    ok = used = 0
    for o in out:
        ws = [w for w in _tokens(str(o.get("text", "")))
              if _word_weight(w) >= 1.0]
        if not ws:
            continue
        used += 1
        blob = " ".join(str(v.get("text", "")).lower() for v in vocals
                        if min(float(v["end"]), float(o["end"]))
                        - max(float(v["start"]), float(o["start"])) > 0)
        btoks = set(_tokens(blob))
        if any(w in btoks for w in ws):
            ok += 1
    return (ok / used) if used else 0.0


def _sync_by_words(lines: list[str], words: list[dict[str, Any]],
                   end_limit: float,
                   ) -> Optional[tuple[list[dict[str, Any]], int]]:
    """가사 단어 ↔ Whisper 단어 DP 정렬 → 각 줄을 '첫 단어 시작점'에 앵커.

    조각(segment) 단위가 아니라 단어 단위로 정렬하므로 한 조각 안에
    여러 가사 줄이 들어와도 각 줄을 자기 단어 시각에 따로 놓을 수 있다.

    반환: (타임라인, 앵커 수). 매칭이 전혀 없으면 None.
    """
    lt: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        for t in _tokens(ln):
            lt.append((i, t))
    wt: list[tuple[float, float, str]] = []
    for w in words:
        try:
            s, e = float(w["a"]), float(w["b"])
        except (KeyError, TypeError, ValueError):
            continue
        t = str(w.get("w", "") or "").strip().lower()
        if not t or e <= s or s < 0.0 or s > end_limit:
            continue
        if not _word_ok(t):
            continue                      # 캐시에 남은 찌꺼기 파편 → 무시
        wt.append((s, e, t))
    wt.sort(key=lambda x: (x[0], x[1]))
    if not lt or not wt:
        return None

    n, m = len(lt), len(wt)
    SKIP_W = 0.03                            # Whisper 단어 건너뛸 때 감점
    _RW = [_word_weight(t) for _, t in lt]   # 내용어 1.0 / 조사·관사 0.2
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    bt = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        li = lt[i - 1][1]
        dpim, dpi, bti = dp[i - 1], dp[i], bt[i]
        for j in range(1, m + 1):
            best, b = dpim[j], 1            # 가사 토큰 건너뜀 (미인식 줄)
            v = dpi[j - 1] - SKIP_W         # whisper 단어 건너뜀 → 멀리 있는
            if v > best:                    # 반복 단어와의 매칭을 억제
                best, b = v, 2
            rw = _RW[i - 1]   # 이 가사 토큰의 내용어 가중 보상
            if _tok_equal(li, wt[j - 1][2]) and dpim[j - 1] + rw > best:
                best, b = dpim[j - 1] + rw, 3
            dpi[j], bti[j] = best, b

    pairs: list[tuple[int, int, str]] = []
    i, j = n, m
    while i > 0 and j > 0:
        b = bt[i][j]
        if b == 3:
            # (가사 줄 번호, Whisper 단어 idx, 가사 토큰)
            pairs.append((lt[i - 1][0], j - 1, lt[i - 1][1]))
            i, j = i - 1, j - 1
        elif b == 1:
            i -= 1
        else:
            j -= 1
    if not pairs:
        return None
    pairs.reverse()

    hit: dict[int, list[tuple[int, str]]] = {}
    for li, wj, tk in pairs:
        hit.setdefault(li, []).append((wj, tk))
    N = len(lines)
    cand: list[tuple[int, float, float]] = []
    for li in sorted(hit):
        idxs = hit[li]
        ws = [wt[k] for k, _ in idxs]
        # 내용어 매칭이 있으면 '첫 내용어 시작점'을 앵커로 (조사·관사 점프 방지)
        cw = [wt[k] for k, tk in idxs if _word_weight(tk) >= 1.0]
        use = cw if cw else ws
        st = min(x[0] for x in use)
        en = max(max(x[1] for x in ws), st)
        cand.append((li, st, en))

    # 앵커가 앞/뒤로 남은 줄을 곡 안에 넣을 공간을 확보했을 때만 인정.
    # (공간 부족하면 _normalize_sync 가 줄들을 곡 끝에 겹쳐 몰아넣는다)
    kept: list[tuple[int, float, float]] = []
    prev_i, prev_en = -1, 0.0
    for li, st, en in cand:
        gap = li - prev_i - 1
        if st - prev_en < gap * 0.5 - 1e-6:
            continue                        # 앵커 보류 → 그 줄은 보간으로
        kept.append((li, st, en))
        prev_i, prev_en = li, en
    while kept and (end_limit - kept[-1][2]) < \
            (N - 1 - kept[-1][0]) * 0.5 - 1e-6:
        kept.pop()                          # 뒤 줄 공간이 안 나오면 마지막 앵커 보류
    if not kept:
        return None

    out: list[Optional[dict[str, Any]]] = [None] * N
    for li, st, en in kept:
        out[li] = {"text": lines[li].strip(), "start": round(st, 2),
                   "end": round(en, 2)}

    def put(i0: int, i1: int, t0: float, t1: float) -> None:
        for k, o in enumerate(_fill_lines(lines, i0, i1, t0, t1)):
            out[i0 + k] = o

    put(0, kept[0][0], 0.0, kept[0][1])                       # 앞부분
    for (ia, _, aen), (ib, _, bst) in zip(kept, kept[1:]):     # 앵커 사이
        put(ia + 1, ib, aen, bst)
    put(kept[-1][0] + 1, N, kept[-1][2], end_limit)            # 뒷부분
    return [o for o in out if o], len(kept)


def _sync_lyrics_to_vocals(lyrics_lines: list[str],
                           vocals: list[dict[str, Any]],
                           song_dur: Optional[float] = None,
                           words: Optional[list[dict[str, Any]]] = None,
                           ) -> list[dict[str, Any]]:
    """가사-음성 싱크 매칭.
    1순위: 가사 단어 ↔ Whisper 단어 정렬 → 각 줄을 '첫 단어 시작점'에 앵커
          (한 조각 안에 여러 가사 줄이 들어와도 줄별로 따로 놓을 수 있다)
    2순위: 조각 단위 DP 정렬 (단어 목록이 없거나 단어 앵커가 더 적을 때)
    3순위: 매칭 안 된 줄은 앞뒤 앵커 사이 시간에 순서대로 배치."""
    # 원본 가사가 모지바케(이중 인코딩)일 수 있으므로 매칭 전에 복구한다.
    # (복구 없이 매칭하면 Whisper 텍스트와 유사도가 0에 가까워 매칭 실패)
    lines = [_clean_lyric(ln) for ln in lyrics_lines
             if ln and not re.match(r"^\[.*\]$", ln.strip())]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []
    # 3초 조각 분석에서 나온 비정상(환각) 타임스탬프 방어:
    # ① 조각 길이를 넘게 늘어난 구간 ② 곡 길이를 넘어선 구간은 시간
    # 기준으로 쓰지 않는다(자막이 곡 밖으로 밀리는 문제 방지).
    limit = float(song_dur) if song_dur else float("inf")
    vocals = [{"start": float(v["start"]), "end": float(v["end"]),
               "text": v.get("text", "")}
              for v in vocals
              if 0.0 <= float(v.get("start", 0)) < limit
              and 0.0 < float(v.get("end", 0)) - float(v.get("start", 0)) <= 8.0]
    vocals.sort(key=lambda v: v["start"])
    # 자막 타임라인의 끝: 곡 길이를 알면 곡 길이, 모르면 관측된 마지막 시각
    if song_dur:
        end_limit = float(song_dur)
    elif vocals:
        end_limit = float(vocals[-1]["end"])
    else:
        end_limit = max((float(w["b"]) for w in (words or []) if "b" in w),
                        default=0.0)
    n = len(lines)
    # ---- 1순위: 단어 단위 정렬 (조각이 하나도 없어도 동작) ----
    wres = _sync_by_words(lines, words or [], end_limit) if words else None
    if not vocals:
        return _normalize_sync(wres[0], end_limit) if wres else []
    m = len(vocals)
    # 참고: (구)버전은 m <= n 이면 DP를 건너뛰고 가사 길이로만 균등배분했다.
    # 조각이 가사 줄수보다 적어도 DP는 '실제로 맞은 줄'을 앵커로 쓰고
    # 나머지를 앵커 사이에서 보간하므로 균등배분보다 항상 정확하다.
    # (조각이 3개뿐이던 실패 사례에서 이 분기 때문에 매칭이 통째로 무효화됐다)

    # ---- 1순위: DP 전역 정렬 (순서 강제, 안 맞는 조각은 버림) ----
    # Whisper가 잘못 인식한 조각은 원본 가사와 유사도가 낮으므로 정렬에서
    # 제외되고, 원본과 실제로 맞는 조각만 시간 기준점(앵커)으로 쓰인다.
    vn = [_norm_match_text(v.get("text", "")) for v in vocals]
    sim = [[_match_score(_norm_match_text(ln), vn[j]) for j in range(m)]
           for ln in lines]
    MATCH_MIN = 0.60  # 원본 가사와 이 정도는 맞아야 앵커로 인정
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    bt = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best, b = dp[i - 1][j], 1                 # 가사 줄 미인식
            if dp[i][j - 1] > best:
                best, b = dp[i][j - 1], 2             # 조각 안 맞음(버림)
            sc = sim[i - 1][j - 1]
            # 앵커로 인정하려면 뒤에 남은 줄들이 곡 끝까지 최소 시간(0.5초씩)을
            # 확보할 수 있어야 한다. (마지막 조각에 훌쩍 뒤 줄이 붙으면
            # 곡 끝 0.5초에 줄들이 전부 겹쳐 자막이 한꺼번에 튀어나온다)
            room = vocals[j - 1]["end"] + (n - i) * 0.5 <= end_limit + 1e-6
            if sc >= MATCH_MIN and room and dp[i - 1][j - 1] + sc > best:
                best, b = dp[i - 1][j - 1] + sc, 3    # 원본과 일치 → 앵커
            dp[i][j], bt[i][j] = best, b
    match: list[int | None] = [None] * n
    i, j = n, m
    while i > 0 and j > 0:
        if bt[i][j] == 3:
            match[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif bt[i][j] == 1:
            i -= 1
        else:
            j -= 1

    seg_cnt = sum(1 for x in match if x is not None)
    # 단어 정렬이 조각 정렬과 같거나 더 많은 앵커를 만들면 단어쪽을 쓴다.
    # (단어 앵커는 '첫 단어 시작점'이라 조각 시작점보다 정확하다)
    if wres and wres[1] >= seg_cnt:
        return _normalize_sync(wres[0], end_limit)
    if seg_cnt == 0:
        return _normalize_sync(_weighted_assign(lines, vocals), end_limit)

    # ---- 조각 DP 타임라인 생성 (fill 로직) ----
    def _build_seg_out() -> list[dict[str, Any]]:
        out2: list[Optional[dict[str, Any]]] = [None] * n
        anchors2 = [(i, s) for i, s in enumerate(match) if s is not None]

        def fill2(i0: int, i1: int, s0: int, s1: int,
                  t_start: float, t_end: float) -> None:
            """lines[i0:i1]을 segs[s0:s1]로 채움. 부족하면 시간 보간."""
            seg = vocals[s0:s1]
            cnt = i1 - i0
            if cnt <= 0:
                return
            if not seg:
                for k, ii in enumerate(range(i0, i1)):
                    a = t_start + (t_end - t_start) * k / cnt
                    b = t_start + (t_end - t_start) * (k + 1) / cnt
                    out2[ii] = {"text": lines[ii].strip(),
                                "start": round(a, 1), "end": round(b, 1)}
            elif len(seg) >= cnt:
                part = _weighted_assign(lines[i0:i1], seg)
                for k, ii in enumerate(range(i0, i1)):
                    out2[ii] = part[k]
            else:
                a0, b0 = seg[0]["start"], seg[-1]["end"]
                for k, ii in enumerate(range(i0, i1)):
                    a = a0 + (b0 - a0) * k / cnt
                    b = a0 + (b0 - a0) * (k + 1) / cnt
                    out2[ii] = {"text": lines[ii].strip(),
                                "start": round(a, 1), "end": round(b, 1)}

        first_i, first_s = anchors2[0]
        t0 = vocals[0]["start"] if first_s > 0 else \
            max(vocals[first_s]["start"] - 3.0, 0.0)
        fill2(0, first_i, 0, first_s, t0, vocals[first_s]["start"])
        for i, s in anchors2:
            out2[i] = {"text": lines[i].strip(),
                       "start": round(vocals[s]["start"], 1),
                       "end": round(vocals[s]["end"], 1)}
        for (ia, sa), (ib, sb) in zip(anchors2, anchors2[1:]):
            fill2(ia + 1, ib, sa + 1, sb, vocals[sa]["end"], vocals[sb]["start"])
        last_i, last_s = anchors2[-1]
        last = vocals[last_s]
        # 마지막 앵커 뒤 줄들은 곡 끝(보컬맵 끝/곡 길이)까지 순서대로 배치
        fill2(last_i + 1, n, last_s + 1, m, last["end"], end_limit)
        return [o for o in out2 if o]

    if seg_cnt == 0:
        if wres:
            return _normalize_sync(wres[0], end_limit)
        return _normalize_sync(_weighted_assign(lines, vocals), end_limit)

    # ---- 최종 선택: 앵커 개수가 아니라 내부 적중율로 ----
    # v8은 앵커가 많으면 단어쪽을 써서 틀린 앵커가 그대로 채택됐다.
    seg_out = _build_seg_out()
    if not wres:
        return _normalize_sync(seg_out, end_limit)
    seg_hit = _anchor_hit_rate(seg_out, vocals)
    word_hit = _anchor_hit_rate(wres[0], vocals)
    if word_hit >= seg_hit:
        return _normalize_sync(wres[0], end_limit)
    return _normalize_sync(seg_out, end_limit)
# /audio 직접 서빙 제거됨: /media Range 스트리밍으로 대체 (1초 멈춤 해결)
app.mount("/jobs", StaticFiles(directory=str(JOBS_DIR)), name="jobs")
_static = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(_static), html=True), name="ui")


_load_persisted_jobs()
