const $ = (id) => document.getElementById(id);
const state = { polling: null, logJob: null };

const TEMPLATE = `[Verse]\n창문 너머 아침 빛이 스며들어\n도시는 잠에서 깨어나\n\n[Chorus]\n리듬에 몸을 맡기고\n함께라면 우리는 혼자가 아니야`;

// ---------- init ----------
async function init() {
  const cfg = await (await fetch("/api/config")).json();
  $("steps").value = cfg.defaults.num_inference_steps;
  $("cot").value = cfg.defaults.cot;
  $("quant").value = cfg.defaults.quantization;

  const h = await (await fetch("/api/health")).json();
  if (!h.cli_exists) {
    $("gpu-badge").innerHTML = "audio.cpp 미빌드 — scripts\\build_runtime.ps1 실행";
    $("gpu-badge").style.color = "#ff6b6b";
  } else if (!h.model_ready) {
    $("gpu-badge").innerHTML = "모델 없음 — scripts\\download_models.ps1 실행";
    $("gpu-badge").style.color = "#ffb454";
  }
  refreshGpu();
  loadTags();
  setInterval(pollJobs, 2000);
  pollJobs();
}

async function refreshGpu() {
  try {
    const r = await fetch("/api/gpu");
    const g = await r.json();
    if (g && g.name) {
      $("gpu-badge").innerHTML = `<b>${g.name}</b> · VRAM ${g.vram_used}/${g.vram_total} MiB`;
    }
  } catch (e) { /* nvidia-smi 실패 무시 */ }
}

// ---------- generation ----------
async function generate() {
  const body = {
    title: $("title").value.trim(),
    style: $("style").value,
    lyrics: $("lyrics").value,
    cot: $("cot").value,
    quantization: $("quant").value,
    num_inference_steps: parseInt($("steps").value || "8"),
    seed: $("seed").value.trim() || "random",
  };
  const gs = $("gs").value.trim();
  if (gs !== "") body.guidance_scale = parseFloat(gs);

  const res = await fetch("/api/generate", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(err.detail || "생성 요청 실패");
    return;
  }
  $("btn-gen").disabled = true;
  pollJobs();
}

// ---------- polling ----------
let lastJobsJson = "";
async function pollJobs() {
  const jobs = await (await fetch("/api/jobs")).json();
  renderStatus(jobs);
  // 재생 중 끊김 방지: 변경 없으면 라이브러리 다시 그리지 않음
  const sig = JSON.stringify(jobs.map(j => [j.id, j.status, j.title, j.mp3, j.mp4, j.mp4_landscape, j.wav,
    j.tag?.name, j.tag?.preset, j.tag?.place, (j.tag?.applied || []).join(","), j.vocal_map?.nseg]));
  if (sig !== lastJobsJson) {
    lastJobsJson = sig;
    renderLibrary(jobs);
  }
}

function renderStatus(jobs) {
  const active = jobs.find(j => j.status === "running" || j.status === "queued");
  const panel = $("status-panel");
  if (!active) {
    if (state.logJob) { panel.hidden = true; state.logJob = null; }
    $("btn-gen").disabled = false;
    return;
  }
  panel.hidden = false;
  state.logJob = active.id;
  const opt = active.options || {};
  const txt = active.status === "running"
    ? `생성 중 — ${active.title} (CoT: ${opt.cot}, ${opt.quantization}, 스텝 ${opt.num_inference_steps})`
    : `대기 중 (앞의 작업 실행 후 시작) — ${active.title}`;
  $("status-text").textContent = txt;
  fetch(`/api/jobs/${active.id}/log`).then(r => r.json()).then(l => {
    $("status-log").textContent = l.tail || "로그 대기 중…";
  });
}

function renderLibrary(jobs) {
  state.jobs = jobs;
  const done = jobs.filter(j => j.status === "done");
  const lib = $("library");
  if (!done.length) { lib.innerHTML = `<div class="empty">아직 생성된 곡이 없습니다.</div>`; return; }
  lib.innerHTML = done.map(j => {
    const o = j.options || {};
    const tagPlace = j.tag?.place && j.tag.place !== "all" ? ` · ${j.tag.place}번만` : "";
    const tagState = j.tag ? ` · 🏷️ 태그 적용됨(${esc(j.tag.preset)}${tagPlace})` : "";
    const meta = [
      o.style, `시드 ${o.seed}`, `CoT ${o.cot}`, o.quantization, `스텝 ${o.num_inference_steps}`,
      new Date(j.created_at * 1000).toLocaleString("ko-KR"),
    ].filter(Boolean).join(" · ") + tagState;
    const score = j.score ? `<a href="${j.score}" download>🎼 ABC 악보</a>` : "";
    const wavSrc = j.wav || "";
    // MP3가 있으면 MP3 우선 재생 (가벼워서 끊김 없음), 없으면 WAV
    const playSrc = j.mp3 || wavSrc;
    const mp3Btn = j.mp3 ? `<a href="${j.mp3}" download="${escAttr(j.fname || j.title)}.mp3">⬇ MP3 다운로드</a>` : "";
    const wavBtn = wavSrc ? `<a href="${wavSrc}" download="${escAttr(j.fname || j.title)}.wav">⬇ WAV</a>` : "";
    const mp4ConvBtn = `<button class="ghost btn-mp4c" data-id="${j.id}">🎬 세로영상 제작</button>`;
    const mp4LandConvBtn = `<button class="ghost btn-mp4l" data-id="${j.id}">🎬 가로영상 제작</button>`;
    const mp4Btn = j.mp4 ? `<a href="${j.mp4}" download="${escAttr(j.fname || j.title)}.mp4">⬇ 세로MP4 다운로드</a>` : "";
    const mp4LandBtn = j.mp4_landscape ? `<a href="${j.mp4_landscape}" download="${escAttr(j.fname || j.title)}_landscape.mp4">⬇ 가로MP4 다운로드</a>` : "";
    const tagBtn = `<button class="ghost btn-tag" data-id="${j.id}">🏷️ 태그 박기(전체)</button>`;
    const lyricsBtn = `<button class="ghost btn-lyrics" data-id="${j.id}">📜 가사</button>`;
    const renameBtn = `<button class="ghost btn-rename" data-id="${j.id}">✏️ 제목변경</button>`;
    const syncState = j.vocal_map ? ` · 🎯 싱크됨(보컬 ${j.vocal_map.nseg}구간)` : "";
    const syncBtn = `<button class="ghost btn-sync" data-id="${j.id}">🎯 보컬 싱크 분석</button>`;
    const songDur = j.vocal_map?.song_dur || 228;
    const tagPos = j.tag?.positions || [0.5, songDur / 2, songDur - 5];
    const curTag = esc(j.tag?.name || state.tagDefault || "made by raccoon story");
    const tagOptions = (state.tagList && state.tagList.length)
      ? state.tagList.map(t => `<option value="${esc(t.name)}" ${t.name === curTag ? "selected" : ""}>${esc(t.name)}</option>`).join("")
      : `<option value="${curTag}" selected>${curTag}</option>`;
    const tagPanel = `
      <details class="tag-panel">
        <summary>🏷️ 태그 설정</summary>
        <div class="row">
          <label class="inline">태그</label>
          <select id="tag-sel-${j.id}">${tagOptions}</select>
        </div>
        <div class="row">
          <label class="inline">위치(초)</label>
          <input id="tag-p0-${j.id}" type="number" step="0.1" min="0" value="${tagPos[0]}" style="width:80px">
          <input id="tag-p1-${j.id}" type="number" step="0.1" min="0" value="${tagPos[1]}" style="width:80px">
          <input id="tag-p2-${j.id}" type="number" step="0.1" min="0" value="${tagPos[2]}" style="width:80px">
          <button class="ghost btn-tag-auto" data-id="${j.id}">자동탐색값</button>
        </div>
        <div class="row">
          <label class="inline">볼륨(dB)</label>
          <input id="tag-db-${j.id}" type="number" step="1" value="${j.tag?.level_db ?? -8}" style="width:70px">
          <span class="hint">클수록 크게 (권장 -8)</span>
        </div>
        <div class="row">
          <button class="ghost btn-tag-prev" data-id="${j.id}">👂 미리듣기</button>
          <audio id="tag-audio-${j.id}" controls preload="none" style="display:none;width:100%"></audio>
        </div>
        <div class="row">
          <button class="ghost btn-tag-apply" data-id="${j.id}" data-place="all">🏷️ 전체 태그 박기(1·2·3)</button>
          <button class="ghost btn-tag-apply" data-id="${j.id}" data-place="1">🏷️ 1번 태그만</button>
          <button class="ghost btn-tag-apply" data-id="${j.id}" data-place="2">🏷️ 2번 태그만</button>
          <button class="ghost btn-tag-apply" data-id="${j.id}" data-place="3">🏷️ 3번 태그만</button>
        </div>
      </details>`;
    const mp4Play = j.mp4_landscape
      ? `<video controls preload="none" src="${j.mp4_landscape}" style="width:100%;border-radius:8px"></video>`
      : (j.mp4 ? `<video controls preload="none" src="${j.mp4}" style="width:100%;border-radius:8px"></video>` : "");
    return `
    <div class="song">
      <h3>${esc(j.title)} <span class="badge-state st-done">완료</span></h3>
      <div class="meta">${esc(meta)}${syncState}</div>
      <audio controls preload="metadata" src="${playSrc}"></audio>
      ${mp4Play}
      <div class="actions">
        ${mp3Btn}
        ${wavBtn}
        ${score}
        ${lyricsBtn}
        ${renameBtn}
        ${mp4ConvBtn}
        ${mp4LandConvBtn}
        ${mp4Btn}
        ${mp4LandBtn}
        ${tagBtn}
        ${syncBtn}
      </div>
      ${tagPanel}
      <div class="hint mp4-status" id="mp4-${j.id}"></div>
      <div class="hint tag-status" id="tag-${j.id}"></div>
      <div class="hint sync-status" id="sync-${j.id}"></div>
    </div>`;
  }).join("");
  lib.querySelectorAll(".btn-mp4c").forEach(b => b.addEventListener("click", () => makeMp4(b.dataset.id)));
  lib.querySelectorAll(".btn-mp4l").forEach(b => b.addEventListener("click", () => makeMp4Landscape(b.dataset.id)));
  lib.querySelectorAll(".btn-tag").forEach(b => b.addEventListener("click", () => makeTag(b.dataset.id)));
  lib.querySelectorAll(".btn-lyrics").forEach(b => b.addEventListener("click", () => openLyrics(b.dataset.id)));
  lib.querySelectorAll(".btn-rename").forEach(b => b.addEventListener("click", () => renameJob(b.dataset.id)));
  lib.querySelectorAll(".btn-tag-prev").forEach(b => b.addEventListener("click", () => previewTag(b.dataset.id)));
  lib.querySelectorAll(".btn-tag-apply").forEach(b => b.addEventListener("click", () => applyTagManual(b.dataset.id, b.dataset.place)));
  lib.querySelectorAll(".btn-tag-auto").forEach(b => b.addEventListener("click", () => fillAutoPos(b.dataset.id)));
  lib.querySelectorAll(".btn-sync").forEach(b => b.addEventListener("click", () => makeSync(b.dataset.id)));
  fillImgJobs();  // 이미지 패널의 곡 드롭다운 동기화
}

function openLyrics(jobId) {
  // 새 탭에서 가사창 열기
  window.open(`/lyrics/${jobId}`, "_blank");
}

async function renameJob(jobId) {
  const job = (state.jobs || []).find(j => j.id === jobId) || {};
  const cur = job.title || "";
  const input = prompt("새 제목을 입력하세요", cur);
  if (input === null) return;
  const title = input.trim();
  if (!title || title === cur) return;
  try {
    const res = await fetch(`/api/jobs/${jobId}/rename`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { alert(`❌ ${data.detail || "제목 변경 실패"}`); return; }
    lastJobsJson = "";
    pollJobs();
  } catch (e) {
    alert("❌ 제목 변경 실패 (서버 연결 확인)");
  }
}

async function makeMp4(jobId) {
  const el = document.getElementById(`mp4-${jobId}`);
  if (el) el.textContent = "🎬 세로영상 제작 중… (1~3분 소요)";
  try {
    const res = await fetch(`/api/jobs/${jobId}/mp4`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) { if (el) el.textContent = `❌ ${data.detail || "MP4 변환 실패"}`; return; }
    if (el) el.textContent = "✅ 세로영상 완성!";
    pollJobs();
  } catch (e) {
    if (el) el.textContent = "❌ MP4 변환 실패 (서버 연결 확인)";
  }
}

async function makeMp4Landscape(jobId) {
  const el = document.getElementById(`mp4-${jobId}`);
  if (el) el.textContent = "🎬 가로영상 제작 중… (1~3분 소요)";
  try {
    const res = await fetch(`/api/jobs/${jobId}/mp4-landscape`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) { if (el) el.textContent = `❌ ${data.detail || "MP4 변환 실패"}`; return; }
    if (el) el.textContent = "✅ 가로영상 완성!";
    pollJobs();
  } catch (e) {
    if (el) el.textContent = "❌ MP4 변환 실패 (서버 연결 확인)";
  }
}

async function makeTag(jobId) {
  const el = document.getElementById(`tag-${jobId}`);
  if (el) el.textContent = "🏷️ 태그 박는 중… (1분 이내)";
  try {
    const tagSel = document.getElementById(`tag-sel-${jobId}`);
    const res = await fetch(`/api/jobs/${jobId}/tag`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag: (tagSel && tagSel.value) || state.tagDefault || "made by raccoon story" }),
    });
    const data = await res.json();
    if (!res.ok) { if (el) el.textContent = `❌ ${data.detail || "태그 실패"}`; return; }
    if (el) el.textContent = `✅ 태그 적용! (${data.tag.preset}, ${data.tag.positions.join("s · ")}s)`;
    lastJobsJson = "";
    pollJobs();
  } catch (e) {
    if (el) el.textContent = "❌ 태그 실패 (서버 연결 확인)";
  }
}

function tagOpts(jobId, place) {
  const v = id => document.getElementById(id)?.value;
  return {
    tag: v(`tag-sel-${jobId}`) || state.tagDefault || "made by raccoon story",
    positions: [parseFloat(v(`tag-p0-${jobId}`)), parseFloat(v(`tag-p1-${jobId}`)), parseFloat(v(`tag-p2-${jobId}`))],
    level_db: parseFloat(v(`tag-db-${jobId}`)),
    place: place || "all",
  };
}

async function previewTag(jobId) {
  const el = document.getElementById(`tag-${jobId}`);
  const au = document.getElementById(`tag-audio-${jobId}`);
  if (el) el.textContent = "👂 미리듣기 생성 중… (30초 이내)";
  try {
    const res = await fetch(`/api/jobs/${jobId}/tag`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...tagOpts(jobId), preview: true }),
    });
    const data = await res.json();
    if (!res.ok) { if (el) el.textContent = `❌ ${data.detail || "미리듣기 실패"}`; return; }
    if (el) el.textContent = `👂 미리듣기 준비됨 (${data.tag.positions.join("s · ")}s) — 아래 플레이어로 확인 후 박아넣기를 누르세요`;
    if (au) { au.style.display = "block"; au.src = data.preview + "?t=" + Date.now(); au.play().catch(() => {}); }
  } catch (e) {
    if (el) el.textContent = "❌ 미리듣기 실패 (서버 연결 확인)";
  }
}

const TAG_PLACE_LBL = { all: "전체(1·2·3)", "1": "1번만", "2": "2번만", "3": "3번만" };

async function applyTagManual(jobId, place) {
  place = place || "all";
  const lbl = TAG_PLACE_LBL[place] || TAG_PLACE_LBL.all;
  const where = place === "all" ? "1·2·3번 전체 위치" : `${place}번 위치만`;
  const el = document.getElementById(`tag-${jobId}`);
  if (!confirm(`태그를 ${where}에 박아넣을까요? (원본은 자동 백업됩니다)`)) return;
  if (el) el.textContent = `🏷️ 태그 박는 중… (${lbl}, 1분 이내)`;
  try {
    const res = await fetch(`/api/jobs/${jobId}/tag`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(tagOpts(jobId, place)),
    });
    const data = await res.json();
    if (!res.ok) { if (el) el.textContent = `❌ ${data.detail || "태그 실패"}`; return; }
    const applied = data.tag.applied || data.tag.positions;
    if (el) el.textContent = `✅ 태그 적용! (${lbl} · ${data.tag.name} · ${applied.join("s · ")}s)`;
    lastJobsJson = "";
    pollJobs();
  } catch (e) {
    if (el) el.textContent = "❌ 태그 실패 (서버 연결 확인)";
  }
}

async function fillAutoPos(jobId) {
  const el = document.getElementById(`tag-${jobId}`);
  if (el) el.textContent = "🔍 조용한 구간 탐색 중…";
  try {
    const res = await fetch(`/api/jobs/${jobId}/tag`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag: document.getElementById(`tag-sel-${jobId}`)?.value || state.tagDefault || "made by raccoon story", preview: true }),
    });
    const data = await res.json();
    if (!res.ok) { if (el) el.textContent = `❌ ${data.detail || "탐색 실패"}`; return; }
    const p = data.tag.positions;
    document.getElementById(`tag-p0-${jobId}`).value = p[0];
    document.getElementById(`tag-p1-${jobId}`).value = p[1];
    document.getElementById(`tag-p2-${jobId}`).value = p[2];
    if (el) el.textContent = `🔍 자동탐색값 입력됨 (${p.join("s · ")}s) — 미리듣기로 확인하세요`;
  } catch (e) {
    if (el) el.textContent = "❌ 탐색 실패 (서버 연결 확인)";
  }
}

async function makeSync(jobId) {
  const el = document.getElementById(`sync-${jobId}`);
  if (el) el.textContent = "🎯 보컬 구간 분석 중… (2~5분 소요, 1회만 하면 됨)";
  try {
    const res = await fetch(`/api/jobs/${jobId}/vocal-map`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) { if (el) el.textContent = `❌ ${data.detail || "분석 실패"}`; return; }
    if (el) el.textContent = `✅ 분석 완료! 보컬 ${data.vocals.length}구간 · 무보컬 ${data.gaps.length}구간 (태그·MP4가 이 시간표 사용)`;
    lastJobsJson = "";
    pollJobs();
  } catch (e) {
    if (el) el.textContent = "❌ 분석 실패 (서버 연결 확인)";
  }
}

// ---------- 태그 녹음 (마이크) ----------
let recStream = null, recMedia = null, recChunks = [], recT0 = 0, recTimer = null;

function updRecBtns() {
  const on = !!(recMedia && recMedia.state === "recording");
  $("rec-stop").disabled = !on;
  $("rec-preview").disabled = !(state.recBlob && !on);
  $("rec-save").disabled = !(state.recBlob && !on);
  $("rec-start").disabled = on;
  $("rec-del").disabled = !(state.recBlob && !on);
}

async function recStart() {
  try {
    recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    $("rec-status").textContent = "❌ 마이크 권한이 필요합니다. 브라우저에서 허용해 주세요.";
    return;
  }
  state.recBlob = null;
  $("rec-audio").removeAttribute("src");
  $("rec-audio").style.display = "none";
  recChunks = [];
  recMedia = new MediaRecorder(recStream);
  recMedia.ondataavailable = e => { if (e.data && e.data.size) recChunks.push(e.data); };
  recMedia.onstop = () => {
    state.recBlob = new Blob(recChunks, { type: recMedia.mimeType || "audio/webm" });
    recStream.getTracks().forEach(t => t.stop());
    recStream = null;
    $("rec-status").textContent = "⏹ 녹음 완료 — 미리듣기로 확인 후 저장하세요.";
    $("rec-preview").style.display = "";
    updRecBtns();
  };
  recMedia.start();
  recT0 = Date.now();
  updRecBtns();
  $("rec-preview").style.display = "none";
  $("rec-status").textContent = "🔴 녹음 중…";
  recTimer = setInterval(() => {
    $("rec-status").textContent = `🔴 녹음 중… ${((Date.now() - recT0) / 1000).toFixed(1)}초`;
  }, 100);
}

function recStop() {
  if (recMedia && recMedia.state === "recording") recMedia.stop();
  if (recTimer) { clearInterval(recTimer); recTimer = null; }
}

function recPreview() {
  if (!state.recBlob) return;
  const au = $("rec-audio");
  au.style.display = "";
  au.src = URL.createObjectURL(state.recBlob);
  au.play().catch(() => {});
}

function recDelete() {
  state.recBlob = null;
  const au = $("rec-audio");
  au.pause(); au.removeAttribute("src"); au.style.display = "none";
  $("rec-preview").style.display = "none";
  $("rec-status").textContent = "🗑 녹음을 버렸습니다. 다시 녹음해 주세요.";
  updRecBtns();
}

async function recSave() {
  if (!state.recBlob) return;
  const name = ($("rec-name").value || "").trim();
  if (!name) { $("rec-status").textContent = "❌ 태그 이름을 입력하세요."; return; }
  $("rec-status").textContent = "💾 저장 중…";
  const b64 = await new Promise(res => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result));
    fr.readAsDataURL(state.recBlob);
  });
  try {
    const res = await fetch("/api/tags", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, data: b64 }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { $("rec-status").textContent = `❌ ${data.detail || "저장 실패"}`; return; }
    $("rec-status").textContent = `✅ 저장 완료: ${data.name} (${data.duration ?? "?"}초) — 곡의 태그 목록에 추가되었습니다`;
    state.recBlob = null;
    lastJobsJson = "";
    loadTags(); pollJobs();
  } catch (e) {
    $("rec-status").textContent = "❌ 저장 실패 (서버 연결 확인)";
  }
}

async function loadTags() {
  try {
    const r = await fetch("/api/tags");
    const d = await r.json();
    state.tagList = d.tags || [];
    state.tagDefault = d.default || (state.tagList[0] && state.tagList[0].name) || "made by raccoon story";
  } catch (e) {
    state.tagList = state.tagList || [];
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escAttr(s) {
  return String(s ?? "").replace(/[&"]/g, c => (
    { "&": "&amp;", '"': "&quot;" }[c]));
}

// ---------- 배경 이미지 패널 ----------
state.imgSel = new Set();   // 선택된 이미지 name 목록
state.imgJob = "";          // 현재 선택 곡

function fillImgJobs() {
  const sel = $("img-job");
  const cur = sel.value;
  const done = (state.jobs || []).filter(j => j.status === "done");
  sel.innerHTML = `<option value="">곡을 선택하세요</option>` +
    done.map(j => `<option value="${j.id}">${esc(j.title)}</option>`).join("");
  if (cur && done.some(j => j.id === cur)) sel.value = cur;
  state.imgJob = sel.value;
}

async function loadImages() {
  const jid = $("img-job").value;
  state.imgJob = jid;
  state.imgSel = new Set();
  $("img-grid").innerHTML = "";
  if (!jid) return;
  $("img-status").textContent = "불러오는 중…";
  try {
    const d = await (await fetch(`/api/jobs/${jid}/images`)).json();
    renderImages(d.images || [], $("img-orient").value);
    $("img-status").textContent = d.images && d.images.length
      ? `${d.images.length}장 — 클릭해 선택하세요.`
      : "아직 이미지가 없습니다. 위에서 생성하세요.";
  } catch (e) {
    $("img-status").textContent = "❌ 이미지 목록 불러오기 실패";
  }
}

function renderImages(list, orient) {
  const grid = $("img-grid");
  if (!list.length) { grid.innerHTML = ""; return; }
  grid.innerHTML = list.map(im => `
    <div class="img-cell ${orient === "landscape" ? "landscape" : ""}" data-name="${escAttr(im.name)}" title="${escAttr(im.name)}">
      <img loading="lazy" src="${im.url}" alt="">
      <div class="pick">✓</div>
    </div>`).join("");
  grid.querySelectorAll(".img-cell").forEach(c => c.addEventListener("click", () => {
    const nm = c.dataset.name;
    if (state.imgSel.has(nm)) { state.imgSel.delete(nm); c.classList.remove("sel"); }
    else { state.imgSel.add(nm); c.classList.add("sel"); }
  }));
}

async function genImage(mode) {
  const jid = $("img-job").value;
  if (!jid) { $("img-status").textContent = "먼저 곡을 선택하세요."; return; }
  const orient = $("img-orient").value;
  const count = parseInt($("img-count").value, 10) || 3;
  const body = { mode, count, orient };
  if (mode === "manual") {
    const p = $("img-prompt").value.trim();
    if (!p) { $("img-status").textContent = "프롬프트를 입력하세요."; return; }
    body.prompt = p;
  }
  $("img-status").textContent = `🎨 생성 중… (장당 ~30초, ${count}장 ≈ ${count * 30}초) 모델 로딩 포함 시 더 걸림`;
  $("img-auto").disabled = $("img-manual").disabled = true;
  try {
    const res = await fetch(`/api/jobs/${jid}/bgimage`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { $("img-status").textContent = `❌ ${d.detail || "생성 실패"}`; return; }
    state.imgSel = new Set((d.images || []).map(i => i.name));  // 새 이미지 자동 선택
    renderImages(d.all || [], orient);
    const made = (d.images || []).length;
    $("img-status").textContent = made
      ? `✅ ${made}장 생성 완료 (선택됨). MP4 변환 전 '배경으로 적용' 누르세요.`
      : "⚠ 생성된 이미지가 없습니다 (단색 실패 등).";
  } catch (e) {
    $("img-status").textContent = "❌ 생성 실패 (서버/SD 확인)";
  } finally {
    $("img-auto").disabled = $("img-manual").disabled = false;
  }
}

async function applyImages() {
  const jid = $("img-job").value;
  if (!jid) { $("img-status").textContent = "먼저 곡을 선택하세요."; return; }
  if (!state.imgSel.size) { $("img-status").textContent = "적용할 이미지를 선택하세요."; return; }
  try {
    const res = await fetch(`/api/jobs/${jid}/bgimage-select`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: [...state.imgSel] }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { $("img-status").textContent = `❌ ${d.detail || "적용 실패"}`; return; }
    $("img-status").textContent = `✅ ${d.count}장을 배경으로 적용했습니다. 이제 MP4 변환하세요.`;
  } catch (e) {
    $("img-status").textContent = "❌ 적용 실패 (서버 연결 확인)";
  }
}

// ---------- events ----------
$("btn-gen").addEventListener("click", generate);
$("btn-template").addEventListener("click", () => {
  if (!$("lyrics").value.trim()) $("lyrics").value = TEMPLATE;
  updLen();
});
$("btn-rand").addEventListener("click", () => {
  $("seed").value = Math.floor(Math.random() * 2 ** 31);
});
$("lyrics").addEventListener("input", updLen);
function updLen() { $("lyrics-len").textContent = `${$("lyrics").value.length} 자`; }
document.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => {
  $("style").value = c.dataset.style;
}));

// ---------- 녹음 패널 이벤트 ----------
$("rec-start").addEventListener("click", recStart);
$("rec-stop").addEventListener("click", recStop);
$("rec-preview").addEventListener("click", recPreview);
$("rec-save").addEventListener("click", recSave);
$("rec-del").addEventListener("click", recDelete);

// ---------- 배경 이미지 패널 이벤트 ----------
$("img-job").addEventListener("change", loadImages);
$("img-orient").addEventListener("change", () => {
  const jid = $("img-job").value;
  if (jid) loadImages();
});
$("img-auto").addEventListener("click", () => genImage("auto"));
$("img-manual").addEventListener("click", () => genImage("manual"));
$("img-apply").addEventListener("click", applyImages);

init();
