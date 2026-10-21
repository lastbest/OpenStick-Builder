"use strict";
// OpenStick 관리페이지 프런트엔드 (프레임워크 없음)

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
let csrf = null;
let pollTimer = null;
let lastWan = null;

// ---------- 공통 ----------
async function api(path, body) {
  const opt = { method: body === undefined ? "GET" : "POST", headers: {}, credentials: "same-origin" };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.headers["X-CSRF-Token"] = csrf || "";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch { /* 빈 응답 */ }
  if (r.status === 401 && path !== "/api/login") { showAuth(false); throw new Error(data.error || "로그인이 필요합니다"); }
  if (!r.ok) throw new Error(data.error || `오류 (${r.status})`);
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.add("hidden"), 3000);
}

function setResult(form, msg, ok) {
  const el = $(".result", form);
  el.textContent = msg;
  el.className = "result " + (ok ? "ok" : "err");
}

async function busy(btn, fn) {
  btn.disabled = true;
  try { return await fn(); } finally { btn.disabled = false; }
}

const fmtBytes = (n) => {
  if (n == null) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};
const fmtRate = (bps) => (bps == null ? "-" : bps >= 1e6 ? `${(bps / 1e6).toFixed(1)} Mbps` : `${(bps / 1e3).toFixed(0)} kbps`);
const fmtDur = (s) => {
  if (s == null) return "-";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}일 ${h}시간` : h ? `${h}시간 ${m}분` : `${m}분`;
};
const dash = (v, unit = "") => (v == null || v === "" ? "-" : `${typeof v === "number" ? Math.round(v * 10) / 10 : v}${unit}`);

function signalLevel(rsrp, quality) {
  if (rsrp != null) return rsrp >= -85 ? 4 : rsrp >= -95 ? 3 : rsrp >= -105 ? 2 : rsrp >= -115 ? 1 : 0;
  if (quality != null) return Math.min(4, Math.ceil(quality / 25));
  return 0;
}
function setBars(el, level) { $$("i", el).forEach((b, i) => b.classList.toggle("on", i < level)); }

function kv(el, rows) {
  el.replaceChildren(...rows.flatMap(([k, v]) => {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v ?? "-";
    return [dt, dd];
  }));
}

// ---------- 로그인 ----------
function showAuth(setup) {
  stopPoll();
  $("#app").classList.add("hidden");
  $("#auth").classList.remove("hidden");
  $("#auth-form").dataset.setup = setup ? "1" : "";
  $("#auth-desc").textContent = setup
    ? "처음 사용합니다. 관리자 비밀번호(8자 이상)를 정하세요."
    : "관리자 비밀번호를 입력하세요.";
  $("#auth-pw2").classList.toggle("hidden", !setup);
  $("#auth-form button").textContent = setup ? "설정하고 시작" : "로그인";
  $("#auth-pw").focus();
}

$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const setup = !!e.target.dataset.setup;
  const pw = $("#auth-pw").value;
  $("#auth-err").textContent = "";
  if (setup && pw !== $("#auth-pw2").value) { $("#auth-err").textContent = "비밀번호 확인이 다릅니다"; return; }
  try {
    const r = await api(setup ? "/api/setup" : "/api/login", { password: pw });
    csrf = r.csrf;
    $("#auth-pw").value = $("#auth-pw2").value = "";
    enterApp();
  } catch (err) { $("#auth-err").textContent = err.message; }
});

$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", {}); } catch { /* 무시 */ }
  csrf = null;
  showAuth(false);
});

// ---------- 탭 ----------
const loaders = { dash: loadStatus, sms: loadSms, wifi: loadWifi, wan: loadWan, clients: loadClients, diag: loadDiag, device: loadDevice };
$$(".tabs button").forEach((b) => b.addEventListener("click", () => openTab(b.dataset.tab)));

function openTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== `tab-${name}`));
  try { localStorage.setItem("osa-tab", name); } catch { /* 무시 */ }
  history.replaceState(null, "", `#${name}`);
  loaders[name]().catch((e) => toast(e.message));
}

function enterApp() {
  $("#auth").classList.add("hidden");
  $("#app").classList.remove("hidden");
  let tab = location.hash.slice(1);
  if (!loaders[tab]) {
    try { tab = localStorage.getItem("osa-tab") || "dash"; } catch { tab = "dash"; }
  }
  openTab(loaders[tab] ? tab : "dash");
  startPoll();
}

function startPoll() { stopPoll(); pollTimer = setInterval(() => loadStatus().catch(() => {}), 3000); }
function stopPoll() { clearInterval(pollTimer); pollTimer = null; lastWan = null; }

// ---------- 대시보드 ----------
async function loadStatus() {
  const s = await api("/api/status");
  const m = s.modem, w = s.wan, sys = s.system;
  const level = signalLevel(m.rsrp, m.quality);
  setBars($("#top-signal"), level);
  setBars($("#d-bars"), level);
  $("#top-op").textContent = `${m.operator || "-"} ${(m.access_tech[0] || "").toUpperCase()}`;
  $("#d-quality").textContent = m.quality != null ? `${Math.round(m.quality)}%` : "-";
  $("#d-rsrp").textContent = dash(m.rsrp, " dBm");
  $("#d-rsrq").textContent = dash(m.rsrq, " dB");
  $("#d-snr").textContent = dash(m.snr, " dB");
  $("#d-conn").textContent = w.connected ? "연결됨" : "연결 안 됨";
  $("#d-conn").style.color = w.connected ? "var(--ok)" : "var(--danger)";
  $("#d-op").textContent = m.operator || "-";
  $("#d-tech").textContent = (m.access_tech || []).join(", ").toUpperCase() || "-";
  $("#d-ip").textContent = w.ipv4 || "-";
  $("#d-apn").textContent = w.apn || "-";
  $("#d-sim").textContent = m.sim || "-";

  if (lastWan && w.ts > lastWan.ts) {
    const dt = w.ts - lastWan.ts;
    $("#d-down").textContent = fmtRate(Math.max(0, w.lan_down_bytes - lastWan.lan_down_bytes) * 8 / dt);
    $("#d-up").textContent = fmtRate(Math.max(0, w.lan_up_bytes - lastWan.lan_up_bytes) * 8 / dt);
  }
  lastWan = w;
  $("#d-total").textContent = `↓${fmtBytes(w.rx_bytes)} ↑${fmtBytes(w.tx_bytes)}` +
    (w.connected_sec ? ` (${fmtDur(w.connected_sec)})` : "");

  $("#d-clients").textContent = `${s.clients}대`;
  setSmsBadge(s.unread);
  $("#d-uptime").textContent = fmtDur(sys.uptime);
  $("#d-mem").textContent = sys.mem_total_kb
    ? `${Math.round((sys.mem_total_kb - sys.mem_avail_kb) / 1024)} / ${Math.round(sys.mem_total_kb / 1024)} MB` : "-";
  $("#d-load").textContent = (sys.load || []).join(" ");
  $("#d-temp").textContent = dash(sys.temp_c, " °C");
  const u = s.usage;
  if (u) {
    const pct = u.percent;
    $("#d-usage").textContent = fmtBytes(u.period.used) + (u.cap_bytes ? ` / ${fmtBytes(u.cap_bytes)}` : "");
    const bar = $("#d-usage-bar");
    bar.style.width = `${Math.min(100, pct || 0)}%`;
    bar.className = pct == null ? "" : pct >= 100 ? "over" : pct >= u.warn_pct ? "warn" : "";
    $("#d-usage-today").textContent = `↓${fmtBytes(u.today.rx)} ↑${fmtBytes(u.today.tx)}`;
    $("#d-usage-period").textContent = `${u.period.start} ~`;
    const w = $("#d-usage-warn");
    w.textContent = u.blocked_now ? "⛔ 월 한도 초과 — 접속 기기 인터넷 차단 중"
      : pct != null && pct >= u.warn_pct ? `⚠️ 월 한도의 ${pct}% 사용` : (u.cap_bytes ? "" : "월 한도 미설정");
    w.className = "small " + (u.blocked_now || (pct != null && pct >= u.warn_pct) ? "error" : "muted");
  }
  const st = s.storage;
  if (st) {
    $("#d-disk").textContent = `${fmtBytes(st.disk_free_bytes)} / ${fmtBytes(st.disk_total_bytes)}`;
    $("#d-msgs").textContent = `${st.messages}건 (${fmtBytes(st.db_bytes)})`;
    $("#d-media").textContent = `${fmtBytes(st.media_bytes)} / ${fmtBytes(st.media_cap_bytes)}`;
    $("#d-prune").textContent = `한도를 넘거나 디스크 여유가 ${fmtBytes(st.min_free_bytes)} 미만이면 오래된 MMS 사진부터 자동 정리(글은 유지)` +
      (st.pruned ? ` · 정리된 MMS ${st.pruned}건` : "");
  }
}

// ---------- 문자 ----------
let smsOldest = null;

function setSmsBadge(n) {
  const b = $("#sms-badge");
  b.textContent = n > 99 ? "99+" : String(n || "");
  b.classList.toggle("hidden", !n);
}

function smsSegments(text) {
  const gsm = /^[\x20-\x7E\n\r£¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉÄÖÑÜ§¿äöñüà]*$/.test(text);
  const n = [...text].length;
  const [one, many] = gsm ? [160, 153] : [70, 67];
  return { n, parts: n === 0 ? 0 : n <= one ? 1 : Math.ceil(n / many) };
}
// ---------- MMS 첨부 ----------
const MMS_MAX = 1024 * 1024;     // 서버와 같은 한도(합계)
const IMG_TARGET = 300 * 1024;   // 통신사가 대개 이 근처에서 자르므로 사진은 미리 줄여서 보냄
const IMG_MAX_PX = 1280;
let atts = [];

const b64 = (buf) => {
  const b = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return btoa(s);
};

// 기기에는 이미지 라이브러리가 없으므로 축소는 브라우저에서 한다.
async function shrink(file) {
  if (!file.type.startsWith("image/") || file.type === "image/gif") return file;
  const bmp = await createImageBitmap(file).catch(() => null);
  if (!bmp) return file;
  const scale = Math.min(1, IMG_MAX_PX / Math.max(bmp.width, bmp.height));
  if (scale === 1 && file.size <= IMG_TARGET) return file;
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * scale);
  c.height = Math.round(bmp.height * scale);
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  bmp.close();
  for (const q of [0.8, 0.6, 0.45]) {
    const blob = await new Promise((r) => c.toBlob(r, "image/jpeg", q));
    if (!blob) break;
    if (blob.size <= IMG_TARGET || q === 0.45) {
      return new File([blob], file.name.replace(/\.[^.]*$/, "") + ".jpg", { type: "image/jpeg" });
    }
  }
  return file;
}

function mmsMode() {
  return atts.length > 0 || !!$("#sms-form").subject.value.trim() || smsSegments($("#sms-form").text.value).n > 1000;
}

function syncSms() {
  const f = $("#sms-form");
  const { n, parts } = smsSegments(f.text.value);
  const mms = mmsMode();
  $("#sms-kind").classList.toggle("hidden", !mms);
  $("#sms-count").textContent = mms
    ? `${n}자 · MMS 1건`
    : `${n}자 · SMS ${parts}건${parts > 1 ? " (나뉘어 발송, 건별 요금)" : ""}`;
  const total = atts.reduce((s, a) => s + a.size, 0);
  $("#sms-attinfo").textContent = atts.length
    ? `첨부 ${atts.length}개 · ${fmtBytes(total)}${total > IMG_TARGET ? " (통신사에서 거부될 수 있는 크기)" : ""}`
    : "";
  $("#sms-atts").replaceChildren(...atts.map((a, i) => {
    const li = el("li");
    if (a.type.startsWith("image/")) {
      const img = el("img");
      img.src = `data:${a.type};base64,${a.data}`;   // CSP가 blob:은 막고 data:는 허용한다
      img.alt = "";
      li.append(img);
    } else {
      li.append(el("span", "tag", a.type.split("/")[1] || "파일"));
    }
    li.append(el("span", "nm", a.name), el("span", "sz", fmtBytes(a.size)));
    const x = el("button", "ghost small", "✕");
    x.type = "button";
    x.addEventListener("click", () => { atts.splice(i, 1); syncSms(); });
    li.append(x);
    return li;
  }));
}

$("#sms-form").text.addEventListener("input", syncSms);
$("#sms-form").subject.addEventListener("input", syncSms);
$("#sms-subj-btn").addEventListener("click", () => {
  $("#sms-subj-row").classList.toggle("hidden");
  if (!$("#sms-subj-row").classList.contains("hidden")) $("#sms-form").subject.focus();
  else { $("#sms-form").subject.value = ""; syncSms(); }
});
$("#sms-attach").addEventListener("click", () => $("#sms-files").click());
$("#sms-files").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  await busy($("#sms-attach"), async () => {
    for (const raw of files) {
      if (atts.length >= 10) { toast("첨부는 최대 10개"); break; }
      let file;
      try { file = await shrink(raw); } catch { file = raw; }
      const total = atts.reduce((s, a) => s + a.size, 0) + file.size;
      if (total > MMS_MAX) { toast(`${file.name}: 합계 ${fmtBytes(MMS_MAX)}를 넘어 제외했습니다`); continue; }
      atts.push({
        name: file.name, type: file.type || "application/octet-stream", size: file.size,
        data: b64(await file.arrayBuffer()),
      });
    }
  });
  syncSms();
});

const fmtTime = (ts) => {
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
};
const STATUS = { pending: "MMS 받는 중", error: "MMS 받기 실패", expired: "MMS 기간 만료",
                 sending: "보내는 중", failed: "발송 실패" };

const fmtNum = (n) => (n || "").replace(/^\+82/, "0");

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function renderMsg(m) {
  const li = el("li", "msg" + (m.direction === "in" && !m.read ? " unread" : ""));
  const head = el("div", "msg-head");
  const sel = el("input", "msg-sel"); sel.type = "checkbox"; sel.dataset.id = m.id;
  sel.addEventListener("change", syncBulk);
  head.append(sel);
  head.append(el("span", "msg-dir", m.direction === "in" ? "받음" : "보냄"), el("span", "msg-num", fmtNum(m.number) || "(번호 없음)"));
  if (m.kind === "mms") head.append(el("span", "tag", "MMS"));
  if (STATUS[m.status]) head.append(el("span", "tag err", STATUS[m.status]));
  head.append(el("span", "msg-time", fmtTime(m.ts)));
  li.append(head);
  if (m.subject && m.subject !== "[제목없음]") li.append(el("div", "msg-sub", m.subject));
  if (m.text) li.append(el("div", "msg-body", m.text));
  if (m.parts && m.parts.length) {
    const media = el("div", "msg-media");
    m.parts.forEach((p, i) => {
      const url = `/api/messages/media?id=${m.id}&part=${i}`;
      if (p.type.startsWith("image/")) {
        const a = el("a"); a.href = url; a.target = "_blank"; a.rel = "noopener";
        const img = el("img"); img.src = url; img.alt = p.name || "사진"; img.loading = "lazy";
        a.append(img); media.append(a);
      } else {
        const a = el("a", "tag", `📎 ${p.name || p.type}`); a.href = url; media.append(a);
      }
    });
    li.append(media);
  }
  if (m.pruned) li.append(el("div", "muted small", "첨부 정리됨 (저장공간 확보를 위해 사진 삭제, 글은 유지)"));
  if (m.error) li.append(el("div", "muted small", m.error));
  const act = el("div", "msg-actions");
  if (m.number) {
    const r = el("button", "ghost small", "답장");
    r.addEventListener("click", () => {
      const f = $("#sms-form"); f.number.value = fmtNum(m.number); f.text.focus();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    act.append(r);
  }
  if (m.kind === "mms" && (m.status === "error" || m.status === "pending")) {
    const r = el("button", "ghost small", "다시 받기");
    r.addEventListener("click", () => api("/api/messages/retry", { id: m.id }).then(() => toast("다시 받는 중…")).catch((e) => toast(e.message)));
    act.append(r);
  }
  const d = el("button", "ghost small", "삭제");
  d.addEventListener("click", async () => {
    if (!confirm("이 문자를 삭제할까요?")) return;
    try { await api("/api/messages/delete", { ids: [m.id] }); li.remove(); } catch (e) { toast(e.message); }
  });
  act.append(d);
  li.append(act);
  return li;
}

async function loadSms(more = false) {
  const r = await api(`/api/messages?limit=50${more && smsOldest ? `&before=${smsOldest}` : ""}`);
  const list = $("#sms-list");
  const items = r.messages.map(renderMsg);
  if (more) list.append(...items); else list.replaceChildren(...items);
  if (!more && !items.length) list.append(el("li", "muted", "문자가 없습니다"));
  const last = r.messages[r.messages.length - 1];
  if (last) smsOldest = `${last.ts}:${last.id}`;
  $("#sms-more").classList.toggle("hidden", r.messages.length < 50);
  $("#sms-info").textContent = r.error ? `수집 오류: ${r.error}` :
    r.last_poll ? `마지막 확인 ${fmtTime(r.last_poll)} · 15초마다 자동 수신` : "";
  setSmsBadge(r.unread);
  syncBulk();
  const unread = r.messages.filter((m) => m.direction === "in" && !m.read).map((m) => m.id);
  if (unread.length) api("/api/messages/read", { ids: unread }).then(() => setSmsBadge(Math.max(0, r.unread - unread.length))).catch(() => {});
}
function selectedIds() { return $$(".msg-sel:checked").map((c) => +c.dataset.id); }
function syncBulk() {
  const n = selectedIds().length;
  $("#sms-delsel").disabled = !n;
  $("#sms-delsel").textContent = n ? `선택 삭제 (${n})` : "선택 삭제";
}
$("#sms-selall").addEventListener("change", (e) => { $$(".msg-sel").forEach((c) => { c.checked = e.target.checked; }); syncBulk(); });
$("#sms-delsel").addEventListener("click", async () => {
  const ids = selectedIds();
  if (!ids.length || !confirm(`선택한 문자 ${ids.length}건을 삭제할까요?`)) return;
  try { const r = await api("/api/messages/delete", { ids }); toast(`${r.deleted}건 삭제`); $("#sms-selall").checked = false; loadSms(); }
  catch (e) { toast(e.message); }
});
$("#sms-delold").addEventListener("click", async () => {
  const days = +$("#sms-olddays").value;
  const label = $("#sms-olddays").selectedOptions[0].textContent;
  if (!confirm(`${label} 넘게 지난 문자를 모두 삭제할까요? (사진 포함, 되돌릴 수 없음)`)) return;
  try { const r = await api("/api/messages/delete", { older_than_days: days }); toast(`${r.deleted}건 삭제`); loadSms(); }
  catch (e) { toast(e.message); }
});

$("#sms-more").addEventListener("click", () => loadSms(true).catch((e) => toast(e.message)));
$("#sms-refresh").addEventListener("click", () => loadSms().catch((e) => toast(e.message)));
$("#sms-readall").addEventListener("click", () => api("/api/messages/read", {}).then(() => loadSms()).catch((e) => toast(e.message)));

$("#sms-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  const mms = mmsMode();
  if (!mms && !f.text.value.trim()) { setResult(f, "내용을 입력하세요.", false); return; }
  const { parts } = smsSegments(f.text.value);
  if (!mms && parts > 1 && !confirm(`긴 문자라 SMS ${parts}건으로 나뉘어 발송됩니다. 보낼까요?`)) return;
  busy($("button.primary", f), async () => {
    try {
      if (mms) {
        await api("/api/messages/send-mms", {
          number: f.number.value, text: f.text.value, subject: f.subject.value,
          attachments: atts.map((a) => ({ name: a.name, type: a.type, data: a.data })),
        });
      } else {
        await api("/api/messages/send", { number: f.number.value, text: f.text.value });
      }
      setResult(f, "보냈습니다.", true);
      f.text.value = "";
      f.subject.value = "";
      $("#sms-subj-row").classList.add("hidden");
      atts = [];
      syncSms();
      loadSms().catch(() => {});
    } catch (err) { setResult(f, err.message, false); }
  });
});

// ---------- WiFi ----------
const chSel = $('#wifi-form select[name="channel"]');
for (let c = 1; c <= 13; c++) chSel.add(new Option(`${c}`, c));

async function loadWifi() {
  const w = await api("/api/wifi");
  const f = $("#wifi-form");
  f.ssid.value = w.ssid;
  f.password.value = "";
  f.security.value = w.security;
  f.channel.value = w.channel;
  f.max_clients.value = w.max_clients || 0;
  f.hidden.checked = w.hidden;
  f.isolate.checked = w.isolate;
  setResult(f, w.running ? "" : "WiFi(hostapd)가 꺼져 있습니다", w.running);
}

$("#wifi-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    ssid: f.ssid.value, security: f.security.value, channel: +f.channel.value,
    max_clients: +f.max_clients.value || 0, hidden: f.hidden.checked, isolate: f.isolate.checked,
  };
  if (f.password.value) body.password = f.password.value;
  busy($("button", f), async () => {
    try {
      await api("/api/wifi", body);
      setResult(f, "저장했습니다. WiFi가 재시작됩니다.", true);
      f.password.value = "";
    } catch (err) { setResult(f, err.message, false); }
  });
});

// ---------- 인터넷 ----------
function syncApnFieldset() {
  $("#apn-manual").disabled = $('#apn-form input[name="apn_mode"]:checked')?.value !== "manual";
}
$$('#apn-form input[name="apn_mode"]').forEach((r) => r.addEventListener("change", syncApnFieldset));

async function loadUsage() {
  const u = await api("/api/usage");
  const f = $("#usage-form");
  f.cap_gb.value = u.cap_bytes ? +(u.cap_bytes / 2 ** 30).toFixed(1) : 0;
  f.reset_day.value = u.reset_day;
  f.warn_pct.value = u.warn_pct;
  f.block.checked = u.block_over_cap;
  kv($("#usage-info"), [
    ["이번 기간", `${u.period.start} ~ (매월 ${u.reset_day}일 초기화)`],
    ["사용", `${fmtBytes(u.period.used)}${u.cap_bytes ? ` / ${fmtBytes(u.cap_bytes)} (${u.percent}%)` : ""}`],
    ["받음 / 보냄", `${fmtBytes(u.period.rx)} / ${fmtBytes(u.period.tx)}`],
    ["오늘", `↓${fmtBytes(u.today.rx)} ↑${fmtBytes(u.today.tx)}`],
  ].concat(u.blocked_now ? [["상태", "⛔ 한도 초과로 차단 중"]] : []));
  const max = Math.max(1, ...u.days.map((d) => d.rx + d.tx));
  const today = new Date().toISOString().slice(0, 10);
  $("#usage-days").replaceChildren(...u.days.map((d) => {
    const b = el("div", d.day === today ? "today" : "");
    b.style.height = `${Math.max(2, (d.rx + d.tx) * 100 / max)}%`;
    b.title = `${d.day}: ${fmtBytes(d.rx + d.tx)}`;
    return b;
  }));
}
$("#usage-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  busy($("button.primary", f), async () => {
    try {
      await api("/api/usage", { cap_gb: +f.cap_gb.value || 0, reset_day: +f.reset_day.value, warn_pct: +f.warn_pct.value, block: f.block.checked });
      setResult(f, "저장했습니다.", true); loadUsage().catch(() => {});
    } catch (err) { setResult(f, err.message, false); }
  });
});
$("#usage-reset").addEventListener("click", async () => {
  if (!confirm("지금까지 쌓인 데이터 사용량 기록을 모두 지울까요?")) return;
  try { await api("/api/usage/reset", {}); toast("사용량 기록을 초기화했습니다"); loadUsage(); } catch (e) { toast(e.message); }
});

async function loadWan() {
  loadUsage().catch((e) => toast(e.message));
  const w = await api("/api/wan");
  const d = $("#data-form");
  d.data_enabled.checked = w.data_enabled;
  d.roaming_allowed.checked = w.roaming_allowed;
  const f = $("#apn-form");
  $$('input[name="apn_mode"]', f).forEach((r) => { r.checked = r.value === w.apn_mode; });
  f.apn.value = w.apn;
  f.username.value = w.username;
  f.password.value = "";
  f.ip_type.value = w.ip_type;
  syncApnFieldset();
  const b = w.bearer;
  kv($("#bearer-info"), b ? [
    ["현재 APN", b.apn], ["IP", b.ipv4], ["IP 타입", b.ip_type], ["DNS", (b.dns || []).join(", ")],
  ] : [["상태", "연결 안 됨"]]);
}

$("#data-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  if (!f.data_enabled.checked && !confirm("모바일 데이터를 끄면 이 동글을 통한 인터넷이 모두 끊깁니다. 계속할까요?")) return;
  busy($("button", f), async () => {
    try {
      await api("/api/wan/data", { data_enabled: f.data_enabled.checked, roaming_allowed: f.roaming_allowed.checked });
      setResult(f, "저장했습니다.", true);
      loadWan().catch(() => {});
    } catch (err) { setResult(f, err.message, false); }
  });
});

$("#apn-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  const mode = $('input[name="apn_mode"]:checked', f)?.value;
  if (!mode) { setResult(f, "자동/수동을 선택하세요", false); return; }
  const body = { apn_mode: mode, ip_type: f.ip_type.value };
  if (mode === "manual") Object.assign(body, { apn: f.apn.value.trim(), username: f.username.value, password: f.password.value });
  setResult(f, "LTE 재연결 중… (최대 1~2분)", true);
  busy($("button", f), async () => {
    try {
      const r = await api("/api/wan/apn", body);
      if (r.ok) setResult(f, `연결 성공 — IP ${r.bearer?.ipv4 || "-"}`, true);
      else setResult(f, `${r.error}${r.restored_connected ? " (이전 설정으로 연결 복구됨)" : ""}`, false);
      loadWan().catch(() => {});
    } catch (err) { setResult(f, err.message, false); }
  });
});

// ---------- 접속 기기 ----------
async function setBlock(mac, name, block) {
  if (block && !confirm(`${name || mac} 기기의 인터넷을 차단할까요?`)) return;
  try { await api(block ? "/api/clients/block" : "/api/clients/unblock", { mac, name }); loadClients(); }
  catch (e) { toast(e.message); }
}

async function loadClients() {
  const { clients, blocked_offline: offline } = await api("/api/clients");
  const bl = $("#blocked-list");
  bl.replaceChildren(...(offline || []).map((b) => {
    const li = el("li");
    const btn = el("button", "ghost small", "해제");
    btn.addEventListener("click", () => setBlock(b.mac, b.name, false));
    li.append(el("span", "", `${b.name || "(이름 없음)"} · ${b.mac}`), btn);
    return li;
  }));
  $("#blocked-card").classList.toggle("hidden", !(offline || []).length);
  const body = $("#clients-body");
  if (!clients.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6; td.className = "muted"; td.textContent = "접속한 기기가 없습니다";
    tr.append(td); body.replaceChildren(tr); return;
  }
  body.replaceChildren(...clients.map((c) => {
    const tr = document.createElement("tr");
    [["이름", c.name || "(이름 없음)"], ["IP", c.ip || "-"], ["MAC", c.mac],
     ["연결", c.via + (c.signal_dbm != null ? ` (${Math.round(c.signal_dbm)} dBm)` : "")],
     ["접속 시간", c.connected_sec != null ? fmtDur(c.connected_sec) : "-"]].forEach(([label, v]) => {
      const td = document.createElement("td");
      td.dataset.label = label; td.textContent = v; tr.append(td);
    });
    const td = el("td"); td.dataset.label = "인터넷";
    const btn = el("button", c.blocked ? "danger small" : "ghost small", c.blocked ? "차단됨 · 해제" : "차단");
    btn.addEventListener("click", () => setBlock(c.mac, c.name, !c.blocked));
    td.append(btn); tr.append(td);
    return tr;
  }));
}
$("#clients-refresh").addEventListener("click", () => loadClients().catch((e) => toast(e.message)));

// ---------- 진단 ----------
async function loadDiag() {
  const d = await api("/api/dns");
  $("#dns-presets").replaceChildren(...d.presets.map((p) => {
    const l = el("label", "radio");
    const r = el("input"); r.type = "radio"; r.name = "preset"; r.value = p.id; r.checked = p.id === d.current;
    l.append(r, document.createTextNode(" " + p.label));
    return l;
  }));
  setResult($("#dns-form"), d.state === "active" ? "" : `dnsproxy 상태: ${d.state}`, d.state === "active");
}
$("#dns-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  const preset = $('input[name="preset"]:checked', f)?.value;
  if (!preset) return;
  setResult(f, "적용 중… (최대 1분 정도 걸릴 수 있습니다)", true);
  busy($("button", f), async () => {
    try { await api("/api/dns", { preset }); setResult(f, "적용했습니다.", true); }
    catch (err) { setResult(f, err.message, false); loadDiag().catch(() => {}); }
  });
});
$$("#diag-form button[data-kind]").forEach((b) => b.addEventListener("click", () => {
  const kind = b.dataset.kind, host = $("#diag-form").host.value.trim(), out = $("#diag-out");
  out.classList.remove("hidden");
  out.textContent = kind === "traceroute" ? "경로 추적 중… (최대 20~40초)" : "실행 중…";
  busy(b, async () => {
    try {
      const r = await api("/api/diag", { kind, host });
      if (kind === "ping") out.textContent = r.text;
      else if (kind === "lookup") out.textContent = `${r.name} (조회: ${r.server})\nA     ${r.A.join(", ") || "-"}\nAAAA  ${r.AAAA.join(", ") || "-"}`;
      else out.textContent = r.hops.map((h) => `${String(h.ttl).padStart(2)}  ${h.ip || "*"}${h.ms != null ? `  ${h.ms} ms` : ""}`).join("\n");
    } catch (err) { out.textContent = err.message; }
  });
}));
$("#speed-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target, mb = +f.mb.value;
  if (!confirm(`LTE 데이터 ${mb} MB를 사용합니다. 측정할까요?`)) return;
  setResult(f, "측정 중…", true);
  busy($("button", f), async () => {
    try { const r = await api("/api/diag", { kind: "speedtest", mb }); setResult(f, `다운로드 ${r.mbps} Mbps (${fmtBytes(r.bytes)}, ${r.seconds}초)`, true); }
    catch (err) { setResult(f, err.message, false); }
  });
});

// ---------- 기기 관리 ----------
async function loadSystem() {
  const s = await api("/api/system");
  $("#leds-on").checked = s.leds === "on";
  const tz = $("#tz");
  if (![...tz.options].some((o) => o.value === s.time.timezone)) tz.add(new Option(s.time.timezone, s.time.timezone));
  tz.value = s.time.timezone;
  $("#time-info").textContent = `현재 ${s.time.local}${s.time.ntp_synced ? " · 시간 동기화됨" : " · 시간 동기화 안 됨"}`;
  $("#svc-body").replaceChildren(...s.services.map((v) => {
    const tr = el("tr");
    const st = el("td", v.state === "active" ? "state-active" : "state-bad", v.state);
    const act = el("td");
    if (v.can_restart) {
      const b = el("button", "ghost small", "재시작");
      b.addEventListener("click", () => {
        if (!confirm(`${v.name} 재시작: ${v.warning}. 계속할까요?`)) return;
        busy(b, async () => { try { await api("/api/system/restart", { name: v.name }); toast(`${v.name} 재시작함`); loadSystem(); } catch (e) { toast(e.message); } });
      });
      act.append(b);
    }
    tr.append(el("td", "", v.name), st, act);
    return tr;
  }));
}
$("#leds-on").addEventListener("change", (e) => api("/api/system/leds", { mode: e.target.checked ? "on" : "off" }).then(() => toast("표시등 설정을 바꿨습니다")).catch((err) => toast(err.message)));
$("#tz").addEventListener("change", (e) => api("/api/system/timezone", { timezone: e.target.value }).then(() => { toast("시간대를 바꿨습니다"); loadSystem(); }).catch((err) => toast(err.message)));
$("#svc-refresh").addEventListener("click", () => loadSystem().catch((e) => toast(e.message)));
$("#log-load").addEventListener("click", (e) => busy(e.target, async () => {
  const out = $("#log-out");
  try {
    const r = await api(`/api/logs?unit=${$("#log-unit").value}&lines=${$("#log-lines").value}`);
    out.textContent = r.text || "(로그 없음)";
    out.classList.remove("hidden");
    out.scrollTop = out.scrollHeight;
  } catch (err) { toast(err.message); }
}));

async function loadDevice() {
  loadSystem().catch((e) => toast(e.message));
  const d = await api("/api/device");
  kv($("#device-info"), [
    ["모델", d.model], ["호스트명", d.hostname], ["OS", d.os], ["커널", d.kernel],
    ["IMEI", d.imei], ["모뎀 펌웨어", d.modem_firmware], ["SIM 통신사", d.sim_operator],
    ["ICCID", d.iccid], ["IMSI", d.imsi], ["관리 IP", d.lan_ip],
  ]);
}

$("#pw-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.new.value !== f.new2.value) { setResult(f, "새 비밀번호 확인이 다릅니다", false); return; }
  busy($("button", f), async () => {
    try {
      const r = await api("/api/password", { old: f.old.value, new: f.new.value });
      csrf = r.csrf;
      f.reset();
      setResult(f, "변경했습니다. 다른 기기의 로그인은 모두 해제됩니다.", true);
    } catch (err) { setResult(f, err.message, false); }
  });
});

$("#reboot").addEventListener("click", (e) => {
  if (!confirm("기기를 재부팅할까요? 약 1분간 인터넷이 끊깁니다.")) return;
  busy(e.target, async () => {
    try {
      await api("/api/reboot", {});
      stopPoll();
      $("#reboot-result").textContent = "재부팅 중입니다. 1분 후 새로고침하세요.";
    } catch (err) { $("#reboot-result").textContent = err.message; }
  });
});

// ---------- 시작 ----------
(async () => {
  try {
    const s = await api("/api/session");
    $("#mock-badge").classList.toggle("hidden", !s.mock);
    if (s.logged_in) { csrf = s.csrf; enterApp(); } else showAuth(s.needs_setup);
  } catch (e) {
    showAuth(false);
    $("#auth-err").textContent = e.message;
  }
})();
