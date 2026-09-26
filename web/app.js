/* ═══ H3 片场 — image + prompt → video (internal gateway calls only) ═══ */
"use strict";

const $ = (s) => document.querySelector(s);
const API = ""; // same origin — endpoints stay internal, never shown in UI

/* on 401 the session cookie is stale (e.g. service restarted) — hit "/" once to
   re-mint it, then retry the request; callers never see the auth hiccup */
async function apiFetch(url, opts) {
  let r = await fetch(url, opts);
  if (r.status === 401) {
    await fetch(`${API}/`).catch(() => {});
    r = await fetch(url, opts);
  }
  return r;
}

const state = {
  image: null,        // dataURL
  imageName: "",
  ratio: "9:16",
  duration: 6,
  taskId: null,
  pollTimer: null,
  busy: false,
};

const EXAMPLES = [
  { name: "清晨六点", tag: "cinematic still", url: "/examples/ex1.jpg" },
  { name: "球场之夜", tag: "stadium night", url: "/examples/ex2.jpg" },
  { name: "能量街口", tag: "surreal street", url: "/examples/ex3.jpg" },
  { name: "手作面团", tag: "kitchen top-down", url: "/examples/ex4.jpg" },
];

/* ── toast ─────────────────────────────────────────────────── */
let toastTimer;
function toast(msg, ms = 2600) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), ms);
}

/* ── sticky note status ────────────────────────────────────── */
function note(text, busy = false, sticky = false) {
  const el = $("#note");
  $("#note-text").textContent = text;
  el.classList.add("show");
  el.classList.toggle("busy", busy);
  if (!sticky) {
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 3400);
  }
}
function noteHide() { $("#note").classList.remove("show", "busy"); }

/* ── chips (ratio / duration) ──────────────────────────────── */
function chips(id, onPick) {
  $(id).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    $(id).querySelectorAll("button").forEach((x) => x.classList.remove("is-on"));
    b.classList.add("is-on");
    onPick(b.dataset.v);
  });
}
chips("#seg-ratio", (v) => (state.ratio = v));
chips("#seg-dur", (v) => (state.duration = +v));

/* ── image intake: browse / drop / paste ───────────────────── */
const drop = $("#drop");
const fileInput = $("#file");

drop.addEventListener("click", (e) => {
  if (!e.target.closest("#clear") && !state.busy) fileInput.click();
});
drop.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});
fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) readFile(fileInput.files[0]);
  fileInput.value = "";
});
["dragenter", "dragover"].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("armed"); })
);
["dragleave", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("armed"); })
);
drop.addEventListener("drop", (e) => {
  const f = [...(e.dataTransfer?.files || [])].find((f) => f.type.startsWith("image/"));
  if (f) readFile(f); else toast("需要图片文件（JPG / PNG / WebP）");
});
addEventListener("paste", (e) => {
  const f = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith("image/"));
  if (f) { readFile(f); toast("已从剪贴板读取图片"); }
});

function readFile(file) {
  if (file.size > 20 * 1024 * 1024) return toast("图片超过 20MB 上限");
  const reader = new FileReader();
  reader.onload = () => setImage(reader.result, file.name, fmtSize(file.size));
  reader.readAsDataURL(file);
}

function setImage(src, name, meta) {
  state.image = src;
  state.imageName = name || "image";
  $("#thumb").src = src;
  $("#fname").textContent = name || "image";
  $("#fsize").textContent = meta || "";
  $("#drop-idle").hidden = true;
  $("#drop-preview").hidden = false;
  $("#gen").disabled = false;
}

function clearImage() {
  state.image = null;
  $("#drop-idle").hidden = false;
  $("#drop-preview").hidden = true;
  $("#gen").disabled = true;
}
$("#clear").addEventListener("click", (e) => { e.stopPropagation(); clearImage(); });

function fmtSize(b) {
  return b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
}

/* ── generation ────────────────────────────────────────────── */
$("#gen").addEventListener("click", generate);

async function generate() {
  if (!state.image || state.busy) return;
  state.busy = true;
  $("#gen").classList.add("busy");
  $("#gen-label").textContent = "开机…";
  showProgress("交卷给 H3 引擎…");
  note("片场开机，渲染中…", true, true);
  $("#result").hidden = true;

  try {
    const prompt = $("#prompt").value.trim();
    const body = {
      size: state.ratio,
      seconds: String(state.duration),
      image: state.image,
    };
    if (prompt) body.prompt = prompt;

    const resp = await apiFetch(`${API}/v1/videos`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(cleanErr(data?.error?.message || data?.detail) || `提交失败（${resp.status}）`);

    state.taskId = data.id;
    poll(data.id);
  } catch (err) {
    fail(err.message || "提交失败");
  }
}

/* translate upstream/gateway error payloads into human-readable text */
function cleanErr(m) {
  if (!m) return "出错了";
  const p = m.match(/"(?:error|message)"\s*:\s*"([^"]+)"/);
  const s = p ? p[1] : m;
  if (/could not be started|try again|retryable|overload|unavailable|busy/i.test(s))
    return "引擎繁忙，本次拍摄失败了——请再点一次「开拍」";
  if (/expired/i.test(s)) return "任务已过期，请重新生成";
  if (/invalid gateway api key/i.test(s)) return "会话失效，请刷新页面";
  if (/only 9:16/i.test(s)) return "当前通道只支持 9:16 竖屏";
  return s.length > 90 ? s.slice(0, 90) + "…" : s;
}

function poll(id) {
  clearInterval(state.pollTimer);
  let ticks = 0;
  state.pollTimer = setInterval(async () => {
    ticks++;
    try {
      const r = await apiFetch(`${API}/v1/videos/${id}`);
      const t = await r.json();
      if (!r.ok) throw new Error(t?.detail || "查询失败");
      if (t.status === "succeeded") return done(id, t);
      if (t.status === "failed") throw new Error(cleanErr(t.failure_reason) || "生成失败");
      showProgress(t.status === "queued" ? "排队等机位…" : `镜头渲染中… ${Math.min(ticks * 3, 999)}s`);
    } catch (err) {
      fail(err.message || "出错了");
    }
  }, 3000);
}

function done(id, task) {
  clearInterval(state.pollTimer);
  state.busy = false;
  resetCta();
  showProgress("收工 ✓");
  $("#progress").classList.add("done");
  note("成片出炉啦", false);
  const url = `${API}/v1/videos/${id}/content`;
  $("#video").src = url;
  $("#dl").href = url;
  $("#result-title").textContent = `影像出炉 · ${task.seconds || state.duration}s · ${task.ratio || state.ratio}`;
  $("#result").hidden = false;
  $("#result").scrollIntoView({ behavior: "smooth", block: "nearest" });
  saveHistory(id, task);
  toast("生成完成，已出片");
}

function fail(msg) {
  clearInterval(state.pollTimer);
  state.busy = false;
  resetCta();
  note("这版没拍成，再试试", false);
  $("#progress").hidden = true;
  toast(msg, 4200);
}

function resetCta() {
  $("#gen").classList.remove("busy");
  $("#gen-label").textContent = "开拍";
}

function showProgress(text) {
  const p = $("#progress");
  p.hidden = false;
  if (text !== "收工 ✓") p.classList.remove("done");
  $("#progress-text").textContent = text;
}

$("#copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#dl").href); toast("链接已复制"); }
  catch { toast("复制失败"); }
});
$("#again").addEventListener("click", () => {
  $("#result").hidden = true;
  $("#progress").hidden = true;
  $("#progress").classList.remove("done");
  $("#composer").scrollIntoView({ behavior: "smooth", block: "center" });
});

/* ── history (localStorage) ────────────────────────────────── */
const HKEY = "h3-studio-history-v1";
function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HKEY) || "[]"); } catch { return []; }
}
function saveHistory(id, task) {
  const list = loadHistory().filter((x) => x.id !== id);
  list.unshift({ id, ratio: task.ratio || state.ratio, sec: task.seconds || state.duration, ts: Date.now() });
  localStorage.setItem(HKEY, JSON.stringify(list.slice(0, 8)));
  renderHistory();
}
function renderHistory() {
  const list = loadHistory();
  $("#history-wrap").hidden = !list.length;
  $("#history").innerHTML = list
    .map(
      (h) => `<div class="hchip" data-id="${h.id}">
        <video src="/v1/videos/${h.id}/content" muted preload="metadata"></video>
        <div class="hchip-meta"><b>${h.ratio} · ${h.sec}s</b><small>${new Date(h.ts).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</small></div>
      </div>`
    )
    .join("");
}
$("#history").addEventListener("click", async (e) => {
  const chip = e.target.closest(".hchip");
  if (!chip) return;
  const id = chip.dataset.id;
  try {
    const r = await apiFetch(`${API}/v1/videos/${id}`);
    const t = await r.json();
    if (t.status === "succeeded") done(id, t);
    else if (t.status === "failed") toast("这条素材没拍成");
    else { state.taskId = id; state.busy = true; $("#gen").classList.add("busy"); poll(id); note("片场开机，渲染中…", true, true); }
  } catch { toast("素材不存在或已过期"); }
});

/* ── examples — polaroid cards ─────────────────────────────── */
function renderExamples() {
  $("#exgrid").innerHTML = EXAMPLES.map(
    (x, i) => `<div class="excard" data-i="${i}" tabindex="0" role="button" aria-label="使用示例 ${x.name}">
      <img src="${x.url}" alt="${x.name}" loading="lazy" />
      <span class="excard-go">→</span>
      <div class="excard-info"><b>${x.name}</b><small>${x.tag}</small></div>
    </div>`
  ).join("");
}
$("#exgrid").addEventListener("click", (e) => {
  const card = e.target.closest(".excard");
  if (card) pickExample(card);
});
$("#exgrid").addEventListener("keydown", (e) => {
  const card = e.target.closest(".excard");
  if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pickExample(card); }
});
async function pickExample(card) {
  const x = EXAMPLES[+card.dataset.i];
  card.classList.add("loading");
  try {
    const blob = await (await fetch(x.url)).blob();
    const dataUrl = await new Promise((res) => {
      const r = new FileReader();
      r.onload = () => res(r.result);
      r.readAsDataURL(blob);
    });
    setImage(dataUrl, `${x.name} · 样片`, `${fmtSize(blob.size)} · example`);
    $("#composer").scrollIntoView({ behavior: "smooth", block: "center" });
    toast(`已装入「${x.name}」，点「开拍」`);
  } catch {
    toast("样片加载失败");
  } finally {
    setTimeout(() => card.classList.remove("loading"), 700);
  }
}

/* ── boot ──────────────────────────────────────────────────── */
renderExamples();
renderHistory();
setTimeout(() => note("引擎就绪，随时开拍"), 800);
