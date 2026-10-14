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
const loaders = { dash: loadStatus, wifi: loadWifi, wan: loadWan, clients: loadClients, device: loadDevice };
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
  $("#d-uptime").textContent = fmtDur(sys.uptime);
  $("#d-mem").textContent = sys.mem_total_kb
    ? `${Math.round((sys.mem_total_kb - sys.mem_avail_kb) / 1024)} / ${Math.round(sys.mem_total_kb / 1024)} MB` : "-";
  $("#d-load").textContent = (sys.load || []).join(" ");
  $("#d-temp").textContent = dash(sys.temp_c, " °C");
}

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

async function loadWan() {
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
async function loadClients() {
  const { clients } = await api("/api/clients");
  const body = $("#clients-body");
  if (!clients.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5; td.className = "muted"; td.textContent = "접속한 기기가 없습니다";
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
    return tr;
  }));
}
$("#clients-refresh").addEventListener("click", () => loadClients().catch((e) => toast(e.message)));

// ---------- 기기 관리 ----------
async function loadDevice() {
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
