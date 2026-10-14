"""실제 기기 백엔드: mmcli / nmcli / hostapd / sysfs.

원칙: 셸을 쓰지 않고 인자 리스트로만 subprocess 실행, 입력은 전부 검증.
검증 실패는 ValueError (server.py가 400으로 변환).
"""
import json
import os
import re
import subprocess
import threading
import time

import netctl
import sms
import systools

LTE_CONN = "lte"                 # NetworkManager 연결 이름
HOSTAPD_CONF = "/etc/hostapd/hostapd.conf"
LEASES = "/var/lib/misc/dnsmasq.leases"
WLAN_IF = "wlan0"
LAN_BRIDGE = "br0"

_apn_lock = threading.Lock()
_wifi_lock = threading.Lock()


def run(args, timeout=15, check=True):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args[0]} 실패: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def mmcli_json(*args, timeout=15):
    try:
        return json.loads(run(["mmcli", "-J", *args], timeout=timeout))
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return {}


def _v(x):
    """mmcli의 '--' 등 빈 값을 None으로."""
    return None if x in (None, "", "--") else x


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Backend:
    def __init__(self):
        # 문자: 수신 수집기(백그라운드 스레드) + sqlite 보관
        self.msgs = sms.Messages()
        self.settings = netctl.Settings("/var/lib/openstick-admin/settings.json")
        self.net = netctl.NetCtl(self.settings)            # 데이터 사용량, 기기 차단 (nft inet osa)
        self.sys = systools.SysTools(self.settings, wan_connected=self._wan_up)
        # 신호 상세값(RSRP 등)은 주기 설정을 해야 갱신됨
        try:
            run(["mmcli", "-m", "any", "--signal-setup=10"], timeout=10, check=False)
        except Exception:  # noqa: BLE001
            pass

    # ================= 대시보드 =================
    def status(self):
        m = mmcli_json("-m", "any").get("modem", {})
        g, t = m.get("generic", {}), m.get("3gpp", {})
        sig = mmcli_json("-m", "any", "--signal-get").get("modem", {}).get("signal", {})
        lte = sig.get("lte", {})
        bearer = self._active_bearer(g.get("bearers") or [])
        return {
            "modem": {
                "state": _v(g.get("state")),
                "operator": _v(t.get("operator-name")),
                "registration": _v(t.get("registration-state")),
                "access_tech": g.get("access-technologies") or [],
                "quality": _num((g.get("signal-quality") or {}).get("value")),
                "rsrp": _num(_v(lte.get("rsrp"))), "rsrq": _num(_v(lte.get("rsrq"))),
                "snr": _num(_v(lte.get("snr"))), "rssi": _num(_v(lte.get("rssi"))),
                "sim": self._sim_state(g),
            },
            "wan": {
                "connected": bool(bearer and bearer["connected"]),
                "ipv4": bearer and bearer["ipv4"], "apn": bearer and bearer["apn"],
                # wwan0(bam-dmux)는 sysfs 카운터가 0이라 ModemManager 베어러 통계를 씀(이번 연결 기준)
                "rx_bytes": bearer["rx_bytes"] if bearer else 0,
                "tx_bytes": bearer["tx_bytes"] if bearer else 0,
                "connected_sec": bearer["duration"] if bearer else None,
                # 실시간 속도용: 접속 기기 쪽 브리지 카운터 (br0 tx = 기기로 내려간 양 = 다운로드)
                "lan_down_bytes": int(_read(f"/sys/class/net/{LAN_BRIDGE}/statistics/tx_bytes", "0") or 0),
                "lan_up_bytes": int(_read(f"/sys/class/net/{LAN_BRIDGE}/statistics/rx_bytes", "0") or 0),
                "ts": time.time(),
            },
            "clients": len(self.clients()["clients"]),
            "unread": self.msgs.unread(),
            "storage": self.msgs.storage(),
            "usage": self._usage_summary(),
            "system": self._system(),
        }

    def _sim_state(self, g):
        if not _v(g.get("sim")):
            return "없음"
        lock = _v(g.get("unlock-required"))
        if lock and lock not in ("sim-pin2", "none"):
            return f"잠김({lock})"
        return "정상"

    def _active_bearer(self, paths):
        for p in paths:
            b = mmcli_json("-b", p).get("bearer", {})
            st, pr, v4 = b.get("status", {}), b.get("properties", {}), b.get("ipv4-config", {})
            info = {
                "connected": st.get("connected") == "yes", "interface": _v(st.get("interface")),
                "apn": _v(pr.get("apn")), "ip_type": _v(pr.get("ip-type")),
                "roaming": _v(pr.get("roaming")),
                "ipv4": _v(v4.get("address")), "gateway": _v(v4.get("gateway")),
                "dns": [d for d in (v4.get("dns") or []) if _v(d)],
                "rx_bytes": int(_num(_v(b.get("stats", {}).get("bytes-rx"))) or 0),
                "tx_bytes": int(_num(_v(b.get("stats", {}).get("bytes-tx"))) or 0),
                "duration": int(_num(_v(b.get("stats", {}).get("duration"))) or 0),
            }
            if info["connected"]:
                return info
        return None

    def _system(self):
        mem = {}
        for line in (_read("/proc/meminfo", "") or "").splitlines():
            k, _, v = line.partition(":")
            mem[k] = int(v.split()[0]) if v.split() else 0
        temps = []
        base = "/sys/class/thermal"
        if os.path.isdir(base):
            for z in sorted(os.listdir(base)):
                t = _num(_read(f"{base}/{z}/temp"))
                if t:
                    temps.append(t / 1000)
        return {
            "uptime": int(float((_read("/proc/uptime", "0") or "0").split()[0])),
            "load": (_read("/proc/loadavg", "0 0 0") or "0").split()[:3],
            "mem_total_kb": mem.get("MemTotal"), "mem_avail_kb": mem.get("MemAvailable"),
            "temp_c": max(temps) if temps else None,
        }

    # ================= 접속 기기 =================
    def clients(self):
        leases = {}
        for line in (_read(LEASES, "") or "").splitlines():
            parts = line.split()
            if len(parts) >= 4:
                leases[parts[1].lower()] = {"ip": parts[2], "name": None if parts[3] == "*" else parts[3]}
        # WiFi 접속 기기: iw station dump (hostapd ctrl_interface 없이도 동작)
        wifi, cur = {}, None
        try:
            out = run(["iw", "dev", WLAN_IF, "station", "dump"], timeout=5, check=False)
        except Exception:  # noqa: BLE001
            out = ""
        for line in out.splitlines():
            m = re.match(r"station ([0-9a-f:]{17})", line.strip().lower())
            if m:
                cur = m.group(1)
                wifi[cur] = {}
            elif cur and ":" in line:
                k, _, v = line.strip().partition(":")
                if k == "signal":
                    wifi[cur]["signal"] = v.split()[0] if v.split() else None
                elif k == "connected time":
                    wifi[cur]["connected_time"] = v.split()[0] if v.split() else None
        # 브리지 포트로 접속 경로 판별 (wlan0 = WiFi, usb* = USB)
        port = {}
        try:
            for f in json.loads(run(["bridge", "-j", "fdb", "show", "br", LAN_BRIDGE], timeout=5) or "[]"):
                if f.get("mac") and "permanent" not in (f.get("state") or "") and f.get("ifname"):
                    port[f["mac"].lower()] = f["ifname"]
        except Exception:  # noqa: BLE001
            pass
        neigh = {}
        try:
            for n in json.loads(run(["ip", "-j", "neigh", "show", "dev", LAN_BRIDGE], timeout=5) or "[]"):
                if n.get("lladdr") and ":" not in n.get("dst", ":"):     # IPv4만
                    neigh[n["lladdr"].lower()] = {"ip": n["dst"], "state": (n.get("state") or [""])[0]}
        except Exception:  # noqa: BLE001
            pass

        blocked = {b["mac"] for b in self.net.blocked()}
        result = []
        for mac in sorted(set(leases) | set(wifi) | {m for m, n in neigh.items() if n["state"] in ("REACHABLE", "DELAY", "PROBE")}):
            l, n, w = leases.get(mac, {}), neigh.get(mac, {}), wifi.get(mac)
            online = w is not None or n.get("state") in ("REACHABLE", "DELAY", "PROBE", "STALE")
            if not online:
                continue
            dev = port.get(mac, "")
            via = "WiFi" if (w is not None or dev == WLAN_IF) else "USB" if dev.startswith("usb") else "-"
            result.append({
                "mac": mac, "ip": l.get("ip") or n.get("ip"), "name": l.get("name"),
                "via": via, "blocked": mac in blocked,
                "connected_sec": int(w["connected_time"]) if w and w.get("connected_time", "").isdigit() else None,
                "signal_dbm": _num(w.get("signal")) if w else None,
            })
        online = {c["mac"] for c in result}
        offline_blocked = [b for b in self.net.blocked() if b["mac"] not in online]
        return {"clients": result, "blocked_offline": offline_blocked}

    # ================= WiFi =================
    WIFI_KEYS = ("ssid", "wpa_passphrase", "channel", "ignore_broadcast_ssid", "max_num_sta", "ap_isolate", "wpa_key_mgmt")

    def _hostapd_lines(self):
        with open(HOSTAPD_CONF) as f:
            return f.read().splitlines()

    def wifi_get(self):
        cfg = {}
        for line in self._hostapd_lines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
        return {
            "ssid": cfg.get("ssid", ""),
            "has_password": bool(cfg.get("wpa_passphrase")),
            "security": "WPA2/WPA3" if "SAE" in cfg.get("wpa_key_mgmt", "") else "WPA2",
            "channel": int(cfg.get("channel", "6") or 6),
            "hidden": cfg.get("ignore_broadcast_ssid", "0") != "0",
            # hostapd 기본값 2007 = 제한 없음 (저장 시에도 0 -> 2007로 씀)
            "max_clients": (lambda n: None if n == 0 or n >= 2007 else n)(int(cfg.get("max_num_sta", "0") or 0)),
            "isolate": cfg.get("ap_isolate", "0") == "1",
            "running": run(["systemctl", "is-active", "hostapd"], check=False).strip() == "active",
        }

    def wifi_set(self, d):
        changes = {}
        if "ssid" in d:
            ssid = str(d["ssid"])
            if not (1 <= len(ssid.encode()) <= 32) or any(c in ssid for c in "\n\r"):
                raise ValueError("WiFi 이름은 1~32바이트, 줄바꿈 불가")
            changes["ssid"] = ssid
        if d.get("password"):
            pw = str(d["password"])
            if not (8 <= len(pw) <= 63) or not all(32 <= ord(c) < 127 for c in pw):
                raise ValueError("WiFi 비밀번호는 영문/숫자/기호 8~63자")
            changes["wpa_passphrase"] = pw
        if "channel" in d:
            ch = int(d["channel"])
            if not 1 <= ch <= 13:
                raise ValueError("채널은 1~13")
            changes["channel"] = str(ch)
        if "hidden" in d:
            changes["ignore_broadcast_ssid"] = "1" if d["hidden"] else "0"
        if "isolate" in d:
            changes["ap_isolate"] = "1" if d["isolate"] else "0"
        if "max_clients" in d:
            mc = int(d["max_clients"] or 0)
            if not 0 <= mc <= 32:
                raise ValueError("최대 접속 수는 0(제한없음)~32")
            changes["max_num_sta"] = str(mc or 2007)
        if "security" in d:
            if d["security"] not in ("WPA2", "WPA2/WPA3"):
                raise ValueError("보안 방식 오류")
            changes["wpa_key_mgmt"] = "WPA-PSK SAE" if d["security"] == "WPA2/WPA3" else "WPA-PSK"
            changes["ieee80211w"] = "1" if d["security"] == "WPA2/WPA3" else "0"
        if not changes:
            raise ValueError("변경 사항이 없습니다")

        with _wifi_lock:
            lines = self._hostapd_lines()
            backup = HOSTAPD_CONF + ".bak"
            if not os.path.exists(backup):
                with open(backup, "w") as f:
                    f.write("\n".join(lines) + "\n")
            seen, out = set(), []
            for line in lines:
                k = line.partition("=")[0].strip()
                if "=" in line and not line.lstrip().startswith("#") and k in changes:
                    out.append(f"{k}={changes[k]}")
                    seen.add(k)
                else:
                    out.append(line)
            out += [f"{k}={v}" for k, v in changes.items() if k not in seen]
            tmp = HOSTAPD_CONF + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(out) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, HOSTAPD_CONF)
            run(["systemctl", "restart", "hostapd"], timeout=30)
        return {"ok": True, "applied": sorted(k for k in changes if k != "wpa_passphrase") + (["password"] if "wpa_passphrase" in changes else [])}

    # ================= 인터넷(WAN) / APN =================
    def _nm_get(self):
        out = run(["nmcli", "-t", "-f",
                   "connection.autoconnect,gsm.apn,gsm.username,gsm.auto-config,gsm.home-only,ipv4.method,ipv6.method",
                   "c", "show", LTE_CONN])
        cfg = {}
        for line in out.splitlines():
            k, _, v = line.partition(":")
            cfg[k] = v
        return cfg

    def wan_get(self):
        cfg = self._nm_get()
        g = mmcli_json("-m", "any").get("modem", {}).get("generic", {})
        bearer = self._active_bearer(g.get("bearers") or [])
        v4, v6 = cfg.get("ipv4.method") != "disabled", cfg.get("ipv6.method") not in ("disabled", "ignore")
        return {
            "data_enabled": cfg.get("connection.autoconnect") == "yes",
            "roaming_allowed": cfg.get("gsm.home-only") != "yes",
            "apn_mode": "auto" if cfg.get("gsm.auto-config") == "yes" else "manual",
            "apn": cfg.get("gsm.apn") or "", "username": cfg.get("gsm.username") or "",
            "ip_type": "ipv4v6" if v4 and v6 else ("ipv6" if v6 else "ipv4"),
            "bearer": bearer,
        }

    def wan_set_data(self, d):
        changes = []
        if "roaming_allowed" in d:
            changes += ["gsm.home-only", "no" if d["roaming_allowed"] else "yes"]
        if "data_enabled" in d:
            changes += ["connection.autoconnect", "yes" if d["data_enabled"] else "no"]
        if not changes:
            raise ValueError("변경 사항이 없습니다")
        run(["nmcli", "c", "modify", LTE_CONN, *changes])
        if "data_enabled" in d:
            if d["data_enabled"]:
                run(["nmcli", "c", "up", LTE_CONN], timeout=90)
            else:
                run(["nmcli", "c", "down", LTE_CONN], timeout=30, check=False)
        return {"ok": True}

    def wan_set_apn(self, d):
        mode = d.get("apn_mode")
        if mode not in ("auto", "manual"):
            raise ValueError("APN 모드 오류")
        ip_type = d.get("ip_type", "ipv4v6")
        if ip_type not in ("ipv4", "ipv6", "ipv4v6"):
            raise ValueError("IP 타입 오류")
        apn, user, pw = str(d.get("apn", "")), str(d.get("username", "")), str(d.get("password", ""))
        if mode == "manual" and not re.fullmatch(r"[A-Za-z0-9.\-_]{1,63}", apn):
            raise ValueError("APN은 영문/숫자/.-_ 1~63자")
        for x in (user, pw):
            if len(x) > 64 or any(c in x for c in "\n\r"):
                raise ValueError("사용자명/비밀번호 형식 오류")

        with _apn_lock:
            prev = self._nm_get()
            new = ["gsm.auto-config", "yes" if mode == "auto" else "no",
                   "gsm.apn", apn if mode == "manual" else "",
                   "gsm.username", user if mode == "manual" else ""]
            if mode == "manual" and pw:
                new += ["gsm.password", pw, "gsm.password-flags", "0"]
            new += ["ipv4.method", "auto" if ip_type != "ipv6" else "disabled",
                    "ipv6.method", "auto" if ip_type != "ipv4" else "disabled"]
            run(["nmcli", "c", "modify", LTE_CONN, *new])
            ok = self._reconnect_and_check()
            if ok:
                return {"ok": True, "rolled_back": False, "bearer": ok}
            # 실패 → 이전 설정으로 롤백 (비밀번호는 NM에 그대로 남아 있음)
            run(["nmcli", "c", "modify", LTE_CONN,
                 "gsm.auto-config", prev.get("gsm.auto-config", "yes"),
                 "gsm.apn", prev.get("gsm.apn", ""), "gsm.username", prev.get("gsm.username", ""),
                 "ipv4.method", prev.get("ipv4.method", "auto"), "ipv6.method", prev.get("ipv6.method", "auto")])
            again = self._reconnect_and_check()
            return {"ok": False, "rolled_back": True, "restored_connected": bool(again),
                    "error": "새 APN으로 연결하지 못해 이전 설정으로 되돌렸습니다"}

    def _reconnect_and_check(self):
        run(["nmcli", "c", "down", LTE_CONN], timeout=30, check=False)
        r = subprocess.run(["nmcli", "c", "up", LTE_CONN], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None
        for _ in range(10):
            g = mmcli_json("-m", "any").get("modem", {}).get("generic", {})
            b = self._active_bearer(g.get("bearers") or [])
            if b and b["ipv4"]:
                return b
            time.sleep(2)
        return None

    # ================= 기기 =================
    def device(self):
        m = mmcli_json("-m", "any").get("modem", {})
        g, t = m.get("generic", {}), m.get("3gpp", {})
        sim = {}
        if _v(g.get("sim")):
            sim = mmcli_json("-i", g["sim"]).get("sim", {}).get("properties", {})
        osr = {}
        for line in (_read("/etc/os-release", "") or "").splitlines():
            k, _, v = line.partition("=")
            osr[k] = v.strip('"')
        return {
            "model": (_read("/proc/device-tree/model", "") or "").rstrip("\x00"),
            "hostname": _read("/etc/hostname"),
            "os": osr.get("PRETTY_NAME"),
            "kernel": os.uname().release,
            "imei": _v(t.get("imei")),
            "modem_firmware": ((_v(g.get("revision")) or "").split() or [None])[0],
            "iccid": _v(sim.get("iccid")), "imsi": _v(sim.get("imsi")),
            "sim_operator": _v(sim.get("operator-name")) or _v(t.get("operator-name")),
            "sim_operator_code": _v(sim.get("operator-code")),
            "lan_ip": "192.168.100.1",
        }

    def reboot(self):
        threading.Timer(1.5, lambda: subprocess.run(["systemctl", "reboot"])).start()
        return {"ok": True}

    # ================= 문자 (SMS/MMS) =================
    def messages_list(self, q):
        return self.msgs.list(q.get("limit", 50), q.get("before"))

    def messages_send(self, d):
        return self.msgs.send(d.get("number", ""), d.get("text", ""))

    def messages_delete(self, d):
        if d.get("older_than_days") is not None:
            return self.msgs.delete(older_than_days=d["older_than_days"])
        ids = d.get("ids") or []
        if not isinstance(ids, list):
            raise ValueError("삭제할 문자를 고르세요")
        return self.msgs.delete(ids)

    def messages_read(self, d):
        return self.msgs.mark_read(d.get("ids"))

    def messages_retry(self, d):
        return self.msgs.retry(d.get("id"))

    def messages_media(self, msg_id, part):
        return self.msgs.media(msg_id, part)

    # ================= 데이터 사용량 / 기기 차단 =================
    def _wan_up(self):
        try:
            out = json.loads(run(["ip", "-j", "-4", "addr", "show", "dev", "wwan0"], timeout=5, check=False) or "[]")
            return any(i.get("addr_info") for i in out)
        except Exception:  # noqa: BLE001
            return False

    def _usage_summary(self):
        u = self.net.usage()
        return {k: u[k] for k in ("today", "period", "cap_bytes", "percent", "warn_pct", "blocked_now")}

    def usage_get(self):
        return self.net.usage()

    def usage_set(self, d):
        return self.net.set_usage(d)

    def usage_reset(self):
        return self.net.reset_usage()

    def client_block(self, d, requester_ip):
        return self.net.block(d.get("mac"), d.get("name"), netctl.mac_of_ip(requester_ip))

    def client_unblock(self, d):
        return self.net.unblock(d.get("mac"))

    # ================= 시스템 도구 =================
    def leds_set(self, d):
        return self.sys.set_leds(d.get("mode"))

    def system_get(self):
        return {"leds": self.settings.get("leds", "on"), "time": self.sys.time_info(), **self.sys.services()}

    def timezone_set(self, d):
        return self.sys.set_timezone(d.get("timezone"))

    def logs(self, q):
        return self.sys.logs(q.get("unit", "admin"), q.get("lines", 200))

    def service_restart(self, d):
        return self.sys.restart(d.get("name"))

    def dns_get(self):
        return self.sys.dns_get()

    def dns_set(self, d):
        carrier = []
        if d.get("preset") == "carrier":
            g = mmcli_json("-m", "any").get("modem", {}).get("generic", {})
            b = self._active_bearer(g.get("bearers") or [])
            carrier = (b or {}).get("dns") or []
        return self.sys.dns_set(d.get("preset"), carrier)

    def diag(self, d):
        kind = d.get("kind")
        if kind == "ping":
            return self.sys.ping(d.get("host"))
        if kind == "lookup":
            return self.sys.lookup(d.get("host"))
        if kind == "traceroute":
            return self.sys.traceroute(d.get("host"))
        if kind == "speedtest":
            return self.sys.speedtest(d.get("mb", 10))
        raise ValueError("없는 진단 종류")
