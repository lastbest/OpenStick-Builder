"""Data usage accounting and client blocking (nftables table `inet osa`).

The admin page owns one nft table so its rules never touch /etc/nftables.conf:
- named counters wan_rx / wan_tx on wwan0 (the BAM-DMUX netdev keeps its sysfs
  byte counters at 0, so nft is the reliable source),
- set `blocked` of client MACs whose forwarded traffic is dropped,
- set `capblock` of uplink interfaces to drop when the monthly cap is exceeded.
nftables.service flushes the ruleset when (re)started, so the table is re-created
from saved settings whenever it is missing.
"""
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import date, datetime, timedelta

WAN_IF = "wwan0"
LAN_BRIDGE = "br0"
TICK_SEC = 60
MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")

TABLE = """
table inet osa {
  counter wan_rx { }
  counter wan_tx { }
  set blocked { type ether_addr; }
  set capblock { type ifname; }
  chain acct_in {
    type filter hook prerouting priority -300;
    iifname "%(wan)s" counter name "wan_rx";
  }
  chain acct_out {
    type filter hook postrouting priority 300;
    oifname "%(wan)s" counter name "wan_tx";
  }
  chain forward_f {
    type filter hook forward priority -5; policy accept;
    ether saddr @blocked counter drop;
    iifname "%(lan)s" oifname @capblock counter drop;
  }
}
"""


def _nft(*args, stdin=None, check=True):
    p = subprocess.run(["nft", *args], input=stdin, capture_output=True, text=True, timeout=15)
    if check and p.returncode != 0:
        raise RuntimeError(f"nft: {p.stderr.strip()[:300]}")
    return p.stdout


class Settings:
    """Small JSON settings file shared by the admin modules."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def update(self, **kv):
        with self.lock:
            self.data.update(kv)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)


def period_start(today, reset_day):
    """First day of the current billing cycle (reset_day 1..28)."""
    if today.day >= reset_day:
        return today.replace(day=reset_day)
    prev = today.replace(day=1) - timedelta(days=1)
    return prev.replace(day=reset_day)


class NetCtl:
    def __init__(self, settings, data_dir="/var/lib/openstick-admin", start=True):
        self.s = settings
        self.db = sqlite3.connect(os.path.join(data_dir, "usage.db"), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, rx INTEGER NOT NULL DEFAULT 0,"
                        " tx INTEGER NOT NULL DEFAULT 0)")
        self.db.commit()
        self.lock = threading.Lock()
        self.last = None                    # last counter readings (rx, tx)
        self.error = None
        if start:
            threading.Thread(target=self._loop, name="netctl", daemon=True).start()

    # ---------------- nft table
    def ensure(self):
        if subprocess.run(["nft", "list", "table", "inet", "osa"], capture_output=True).returncode == 0:
            return False
        _nft("-f", "-", stdin=TABLE % {"wan": WAN_IF, "lan": LAN_BRIDGE})
        macs = [b["mac"] for b in self.s.get("blocked", [])]
        if macs:
            _nft("add", "element", "inet", "osa", "blocked", "{ " + ", ".join(macs) + " }")
        self.last = None                    # counters start from zero again
        self._apply_cap()
        return True

    def _counters(self):
        out = json.loads(_nft("-j", "list", "counters", "table", "inet", "osa"))
        c = {o["counter"]["name"]: o["counter"]["bytes"] for o in out.get("nftables", []) if "counter" in o}
        return c.get("wan_rx", 0), c.get("wan_tx", 0)

    # ---------------- accounting
    def _loop(self):
        while True:
            try:
                self.tick()
                self.error = None
            except Exception as e:  # noqa: BLE001
                self.error = str(e)[:300]
            time.sleep(TICK_SEC)

    def tick(self):
        self.ensure()
        rx, tx = self._counters()
        if self.last is not None:
            drx = rx - self.last[0] if rx >= self.last[0] else rx
            dtx = tx - self.last[1] if tx >= self.last[1] else tx
            if drx or dtx:
                day = date.today().isoformat()
                with self.lock:
                    self.db.execute("INSERT INTO usage(day, rx, tx) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE"
                                    " SET rx = rx + excluded.rx, tx = tx + excluded.tx", (day, drx, dtx))
                    self.db.commit()
        self.last = (rx, tx)
        self._apply_cap()

    def usage(self):
        reset_day = int(self.s.get("usage_reset_day", 1))
        cap_gb = float(self.s.get("usage_cap_gb", 0) or 0)
        today = date.today()
        start = period_start(today, reset_day)
        with self.lock:
            days = self.db.execute("SELECT day, rx, tx FROM usage WHERE day >= ? ORDER BY day",
                                   ((today - timedelta(days=62)).isoformat(),)).fetchall()
        period = [d for d in days if d[0] >= start.isoformat()]
        used = sum(d[1] + d[2] for d in period)
        today_row = next((d for d in days if d[0] == today.isoformat()), (None, 0, 0))
        cap = int(cap_gb * 1024 ** 3)
        return {
            "today": {"rx": today_row[1], "tx": today_row[2]},
            "period": {"start": start.isoformat(), "rx": sum(d[1] for d in period),
                       "tx": sum(d[2] for d in period), "used": used},
            "days": [{"day": d[0], "rx": d[1], "tx": d[2]} for d in days if d[0] >= (today - timedelta(days=30)).isoformat()],
            "cap_bytes": cap, "percent": round(used * 100 / cap, 1) if cap else None,
            "warn_pct": int(self.s.get("usage_warn_pct", 80)),
            "block_over_cap": bool(self.s.get("usage_block", False)),
            "blocked_now": self._cap_exceeded(used, cap),
            "reset_day": reset_day, "error": self.error,
        }

    def set_usage(self, d):
        kv = {}
        if "cap_gb" in d:
            v = float(d["cap_gb"] or 0)
            if not 0 <= v <= 10000:
                raise ValueError("월 한도는 0(없음)~10000 GB")
            kv["usage_cap_gb"] = v
        if "reset_day" in d:
            v = int(d["reset_day"])
            if not 1 <= v <= 28:
                raise ValueError("초기화일은 1~28일")
            kv["usage_reset_day"] = v
        if "warn_pct" in d:
            v = int(d["warn_pct"])
            if not 10 <= v <= 100:
                raise ValueError("경고 기준은 10~100%")
            kv["usage_warn_pct"] = v
        if "block" in d:
            kv["usage_block"] = bool(d["block"])
        self.s.update(**kv)
        self._apply_cap()
        return {"ok": True}

    def reset_usage(self):
        with self.lock:
            self.db.execute("DELETE FROM usage")
            self.db.commit()
        return {"ok": True}

    def _cap_exceeded(self, used, cap):
        return bool(cap and used >= cap and self.s.get("usage_block", False))

    def _apply_cap(self):
        u = self.usage_used()
        want = self._cap_exceeded(*u)
        have = WAN_IF in _nft("list", "set", "inet", "osa", "capblock", check=False)
        if want and not have:
            _nft("add", "element", "inet", "osa", "capblock", "{ " + WAN_IF + " }")
            print(f"netctl: monthly cap reached, blocking client internet", flush=True)
        elif have and not want:
            _nft("delete", "element", "inet", "osa", "capblock", "{ " + WAN_IF + " }", check=False)

    def usage_used(self):
        start = period_start(date.today(), int(self.s.get("usage_reset_day", 1)))
        with self.lock:
            used = self.db.execute("SELECT COALESCE(SUM(rx + tx), 0) FROM usage WHERE day >= ?",
                                   (start.isoformat(),)).fetchone()[0]
        return used, int(float(self.s.get("usage_cap_gb", 0) or 0) * 1024 ** 3)

    # ---------------- client blocking
    def blocked(self):
        return self.s.get("blocked", [])

    def block(self, mac, name, requester_mac):
        mac = (mac or "").lower()
        if not MAC_RE.fullmatch(mac):
            raise ValueError("MAC 주소 형식 오류")
        if requester_mac and mac == requester_mac.lower():
            raise ValueError("지금 접속 중인 이 기기는 차단할 수 없습니다")
        lst = [b for b in self.blocked() if b["mac"] != mac]
        lst.append({"mac": mac, "name": (name or "")[:64] or None, "since": int(time.time())})
        self.s.update(blocked=lst)
        self.ensure()
        _nft("add", "element", "inet", "osa", "blocked", "{ " + mac + " }")
        return {"ok": True}

    def unblock(self, mac):
        mac = (mac or "").lower()
        if not MAC_RE.fullmatch(mac):
            raise ValueError("MAC 주소 형식 오류")
        self.s.update(blocked=[b for b in self.blocked() if b["mac"] != mac])
        _nft("delete", "element", "inet", "osa", "blocked", "{ " + mac + " }", check=False)
        return {"ok": True}


def mac_of_ip(ip):
    """MAC of a LAN client by IP (for the self-lockout check)."""
    try:
        for n in json.loads(subprocess.run(["ip", "-j", "neigh", "show", "dev", LAN_BRIDGE],
                                           capture_output=True, text=True, timeout=5).stdout or "[]"):
            if n.get("dst") == ip and n.get("lladdr"):
                return n["lladdr"].lower()
    except Exception:  # noqa: BLE001
        pass
    return None
