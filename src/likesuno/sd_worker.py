"""배경 이미지 생성 워커 (torch + diffusers 전용).

likesuno 본앱(FastAPI)은 torch가 없는 별도 venv 로 격리 실행된다.
torch + diffusers 가 있는 별도 파이썬으로 이 스크립트를 서브프로세스로
실行해 SD 3.5 Large Turbo(Q4 GGUF)로 배경 이미지를 생성한다.

사용법:
    python sd_worker.py --prompt "..." --out-dir DIR --count 3
                      [--width 576 --height 1024] [--seed N]
                      [--steps 8] [--model PATH]

결과: out-dir 에 bg_01.png, bg_02.png ... 저장. JSON 을 stdout 으로 출력.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from pathlib import Path

# Windows cp949(charmap) 환경에서도 한글 print/JSON 이 깨지지 않도록
# stdout/stderr 을 UTF-8 로 강제 (서브프로세스 호출 시 인코딩 오류 방지).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  (구버전/리다이렉트 불가 환경 무시)
        pass

# --- 기본 경로 (없으면 인자로 받음) ---
_ROOT = Path(__file__).resolve().parents[2]          # 저장소 루트
DEFAULT_MODEL = (os.environ.get("LIKESUNO_SD_MODEL")
                 or str(_ROOT / "models" / "sd3.5_large_turbo-q4_k_4_0.gguf"))

# --- 생성은 1/4 해상도(VRAM 절약) → Lanczos 업스케일 ---
# 세로: 576x1024 생성 → 1080x1920 업스케일
# 가로: 1024x576 생성 → 1920x1080 업스케일
GEN_W, GEN_H = 576, 1024        # 세로 9:16 의 1/4
UP_W, UP_H = 1080, 1920         # 최종 세로
GEN_W_L, GEN_H_L = 1024, 576    # 가로 16:9 의 1/4
UP_W_L, UP_H_L = 1920, 1080     # 최종 가로

# 배경용 네거티브 프롬프트 (글자/워터마크/얼굴 왜곡 방지)
NEG = ("lowres, blurry, text, watermark, signature, logo, ugly, deformed, "
       "extra fingers, bad anatomy, cropped, jpeg artifacts")


def load_gguf_pipe(model_path: str, device: str):
    """SD 3.5 Large Q4 GGUF: transformer만 GGUF, CLIP+VAE는 HF에서 로드.

    QK-norm 디퀀타이즈 + pos_embed 배치차원 보정 패치를 적용한다.
    """
    import os

    # base 레포(stabilityai/stable-diffusion-3.5-large)는 게이트 레포라
    # 토큰/로컬 캐시 없이는 허브에서 받을 수 없다(401 → "pass a token").
    #  - 이미 받아둔 캐시가 있으면: LIKESUNO_HF_HOME=<HF 캐시 폴더> 지정 (오프라인 로드)
    #  - 처음 받을 때      : `hf auth login` 으로 토큰을 넣고 LIKESUNO_HF_HOME 은 비워둔다
    hf_home = os.environ.get("LIKESUNO_HF_HOME")
    if hf_home:
        os.environ["HF_HOME"] = hf_home
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

    import torch
    from diffusers import SD3Transformer2DModel, StableDiffusion3Pipeline
    from diffusers.quantizers.quantization_config import GGUFQuantizationConfig

    # GGUF 로드 패치 (QK-norm 디퀀타이즈 + pos_embed 배치차원 보정)
    # gguf_patch.py 가 있는 폴더를 LIKESUNO_GGUF_PATCH_DIR 로 지정할 수 있다.
    scripts_dir = os.environ.get("LIKESUNO_GGUF_PATCH_DIR", "")
    if scripts_dir and scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from gguf_patch import apply as _apply_gguf_patch  # noqa: E402
        _apply_gguf_patch()
    except Exception as _exc:  # noqa: BLE001
        print(f"[sd_worker] gguf_patch 미적용({_exc}) → 기본 로더로 진행", flush=True)

    # HF 토큰 (게이트 레포용, 있으면 사용)
    token = None
    for p in (os.path.expanduser(r"~\.cache\huggingface\token"),
              os.path.expanduser(r"~\.huggingface\token")):
        if os.path.exists(p):
            token = open(p, encoding="utf-8").read().strip()
            os.environ["HF_TOKEN"] = token
            break

    t0 = time.time()
    print(f"[sd_worker] transformer 로드 (GGUF Q4): {model_path}", flush=True)
    qcfg = GGUFQuantizationConfig(compute_dtype=torch.float16)
    transformer = SD3Transformer2DModel.from_single_file(
        model_path, quantization_config=qcfg,
        torch_dtype=torch.float16, token=token, low_cpu_mem_usage=True,
    )
    gc.collect()
    print(f"[sd_worker] transformer 완료: {time.time()-t0:.1f}s", flush=True)

    pipe = StableDiffusion3Pipeline.from_pretrained(
        "stabilityai/stable-diffusion-3.5-large",
        transformer=transformer, text_encoder_3=None, tokenizer_3=None,
        torch_dtype=torch.float16, token=token,
    )
    pipe.to(device)
    try:
        pipe.enable_vae_tiling()
    except Exception:
        pass
    print(f"[sd_worker] 파이프라인 준비 완료: {time.time()-t0:.1f}s", flush=True)
    return pipe


def is_flat(pil_img, tol: float = 6.0) -> bool:
    """단색/저분산 이미지(생성 실패) 판정 — 저장 전 걸름."""
    g = pil_img.convert("L").resize((32, 32))
    px = list(g.getdata())
    if not px:
        return True
    mean = sum(px) / len(px)
    var = sum((p - mean) ** 2 for p in px) / len(px)
    return var < tol


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--width", type=int, default=576)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--orient", choices=("portrait", "landscape"),
                    default="portrait")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1/4 생성 → 업스케일 목표 해상도
    if args.orient == "landscape":
        gen_w, gen_h, up_w, up_h = GEN_W_L, GEN_H_L, UP_W_L, UP_H_L
    else:
        gen_w, gen_h, up_w, up_h = GEN_W, GEN_H, UP_W, UP_H

    try:
        import torch  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"torch import 실패: {e}"}))
        return 2

    try:
        pipe = load_gguf_pipe(args.model, args.device)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"모델 로드 실패: {e}"}))
        return 2

    saved: list[str] = []
    from PIL import Image
    for i in range(1, args.count + 1):
        seed = args.seed if args.seed >= 0 else int(time.time() * 1000) % (2 ** 31) + i
        gen = None
        try:
            gen = torch.Generator(device=args.device).manual_seed(seed)
        except Exception:
            gen = None
        made = False
        for attempt in range(2):  # 단색이면 시드 바꿔 1회 재시도
            try:
                s = seed if attempt == 0 else seed + 7919
                g = None
                try:
                    g = torch.Generator(device=args.device).manual_seed(s)
                except Exception:
                    g = None
                t = time.time()
                with torch.inference_mode():
                    res = pipe(prompt=args.prompt, negative_prompt=NEG,
                               height=gen_h, width=gen_w,
                               num_inference_steps=args.steps,
                               guidance_scale=0.0, generator=g)
                img = res.images[0]
                if is_flat(img):
                    print(f"[sd_worker] {i}번 단색 의심 → 재시도", flush=True)
                    continue
                # 1/4 생성본 → 고품질 업스케일
                img = img.resize((up_w, up_h), Image.LANCZOS)
                img.save(out_dir / f"bg_{i:02d}.png")
                saved.append(str(out_dir / f"bg_{i:02d}.png"))
                print(f"[sd_worker] {i}번 완료 ({time.time()-t:.1f}s) "
                      f"{gen_w}x{gen_h}→{up_w}x{up_h} → bg_{i:02d}.png", flush=True)
                made = True
                break
            except Exception as e:  # noqa: BLE001
                print(f"[sd_worker] {i}번 생성 오류: {e}", flush=True)
                break
        if not made:
            print(f"[sd_worker] {i}번 생성 실패", flush=True)

    print(json.dumps({"ok": True, "images": saved}, ensure_ascii=False))
    return 0 if saved else 3


if __name__ == "__main__":
    raise SystemExit(main())
