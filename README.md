# 🎵 likesuno — YuE2 기반 로컬 음악 생성 앱 (Suno 스타일)

**GTX 1080 Ti (11GB) + 16GB RAM** 에서 돌아가도록 설계된 YuE2 오픈소스 음악 생성 프로그램입니다.

## 왜 이 구성인가?

Pinokio의 `YUE2 // GROOVE`는 **원본 BF16 모델(4B 파라미터, 약 7.8GB)**을 로드하므로
24GB VRAM(Linux)을 권장합니다. 11GB GPU에서는 메모리가 부족합니다.

YuE2의 공식 양자화 가중치는 없지만, **audio.cpp** 프로젝트가 YuE2를
**GGUF 양자화(Q8_0 = 4.26GB, Q4_0 = 2.67GB)**로 변환한 가중치를 제공합니다.

| 항목 | 내용 |
|---|---|
| 런타임 | audio.cpp v0.8.1 (llama.cpp 계열 C++ 추론 엔진, ggml 기반) |
| 모델 | audio-cpp/Yue2-3B-GGUF (Q8_0 권장, Q4_0 대안) |
| 빌드 대상 GPU | **GTX 1080 Ti (Pascal, compute capability 6.1)** |
| 빌드 툴체인 | Visual Studio 2022 Build Tools + CUDA Toolkit **12.9** (13.x는 Pascal 지원 제거됨) |
| 백엔드 | FastAPI (Python 3.11) + vanilla HTML/JS 프론트 |
| 특징 | Suno처럼 스타일 프롬프트 + 가사 → 완성된 곡(보컬+반주) 생성 |

> GTX 10 계열(Pascal)은 NVIDIA CUDA 13에서 지원이 제거되어 prebuilt 바이너리가 없으므로,
> **CUDA 12.9 + sm_61 아키텍처로 직접 빌드**합니다.

## 폴더 구조

```
likesuno\
├── README.md
├── 수정계획서.md             # 기능 요청/수정 계획 + 완료 기록
├── PROGRESS.md               # 개발 진행 노트 (중단/재개용)
├── requirements.txt
├── start_likesuno.ps1        # 웹앱 실행 (127.0.0.1:8560)
├── config\
│   └── likesuno.json         # 앱 설정 (포트/기본값/VRAM 튜닝)
├── scripts\
│   ├── setup_all.ps1         # 전체 설치 한번에 실행
│   ├── install_toolchain.ps1 # VS BuildTools + CUDA 12.9 설치
│   ├── build_runtime.ps1     # audio.cpp sm_61 빌드
│   ├── download_models.ps1   # YuE2 GGUF 가중치 다운로드
│   ├── JustRun.bat           # 더블클릭 실행 (백그라운드 시작 + 브라우저 열기)
│   ├── やめる.bat            # 서버 종료 (PID)
│   └── migrate_to_E.py       # (1회용) 기존 생성물 E: 로 이전 — 실행 완료
├── runtime\audio.cpp\        # audio.cpp 소스 + 빌드 결과
├── models\Yue2-3B-GGUF\      # GGUF 가중치 (~4.6GB)
├── src\likesuno\             # FastAPI 백엔드 + 웹UI
├── output\                   # (구버전) 예전 생성물 위치 — 새 곡은 E: 에 저장
├── jobs\                     # 작업 기록 ({job_id}.json — 제목/가사/스타일/시드)
├── my voice\                 # 제작자 태그 음성 (직접 녹음, 저장소에는 미포함)
└── logs\                     # 생성 로그
```

생성물과 입력 기록은 `config\likesuno.json` 의 `data_root` 폴더에 저장됩니다
(기본값은 비어 있어 **저장소 폴더**에 저장되고, 다른 드라이브로 옮기려면 `data_root` 에 경로를 지정):

```
data_root\
├── input\                    # 곡별 입력 기록 (노래제목.txt — 제목/스타일/가사)
├── output\
│   ├── wav\                  # 원본 WAV      (노래제목.wav)
│   ├── wav_tag\              # 태그 박은 WAV (노래제목.wav) + 미리듣기(.preview.wav)
│   ├── mp3\                  # MP3           (노래제목.mp3)
│   └── mp4\                  # MP4           (노래제목.mp4)
├── _tmp\                     # 작업 중 임시 파일 (자동)
└── _misc\                    # 곡과 무관한 예전 파일 (정리용)
```

- 파일명은 **곡 제목** 기준 (같은 제목이면 `_2`, `_3` 자동 부여)

## 저장소에 포함되지 않는 것 (직접 준비)

| 항목 | 이유 | 준비 방법 |
|---|---|---|
| `venv/` | 설치 산출물 | `scripts\setup_all.ps1` (또는 `python -m venv venv` + `pip install -r requirements.txt`) |
| `models/Yue2-3B-GGUF` | 수 GB 가중치 | `scripts\download_models.ps1` |
| `runtime/audio.cpp` | 소스 + 빌드 결과 | `scripts\build_runtime.ps1` |
| `my voice/` | 개인 녹음(제작자 태그) | 직접 녹음해 넣기 (`🎙️ 태그 만들기`) — 비어 있으면 태그 기능만 비활성 |
| `output/`, `input/`, `jobs/`, `logs/` | 개인 생성물/기록 | 실행하면 자동 생성 |
| API 키/토큰 | 보안 | 이 앱은 외부 API 키를 사용하지 않습니다 |

> **배경 이미지 생성(SD 3.5)은 선택 기능**입니다. `torch` + `diffusers` 가 설치된 별도 파이썬을
> `config\likesuno.json` 의 `sd_venv_python` 과 GGUF 경로 `sd_model` 에 지정해야 켜지고,
> 지정하지 않으면 이미지 생성 버튼을 눌렀을 때 안내 메시지만 표시됩니다.

## 설치 순서 (한 번만)

```powershell
cd <저장소를 받은 폴더>
.\scripts\setup_all.ps1        # 도구체인 → 빌드 → 모델 다운로드 순서로 진행
```

각 단계를 개별 실행할 수도 있습니다:

```powershell
.\scripts\install_toolchain.ps1   # 1) VS BuildTools(D:\VS2022BuildTools) + CUDA 12.9(D:\CUDA\v12.9)
.\scripts\build_runtime.ps1       # 2) audiocpp_cli.exe / audiocpp_server.exe 빌드 (sm_61)
.\scripts\download_models.ps1     # 3) Q8_0 + VAE F16 + sidecar 다운로드 (~4.6GB, -SkipQ4 로 2.7GB 절약)
```

> ⚠️ **관리자 권한** PowerShell에서 실행하세요 (VS/CUDA 설치에 필요).
> 디스크: C: 약 3GB(Windows SDK), D: 약 15GB(빌드 도구+CUDA+모델) 필요.

## 실행

가장 쉬운 방법 — **`scripts\JustRun.bat` 을 더블클릭**:

- 서버는 **백그라운드(창 없음)** 로 실행되고, 잠시 후 브라우저에 http://127.0.0.1:8560 이 자동으로 열립니다
- 실행 창은 몇 초 뒤 자동으로 닫힙니다 (닫혀도 서버는 계속 동작)
- **서버 종료는 `scripts\やめる.bat`** 을 더블클릭하세요
- 이미 실행 중일 때 다시 실행하면 브라우저만 열립니다
- 포트는 `config\likesuno.json` 의 `port` 값을 따릅니다 (기본 8560)

PowerShell로 실행하려면:

```powershell
cd <저장소를 받은 폴더>
.\start_likesuno.ps1
```

## 사용법 (Suno처럼)

1. **스타일**: `Korean, indie pop, warm acoustic guitar, soft drums, emotional female vocal`
2. **가사**: `[Verse]`, `[Chorus]` 등 섹션 태그로 구조화 (한국어 가능)
3. **생성**: 기본값 CoT=off(빠름), 스텝 8, 시드 랜덤
   - CoT 모드: `off`(빠름) / `melody`(멜로디 계획) / `full`(코드+멜로디 전체 계획, 느림+ABC 악보 출력)
4. 결과는 라이브러리에서 재생/다운로드

### 🏷️ 제작자 태그 박기

생성된 곡 카드의 **태그 설정**을 펼치면 제작자 태그("made by raccoon story" 등)를 곡에 박을 수 있습니다.

1. **태그**: 사용할 태그 음성 선택 — `my voice` 폴더의 모든 오디오 파일이 목록에 자동 표시됩니다
   (`my voice` 폴더에 본인 태그 음성을 넣으면 그 이름이 그대로 목록에 표시됩니다)
2. **위치(초)**: 태그를 넣을 3곳 — **자동탐색값** 버튼으로 보컬 없는 구간을 자동 입력
3. **볼륨(dB)**: 태그 크기 조절 (클수록 크게, 기본 -8)
4. **미리듣기**로 확인한 뒤, 원하는 방식으로 박기:
   - **전체 태그 박기(1·2·3)** — 1번·2번·3번 위치에 모두
   - **1번 태그만** — 1번 위치에만
   - **2번 태그만** — 2번 위치에만
   - **3번 태그만** — 3번 위치에만

원본 WAV는 `output\wav\`에 그대로 보존되고, 태그를 박은 WAV는 `output\wav_tag\`에 따로 저장됩니다. 다시 박아도 태그가 겹치지 않습니다.

### 🎙️ 태그 만들기 (내 목소리 녹음)

라이브러리 아래의 **`🎙️ 태그 만들기`** 를 펼치면 내 목소리로 새 태그를 만들 수 있습니다.

1. 이름 입력 (예: `Made by KKK3`) → **🔴 녹음 시작** → 말하기 → **⏹ 정지**
2. **👂 미리듣기**로 확인 (마음에 안 들면 **🗑 버리기** 후 재녹음)
3. **💾 저장** → `my voice\{이름}.webm` 으로 저장되고, 곡 카드의 **태그 목록에 바로 추가**됩니다
   (이미 있는 이름이면 `_2`, `_3` 이 붙어 저장)
- 브라우저 마이크 권한을 허용해야 합니다 (주소창 자물쇠 아이콘 → 마이크 허용)

### 🎯 보컬 싱크 분석 · 🎬 MP4 변환 (쇼츠/릴스)

- **보컬 싱크 분석**: Whisper로 보컬 구간을 **3초 단위(1.5초 겹침)** 로 정밀 분석해 곡별 시간표를
  만듭니다 (곡당 1회, 곡 길이에 따라 3~6분). 태그 자동탐색과 MP4 가사 자막이 이 시간표를
  우선 사용하고, **MP4 변환 시 시간표가 없으면 자동으로 분석**합니다.
  (분석 방식이 개선되면 기존 시간표는 자동으로 무효화되고 다시 분석됩니다)
- **MP4 변환**: **쇼츠·릴스용 세로 영상(1080×1920)** 을 만듭니다.

| 위치 | 내용 |
|---|---|
| 상단 y=200 | 곡 제목 (흰색, 60px) |
| 상단 y=300 | `Made by Raccoon Story` 고정 크레딧 (회색, 28px) |
| 가운데 y≈940 | **5줄 가사 창** — 현재 줄 노란색(84px) + 위2·아래2줄 회색(68px) |
| 하단 y=1580 | 파형 (보라색 라인, 1080×240) |

- 가사창은 **곡 시작부터 끝까지 항상 표시**됩니다 — 줄과 줄 사이 빈 시간을 앞 가사가 앞 절반,
  다음 가사가 뒤 절반을 채워 자막이 사라지는 순간이 없습니다.
- 긴 가사는 화면 폭(980px)에 맞춰 **자동으로 2줄까지 줄바꿈**되고, 그래도 길면 글자가
  자동으로 작아져 좌우로 잘리지 않습니다.
- 가사-음성 싱크는 Whisper 인식 텍스트와 원본 가사를 **DP 전역 정렬**로 매칭해 실제 노래
  타이밍에 맞춥니다. 인식이 부정확한 곡에서는 자막이 빠지지 않도록 보컬 구간에 순차 배분됩니다.
- 가사에 intro 등은 [Intro] 대괄호로 감싸야 영상에 가사로 출력되지 않는다.
 다만 Intro라고 입력해도 음성으로 출력은 안되기 때문에 음악에는 영향을 주진 않지만 보기에 Intro가 표시되기에 []를 붙이는게 좋다


## 💡 1080 Ti 성능 팁

- **Q8_0**(기본, 음질 우선)이 11GB에 여유 있게 올라갑니다. OOM 시 **Q4_0**으로 전환.
- 짧은 곡(1~2절, 코러스 포함 ~30-60초)부터 시작하세요. **가사가 길수록 AR 디코딩 시간이 급증**합니다.
- `cot=off`가 가장 빠릅니다. `cot=full`은 ABC 계획 생성(+수천 토큰 AR)이라 수 배 느립니다.
- 첫 실행 시 모델 로딩에 수 분이 걸릴 수 있습니다.
- 시드를 고정하면 같은 조건으로 재현 가능합니다.

## 라이선스 주의

YuE2 / YuE2-Vae 모델 가중치는 **CC BY-NC 4.0 (비영리)** 입니다.
개인적인 음악 생성/수익화는 허용되지만, 회사의 영리 목적 사용은 별도 라이선스가 필요합니다.
audio.cpp 코드는 Apache-2.0 입니다.

## 참고 자료

- YuE2 원본: https://github.com/multimodal-art-projection/YuE
- YuE2-3B HF: https://huggingface.co/m-a-p/YuE2-3B
- audio.cpp: https://github.com/0xShug0/audio.cpp
- YuE2 GGUF 가중치: https://huggingface.co/audio-cpp/Yue2-3B-GGUF
- YuE2 문서(audio.cpp): runtime/audio.cpp/docs/models/yue2.md
