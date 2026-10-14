"""LEDs, timezone, logs, services, DNS upstream and network diagnostics."""
import os
import random
import re
import socket
import struct
import subprocess
import threading
import time

LEDS = "/sys/class/leds"
LED_WAN, LED_WLAN, LED_POWER = "blue:wan", "green:wlan", "red:power"
LOG_UNITS = {
    "admin": ["-u", "openstick-admin"], "modem": ["-u", "ModemManager"], "network": ["-u", "NetworkManager"],
    "wifi": ["-u", "hostapd"], "dhcp": ["-u", "dnsmasq"], "dns": ["-u", "dnsproxy"],
    "firewall": ["-u", "nftables"], "kernel": ["-k"], "all": [],
}
SERVICES = {   # name -> (unit, can restart, warning)
    "hostapd": ("hostapd", True, "WiFi가 잠깐 끊깁니다"),
    "dnsmasq": ("dnsmasq", True, "DHCP/DNS가 잠깐 멈춥니다"),
    "dnsproxy": ("dnsproxy", True, "DNS가 잠깐 멈춥니다"),
    "ModemManager": ("ModemManager", True, "LTE가 끊겼다가 다시 연결됩니다(수십 초)"),
    "NetworkManager": ("NetworkManager", True, "LTE·USB 네트워크가 잠깐 끊깁니다"),
    "nftables": ("nftables", True, "방화벽/NAT 규칙을 다시 읽습니다"),
    "openstick-admin": ("openstick-admin", False, ""),
    "msm8916-usb-gadget": ("msm8916-usb-gadget", False, ""),
}
DNSPROXY_DROPIN = "/etc/systemd/system/dnsproxy.service.d/osa-upstream.conf"
DNS_BASE = ("/usr/local/bin/dnsproxy --listen 0.0.0.0 --listen :: --port 5353 "
            "--bootstrap 8.8.8.8 --bootstrap 2606:4700:4700::1111 --fallback 9.9.9.9 --fallback 2620:fe::9 "
            "--ratelimit 500 --cache --cache-optimistic --cache-size 2097152 --output /dev/null")
DNS_PRESETS = {
    "adguard": ("AdGuard (광고·추적 차단, DoH/DoQ 암호화)",
                "--upstream [/www.googleadservices.com/]8.8.8.8 --upstream [/ad.doubleclick.net/]8.8.8.8 "
                "--upstream https://dns.adguard.com/dns-query --upstream quic://dns.adguard.com"),
    "cloudflare": ("Cloudflare (차단 없음, DoH 암호화)", "--upstream https://cloudflare-dns.com/dns-query"),
    "google": ("Google (차단 없음, DoH 암호화)", "--upstream https://dns.google/dns-query"),
    "carrier": ("통신사 DNS (차단·암호화 없음, 가장 가까움)", None),
}
HOST_RE = re.compile(r"(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)*[A-Za-z0-9-]{1,63}\.?|[0-9.]{7,15}|[0-9a-fA-F:]{2,39}")


def _run(args, timeout=30, check=True):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args[0]}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def _write(path, value):
    try:
        with open(path, "w") as f:
            f.write(value)
    except OSError:
        pass


def _check_host(h):
    h = str(h or "").strip()
    if not HOST_RE.fullmatch(h) or h.startswith("-"):
        raise ValueError("호스트 이름/주소 형식 오류")
    return h


class SysTools:
    def __init__(self, settings, wan_connected=lambda: False, start=True):
        self.s = settings
        self.wan_connected = wan_connected
        if start:
            self.apply_leds()
            threading.Thread(target=self._led_loop, name="leds", daemon=True).start()

    # ---------------- LEDs
    def apply_leds(self):
        on = self.s.get("leds", "on") == "on"
        if not os.path.isdir(LEDS):
            return
        _write(f"{LEDS}/{LED_POWER}/trigger", "default-on" if on else "none")
        _write(f"{LEDS}/{LED_WLAN}/trigger", "phy0radio" if on else "none")
        _write(f"{LEDS}/{LED_WAN}/trigger", "none")
        for led in (LED_POWER, LED_WLAN, LED_WAN):
            if not on:
                _write(f"{LEDS}/{led}/brightness", "0")
        self._update_wan_led()

    def _update_wan_led(self):
        on = self.s.get("leds", "on") == "on" and self.wan_connected()
        _write(f"{LEDS}/{LED_WAN}/brightness", "1" if on else "0")

    def _led_loop(self):
        while True:
            try:
                self._update_wan_led()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(15)

    def set_leds(self, mode):
        if mode not in ("on", "off"):
            raise ValueError("LED 설정 오류")
        self.s.update(leds=mode)
        self.apply_leds()
        return {"ok": True}

    # ---------------- time
    def time_info(self):
        out = _run(["timedatectl", "show"], check=False)
        kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return {"timezone": kv.get("Timezone"), "ntp_synced": kv.get("NTPSynchronized") == "yes",
                "now": int(time.time()), "local": time.strftime("%Y-%m-%d %H:%M:%S %Z")}

    def set_timezone(self, tz):
        tz = str(tz or "")
        if not re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}", tz):
            raise ValueError("시간대 형식 오류")
        if tz not in _run(["timedatectl", "list-timezones"]).split():
            raise ValueError("없는 시간대")
        _run(["timedatectl", "set-timezone", tz])
        os.environ.pop("TZ", None)
        time.tzset()                        # this process follows the new zone too
        return self.time_info()

    # ---------------- logs / services
    def logs(self, unit="admin", lines=200):
        if unit not in LOG_UNITS:
            raise ValueError("없는 로그 종류")
        lines = max(20, min(int(lines), 1000))
        out = _run(["journalctl", "--no-pager", "-o", "short-iso", "-n", str(lines), *LOG_UNITS[unit]],
                   timeout=20, check=False)
        return {"unit": unit, "text": out[-200_000:]}

    def services(self):
        res = []
        for name, (unit, restart, warn) in SERVICES.items():
            state = _run(["systemctl", "is-active", unit], check=False).strip()
            res.append({"name": name, "state": state, "can_restart": restart, "warning": warn})
        return {"services": res}

    def restart(self, name):
        if name not in SERVICES or not SERVICES[name][1]:
            raise ValueError("재시작할 수 없는 서비스")
        _run(["systemctl", "restart", SERVICES[name][0]], timeout=90)
        return {"ok": True, "state": _run(["systemctl", "is-active", SERVICES[name][0]], check=False).strip()}

    # ---------------- DNS upstream (dnsproxy behind dnsmasq)
    def dns_get(self):
        cur = self.s.get("dns_upstream", "adguard")
        return {"current": cur, "presets": [{"id": k, "label": v[0]} for k, v in DNS_PRESETS.items()],
                "state": _run(["systemctl", "is-active", "dnsproxy"], check=False).strip()}

    def dns_set(self, preset, carrier_dns=()):
        if preset not in DNS_PRESETS:
            raise ValueError("없는 DNS 설정")
        if preset == "carrier":
            ips = [ip for ip in carrier_dns if re.fullmatch(r"[0-9.]{7,15}", ip or "")]
            if not ips:
                raise ValueError("통신사 DNS 주소를 알 수 없습니다 (LTE 연결 확인)")
            ups = " ".join(f"--upstream {ip}:53" for ip in ips)
        else:
            ups = DNS_PRESETS[preset][1]
        old = open(DNSPROXY_DROPIN).read() if os.path.exists(DNSPROXY_DROPIN) else None

        def restore():
            if old is None:
                if os.path.exists(DNSPROXY_DROPIN):
                    os.remove(DNSPROXY_DROPIN)
            else:
                with open(DNSPROXY_DROPIN, "w") as f:
                    f.write(old)
            _run(["systemctl", "daemon-reload"], timeout=180, check=False)
            _run(["systemctl", "restart", "dnsproxy"], timeout=90, check=False)

        os.makedirs(os.path.dirname(DNSPROXY_DROPIN), exist_ok=True)
        with open(DNSPROXY_DROPIN, "w") as f:
            f.write(f"# written by openstick-admin\n[Service]\nExecStart=\nExecStart={DNS_BASE} {ups}\n")
        try:
            # daemon-reload can take tens of seconds on this SoC
            _run(["systemctl", "daemon-reload"], timeout=180)
            _run(["systemctl", "restart", "dnsproxy"], timeout=90, check=False)
            ok = False
            for _ in range(10):             # dnsproxy needs a moment to bootstrap
                time.sleep(1)
                if _dns_query("example.com", server=("127.0.0.1", 5353)):
                    ok = True
                    break
        except Exception:
            restore()
            raise
        if not ok:
            restore()
            raise RuntimeError("새 DNS로 이름 조회가 안 되어 이전 설정으로 되돌렸습니다")
        self.s.update(dns_upstream=preset)
        return {"ok": True, "current": preset}

    # ---------------- diagnostics
    def ping(self, host):
        host = _check_host(host)
        p = subprocess.run(["ping", "-c", "4", "-W", "2", "--", host], capture_output=True, text=True, timeout=20)
        return {"ok": p.returncode == 0, "text": (p.stdout or p.stderr)[-4000:]}

    def lookup(self, name):
        name = _check_host(name)
        res = {}
        for qtype, label in ((1, "A"), (28, "AAAA")):
            res[label] = _dns_query(name, qtype=qtype, server=("127.0.0.1", 53))
        return {"name": name, "server": "dnsmasq (192.168.100.1)", **res}

    def traceroute(self, host, max_hops=20):
        host = _check_host(host)
        hops = []
        for ttl in range(1, max_hops + 1):
            p = subprocess.run(["ping", "-n", "-c", "1", "-W", "1", "-t", str(ttl), "--", host],
                               capture_output=True, text=True, timeout=5)
            m = re.search(r"[Ff]rom ([0-9a-fA-F.:]+)", p.stdout)
            t = re.search(r"time=([\d.]+)", p.stdout)
            if p.returncode == 0:
                ip = re.search(r"bytes from ([0-9a-fA-F.:]+)", p.stdout)
                hops.append({"ttl": ttl, "ip": ip.group(1).rstrip(":") if ip else host,
                             "ms": float(t.group(1)) if t else None})
                break
            hops.append({"ttl": ttl, "ip": m.group(1) if m else None, "ms": None})
        return {"host": host, "hops": hops}

    def speedtest(self, mb, wan_if="wwan0"):
        mb = int(mb)
        if mb not in (5, 10, 25):
            raise ValueError("5/10/25 MB 중 선택")
        url = f"https://speed.cloudflare.com/__down?bytes={mb * 1_000_000}"
        out = _run(["curl", "-s", "-m", "60", "--interface", wan_if, "-o", "/dev/null",
                    "-w", "%{http_code} %{speed_download} %{time_total} %{size_download}", url], timeout=70)
        code, speed, total, size = out.split()
        if code != "200":
            raise RuntimeError(f"측정 서버 응답 {code}")
        return {"mbps": round(float(speed) * 8 / 1e6, 1), "seconds": round(float(total), 1),
                "bytes": int(float(size))}


def _dns_query(name, qtype=1, server=("127.0.0.1", 53), timeout=3):
    """Minimal DNS client (A=1, AAAA=28). Returns list of addresses ([] on failure)."""
    tid = random.randint(0, 0xFFFF)
    q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode("idna") for p in name.rstrip(".").split(".")) + b"\x00"
    q += struct.pack(">HH", qtype, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(q, server)
            r, _ = s.recvfrom(4096)
    except OSError:
        return []
    if len(r) < 12 or struct.unpack(">H", r[:2])[0] != tid or r[3] & 0x0F:
        return []
    an = struct.unpack(">H", r[6:8])[0]
    p = 12
    while r[p]:                             # skip question name
        p += r[p] + 1
    p += 5
    out = []
    for _ in range(an):
        if r[p] & 0xC0 == 0xC0:
            p += 2
        else:
            while r[p]:
                p += r[p] + 1
            p += 1
        t, _, _, ln = struct.unpack(">HHIH", r[p:p + 10])
        p += 10
        if t == 1 and ln == 4:
            out.append(socket.inet_ntop(socket.AF_INET, r[p:p + 4]))
        elif t == 28 and ln == 16:
            out.append(socket.inet_ntop(socket.AF_INET6, r[p:p + 16]))
        p += ln
    return out
