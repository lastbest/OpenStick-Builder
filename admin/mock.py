"""모의 백엔드: 맥/PC에서 기기 없이 화면 개발용. 값은 실제 기기 측정치를 참고한 더미(IMEI 등 식별정보는 가짜)."""
import os
import random
import struct
import tempfile
import time
import zlib

import sms

_start = time.time()


class Backend:
    def __init__(self):
        self.wifi = {"ssid": "4G-UFI-XX", "password": "1234567890", "channel": 6, "hidden": False,
                     "max_clients": None, "isolate": False, "security": "WPA2"}
        self.wan = {"data_enabled": True, "roaming_allowed": True, "apn_mode": "auto",
                    "apn": "", "username": "", "ip_type": "ipv4v6"}
        self.rx, self.tx = 512_000_000, 48_000_000
        self.msgs = _MockMessages()

    def _bearer(self):
        if not self.wan["data_enabled"]:
            return None
        apn = self.wan["apn"] if self.wan["apn_mode"] == "manual" else "alwayson.ktfwing.com"
        return {"connected": True, "interface": "wwan0", "apn": apn, "ip_type": self.wan["ip_type"],
                "roaming": "allowed", "ipv4": "10.0.0.2", "gateway": "10.0.0.1",
                "dns": ["211.219.86.1", "39.16.0.1"]}

    def status(self):
        if self.wan["data_enabled"]:
            self.rx += random.randint(50_000, 900_000)
            self.tx += random.randint(5_000, 120_000)
        b = self._bearer()
        return {
            "modem": {"state": "connected" if b else "registered", "operator": "KT",
                      "registration": "home", "access_tech": ["lte"], "quality": 81.0,
                      "rsrp": -88.0 + random.uniform(-3, 3), "rsrq": -10.0, "snr": 12.4, "rssi": -61.0,
                      "sim": "정상"},
            "wan": {"connected": bool(b), "ipv4": b and b["ipv4"], "apn": b and b["apn"],
                    "rx_bytes": self.rx, "tx_bytes": self.tx, "connected_sec": 3510,
                    "lan_down_bytes": self.rx, "lan_up_bytes": self.tx, "ts": time.time()},
            "clients": len(self.clients()["clients"]),
            "unread": self.msgs.unread(),
            "storage": self.msgs.storage(),
            "system": {"uptime": int(time.time() - _start) + 3600, "load": ["0.12", "0.20", "0.18"],
                       "mem_total_kb": 382964, "mem_avail_kb": 256860, "temp_c": 41.5},
        }

    def clients(self):
        return {"clients": [
            {"mac": "02:00:00:00:00:01", "ip": "192.168.100.39", "name": "iPhone", "via": "WiFi",
             "connected_sec": 1843, "signal_dbm": -48.0},
            {"mac": "02:00:00:00:00:02", "ip": "192.168.100.71", "name": "MacBook-Air", "via": "USB",
             "connected_sec": None, "signal_dbm": None},
        ]}

    def wifi_get(self):
        w = dict(self.wifi)
        w["has_password"] = bool(w.pop("password"))
        w["running"] = True
        return w

    def wifi_set(self, d):
        if "ssid" in d and not 1 <= len(str(d["ssid"]).encode()) <= 32:
            raise ValueError("WiFi 이름은 1~32바이트")
        if d.get("password") and not 8 <= len(d["password"]) <= 63:
            raise ValueError("WiFi 비밀번호는 8~63자")
        for k in ("ssid", "channel", "hidden", "isolate", "max_clients", "security"):
            if k in d:
                self.wifi[k] = d[k]
        if d.get("password"):
            self.wifi["password"] = d["password"]
        time.sleep(0.5)
        return {"ok": True}

    def wan_get(self):
        return dict(self.wan, bearer=self._bearer())

    def wan_set_data(self, d):
        for k in ("data_enabled", "roaming_allowed"):
            if k in d:
                self.wan[k] = bool(d[k])
        return {"ok": True}

    def wan_set_apn(self, d):
        time.sleep(1.5)
        if d.get("apn_mode") == "manual" and d.get("apn") == "fail.test":
            return {"ok": False, "rolled_back": True, "restored_connected": True,
                    "error": "새 APN으로 연결하지 못해 이전 설정으로 되돌렸습니다"}
        self.wan.update(apn_mode=d.get("apn_mode", "auto"), apn=d.get("apn", ""),
                        username=d.get("username", ""), ip_type=d.get("ip_type", "ipv4v6"))
        return {"ok": True, "rolled_back": False, "bearer": self._bearer()}

    def device(self):
        return {"model": "JZ01-45-V33 4G Modem Stick", "hostname": "openstick",
                "os": "Debian GNU/Linux 13 (trixie)", "kernel": "6.12.1-msm8916",
                "imei": "350000000000000", "modem_firmware": "MPSS.DPM.1.0.c7-00193-M8916EAAAANVZM-1",
                "iccid": "8982300000000000000", "imsi": "450080000000000", "sim_operator": "KT",
                "lan_ip": "192.168.100.1"}

    def reboot(self):
        return {"ok": True}

    # 문자
    def messages_list(self, q):
        return self.msgs.list(q.get("limit", 50), q.get("before"))

    def messages_send(self, d):
        return self.msgs.send(d.get("number", ""), d.get("text", ""))

    def messages_delete(self, d):
        if d.get("older_than_days") is not None:
            return self.msgs.delete(older_than_days=d["older_than_days"])
        return self.msgs.delete(d.get("ids") or [])

    def messages_read(self, d):
        return self.msgs.mark_read(d.get("ids"))

    def messages_retry(self, d):
        return {"ok": True}

    def messages_media(self, msg_id, part):
        return self.msgs.media(msg_id, part)


def _png(w=96, h=64):
    rows = b"".join(b"\x00" + bytes(v for x in range(w) for v in (x * 255 // w, y * 255 // h, 180)) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class _MockMessages(sms.Messages):
    """실제 저장 로직 그대로, 모뎀만 가짜 (임시 폴더에 sqlite)."""

    def __init__(self):
        super().__init__(data_dir=tempfile.mkdtemp(prefix="osa-mock-"), start=False)
        now = int(time.time())
        self._add("in", "+821000000001", now - 3600, "sms", "수신테스트01", "received", 1)
        self._add("in", "+821000000001", now - 3500,
                  "sms", "한글테스트하나둘셋넷" * 6 + "총육십오줄", "received", 1)
        mid = self._add("in", "+821000000001", now - 3400, "mms", "사진 보냅니다", "retrieved", 0, subject="[제목없음]")
        with open(os.path.join(self.media_dir, f"{mid}-0"), "wb") as f:
            f.write(_png())
        self._set(mid, parts='[{"type": "image/png", "name": "IMG_0001.png", "file": "%d-0", "size": 1}]' % mid)
        self._add("in", "+821000000002", now - 600, "mms", None, "error", 0, error="MMSC HTTP 연결 실패")
        self._add("out", "+821000000001", now - 3300, "sms", "[OpenStick] 문자 수신 확인", "sent", 1)

    def _add(self, direction, number, ts, kind, text, status, read, subject=None, error=None):
        cur = self.db.execute(
            "INSERT INTO messages(uid,direction,number,ts,kind,text,subject,status,read,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (os.urandom(8).hex(), direction, number, ts, kind, text, subject, status, read, error))
        self.db.commit()
        return cur.lastrowid

    def send(self, number, text):
        number = number.replace("-", "").replace(" ", "")
        if not sms.NUMBER_RE.fullmatch(number):
            raise ValueError("전화번호 형식 오류")
        if not 1 <= len(text) <= 1000:
            raise ValueError("내용은 1~1000자")
        time.sleep(0.8)
        return {"ok": True, "id": self._add("out", number, int(time.time()), "sms", text, "sent", 1)}
