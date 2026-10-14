"""SMS / MMS: ModemManager collector, sqlite archive, MMS (WAP push) retrieval.

Flow
- A collector thread polls ModemManager every POLL_SEC seconds:
  * makes sure the EPS UE mode allows SMS (csps-2; the modem comes up as ps-2,
    a data-only attach in which the network never delivers SMS),
  * copies every received SMS into sqlite (deduplicated) and deletes it from the
    modem (modem memory is small and is lost on reboot),
  * turns WAP-push MMS notifications into "mms" rows and downloads the message
    from the MMSC over the normal data connection (Korean carriers send long texts
    and pictures as MMS).
- Sending: SMS only (ModemManager splits long texts into concatenated SMS).

Only the Python standard library is used. MMS PDUs follow OMA MMS 1.x / WSP.
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime

POLL_SEC = 15
UE_MODE = "csps-2"               # data centric, combined attach -> SMS over SGs
UE_RETRY_SEC = 60
MMS_MAX_BYTES = 3 * 1024 * 1024
MMS_MAX_RETRIES = 6
NUMBER_RE = re.compile(r"\+?[0-9]{3,20}")


def _run(args, timeout=30, check=True):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args[0]}: {(p.stderr or p.stdout).strip()[:300]}")
    return p.stdout


def _mmcli_json(*args):
    try:
        return json.loads(_run(["mmcli", "-J", *args], timeout=20))
    except Exception:  # noqa: BLE001 - modem may be absent or restarting
        return {}


def _v(x):
    return None if x in (None, "", "--") else x


def parse_ts(s):
    if not s:
        return int(time.time())
    s = s.strip()
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return int(time.time())


# ---------------------------------------------------------------- WSP / MMS PDU
WK_CONTENT_TYPES = {
    0x03: "text/plain", 0x1D: "image/gif", 0x1E: "image/jpeg", 0x1F: "image/tiff",
    0x20: "image/png", 0x21: "image/vnd.wap.wbmp", 0x22: "application/vnd.wap.multipart.*",
    0x23: "application/vnd.wap.multipart.mixed", 0x26: "application/vnd.wap.multipart.alternative",
    0x33: "application/vnd.wap.multipart.related", 0x3E: "application/vnd.wap.mms-message",
}
CHARSETS = {3: "ascii", 4: "latin-1", 106: "utf-8", 1000: "utf-16-be", 1015: "utf-16",
            36: "euc-kr", 38: "euc-kr", 17: "shift_jis", 2026: "big5", 113: "gbk"}


class PduError(ValueError):
    pass


class Reader:
    def __init__(self, data, pos=0, end=None):
        self.d, self.p, self.end = data, pos, len(data) if end is None else end

    def eof(self):
        return self.p >= self.end

    def byte(self):
        if self.p >= self.end:
            raise PduError("truncated")
        b = self.d[self.p]
        self.p += 1
        return b

    def peek(self):
        if self.p >= self.end:
            raise PduError("truncated")
        return self.d[self.p]

    def take(self, n):
        if n < 0 or self.p + n > self.end:
            raise PduError("truncated")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def uintvar(self):
        v = 0
        for _ in range(5):
            b = self.byte()
            v = (v << 7) | (b & 0x7F)
            if not b & 0x80:
                return v
        raise PduError("bad uintvar")

    def value_length(self):
        b = self.byte()
        if b <= 30:
            return b
        if b == 31:
            return self.uintvar()
        raise PduError("not a value-length")

    def text(self, lenient=False):
        i = self.d.find(b"\x00", self.p, self.end)
        if i < 0:
            if not lenient:
                raise PduError("unterminated text")
            i = self.end                        # tolerate a string that runs to the end of its field
            s = self.d[self.p:i]
            self.p = i
            self.unterminated = True
            return s[1:] if s[:1] in (b"\x7f", b'"') else s
        s = self.d[self.p:i]
        self.p = i + 1
        if s[:1] in (b"\x7f", b'"'):
            s = s[1:]
        return s

    def long_int(self):
        n = self.byte()
        if n > 30:
            raise PduError("bad long-integer")
        return int.from_bytes(self.take(n), "big")

    def integer(self):
        b = self.peek()
        if b & 0x80:
            return self.byte() & 0x7F
        return self.long_int()

    def skip_value(self):
        """Skip any WSP header value (generic encoding rules)."""
        b = self.peek()
        if b <= 31:
            self.take(self.value_length())
        elif b < 128:
            self.text()
        else:
            self.byte()


def decode_text(raw, charset=106):
    enc = CHARSETS.get(charset, "utf-8")
    try:
        return raw.decode(enc)
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def encoded_string(r):
    if r.peek() <= 31:
        vlen = r.value_length()
        end = r.p + vlen
        sub = Reader(r.d, r.p, end)
        charset = sub.integer() if not sub.eof() else 106
        s = decode_text(sub.text(lenient=True), charset) if not sub.eof() else ""
        r.p = end
        if getattr(sub, "unterminated", False) and r.p < r.end and r.d[r.p] == 0:
            r.p += 1                            # its NUL, if it sits right after the field
        return s
    return decode_text(r.text())


def content_type(r):
    """Returns (media_type, params dict). Consumes the whole field."""
    b = r.peek()
    if b >= 128:
        return WK_CONTENT_TYPES.get(r.byte() & 0x7F, "application/octet-stream"), {}
    if b >= 32:
        return r.text().decode("latin-1"), {}
    vlen = r.value_length()
    end = r.p + vlen
    sub = Reader(r.d, r.p, end)
    if sub.peek() >= 128:
        media = WK_CONTENT_TYPES.get(sub.byte() & 0x7F, "application/octet-stream")
    elif sub.peek() >= 32:
        media = sub.text().decode("latin-1")
    else:
        media = WK_CONTENT_TYPES.get(sub.long_int(), "application/octet-stream")
    params = {}
    while not sub.eof():
        k = sub.peek()
        if k >= 128:
            k = sub.byte() & 0x7F
            if k == 0x01:                       # charset
                params["charset"] = sub.integer()
            elif k in (0x05, 0x06, 0x17, 0x18):  # name / filename
                params["name"] = encoded_string(sub) if sub.peek() <= 31 else decode_text(sub.text())
            elif k in (0x09, 0x0A):              # type / start
                params["type" if k == 0x09 else "start"] = sub.text().decode("latin-1")
            else:
                sub.skip_value()
        else:                                   # untyped parameter: token-text value
            name = sub.text().decode("latin-1").lower()
            val = sub.text() if sub.peek() >= 32 and sub.peek() < 128 else None
            if val is None:
                sub.skip_value()
            else:
                params[name] = val.decode("latin-1")
    r.p = end
    return media, params


def parse_mms_headers(r):
    h = {}
    while not r.eof():
        b = r.byte()
        if b == 0x00:                           # tolerate stray NUL padding between headers
            continue
        if not b & 0x80:                        # application header: text name + text value
            r.p -= 1
            r.text()
            r.text()
            continue
        f = b & 0x7F
        if f == 0x0C:                           # X-Mms-Message-Type
            h["type"] = r.byte()
        elif f == 0x18:                         # Transaction-Id
            h["tid"] = r.text().decode("latin-1")
        elif f == 0x0D:                         # MMS-Version
            h["version"] = r.byte()
        elif f == 0x05:                         # Date
            h["date"] = r.long_int()
        elif f == 0x09:                         # From
            vlen = r.value_length()
            end = r.p + vlen
            if r.byte() == 0x80:
                h["from"] = encoded_string(r)
            r.p = end
        elif f == 0x17:                         # To
            h.setdefault("to", []).append(encoded_string(r))
        elif f == 0x16:                         # Subject
            h["subject"] = encoded_string(r)
        elif f == 0x0E:                         # Message-Size
            h["size"] = r.long_int()
        elif f == 0x08:                         # Expiry
            vlen = r.value_length()
            end = r.p + vlen
            kind = r.byte()
            v = r.long_int()
            h["expiry"] = v if kind == 0x80 else int(time.time()) + v
            r.p = end
        elif f == 0x03:                         # Content-Location
            h["location"] = r.text().decode("latin-1")
        elif f == 0x04:                         # Content-Type -> body follows
            h["content_type"], h["ct_params"] = content_type(r)
            break
        else:
            r.skip_value()
    return h


def parse_multipart(r):
    parts = []
    n = r.uintvar()
    for _ in range(min(n, 64)):
        hlen, dlen = r.uintvar(), r.uintvar()
        hend = r.p + hlen
        sub = Reader(r.d, r.p, hend)
        media, params = content_type(sub)
        name = params.get("name")
        while not sub.eof():                    # part headers: look for Content-Location
            b = sub.byte()
            if b == 0x8E:
                name = name or decode_text(sub.text())
            elif b & 0x80:
                sub.skip_value()
            else:
                sub.p -= 1
                sub.text()
                sub.text()
        r.p = hend
        parts.append({"type": media.lower(), "name": name, "charset": params.get("charset"),
                      "data": r.take(dlen)})
    return parts


def parse_wap_push(data):
    """WAP push (SMS user data, UDH already removed) -> MMS notification dict or None."""
    r = Reader(data)
    r.byte()                                    # TID
    if r.byte() != 0x06:                        # PDU type: push
        return None
    hlen = r.uintvar()
    body = r.p + hlen
    ct, _ = content_type(r)
    if ct != "application/vnd.wap.mms-message":
        return None
    h = parse_mms_headers(Reader(data, body))
    if h.get("type") != 0x82 or not h.get("location"):   # m-notification-ind
        return None
    return h


def parse_retrieve_conf(data):
    r = Reader(data)
    h = parse_mms_headers(r)
    if h.get("type") != 0x84:                   # m-retrieve-conf
        raise PduError(f"unexpected MMS type {h.get('type')}")
    ct = h.get("content_type", "")
    if "multipart" in ct:
        parts = parse_multipart(r)
    else:
        parts = [{"type": ct.lower(), "name": None, "charset": h.get("ct_params", {}).get("charset"),
                  "data": r.d[r.p:]}]
    return h, parts


def build_notifyresp(tid):
    """m-notifyresp-ind, status Retrieved."""
    return bytes([0x8C, 0x83, 0x98]) + tid.encode("latin-1") + b"\x00" + bytes([0x8D, 0x92, 0x95, 0x81])


# ---------------------------------------------------------------- store + collector
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  uid TEXT UNIQUE,
  direction TEXT NOT NULL,           -- in | out
  number TEXT,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,                -- sms | mms
  text TEXT,
  subject TEXT,
  status TEXT NOT NULL,              -- received | sent | failed | pending | retrieved | expired | error
  read INTEGER NOT NULL DEFAULT 0,
  parts TEXT,                        -- JSON [{type,name,file,size}]
  mms_url TEXT, mms_tid TEXT, mms_expiry INTEGER, retries INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
"""


class Messages:
    def __init__(self, data_dir="/var/lib/openstick-admin", wan_if="wwan0",
                 mmsc_url="http://mmsc.ktfwing.com:9082", start=True):
        self.dir = data_dir
        self.media_dir = os.path.join(data_dir, "mms")
        os.makedirs(self.media_dir, mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "messages.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.wan_if, self.mmsc_url = wan_if, mmsc_url
        self.last_ue_try = 0
        self.last_poll = None
        self.last_error = None
        if start:
            threading.Thread(target=self._loop, name="sms-collector", daemon=True).start()

    # ----- public API
    def list(self, limit=50, before_id=None):
        limit = max(1, min(int(limit), 200))
        q, args = "SELECT * FROM messages", []
        if before_id:
            q += " WHERE id < ?"
            args.append(int(before_id))
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
            unread = self.db.execute("SELECT COUNT(*) FROM messages WHERE direction='in' AND read=0").fetchone()[0]
        out = []
        for r in rows:
            d = dict(r)
            d["parts"] = [{k: p[k] for k in ("type", "name", "size")} for p in json.loads(d["parts"] or "[]")]
            for k in ("uid", "mms_url", "mms_tid", "retries"):
                d.pop(k, None)
            out.append(d)
        return {"messages": out, "unread": unread, "last_poll": self.last_poll, "error": self.last_error}

    def unread(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM messages WHERE direction='in' AND read=0").fetchone()[0]

    def mark_read(self, ids=None):
        with self.lock:
            if ids:
                self.db.executemany("UPDATE messages SET read=1 WHERE id=?", [(int(i),) for i in ids])
            else:
                self.db.execute("UPDATE messages SET read=1")
            self.db.commit()
        return {"ok": True}

    def delete(self, ids):
        ids = [int(i) for i in ids]
        with self.lock:
            for i in ids:
                row = self.db.execute("SELECT parts FROM messages WHERE id=?", (i,)).fetchone()
                for p in json.loads((row and row["parts"]) or "[]"):
                    try:
                        os.remove(os.path.join(self.media_dir, p["file"]))
                    except OSError:
                        pass
                self.db.execute("DELETE FROM messages WHERE id=?", (i,))
            self.db.commit()
        return {"ok": True, "deleted": len(ids)}

    def media(self, msg_id, part):
        with self.lock:
            row = self.db.execute("SELECT parts FROM messages WHERE id=?", (int(msg_id),)).fetchone()
        parts = json.loads((row and row["parts"]) or "[]")
        p = parts[int(part)] if 0 <= int(part) < len(parts) else None
        if not p:
            raise ValueError("없는 첨부")
        with open(os.path.join(self.media_dir, p["file"]), "rb") as f:
            return p["type"], f.read(), p.get("name")

    def retry(self, msg_id):
        with self.lock:
            self.db.execute("UPDATE messages SET status='pending', retries=0, error=NULL WHERE id=? AND kind='mms'", (int(msg_id),))
            self.db.commit()
        threading.Thread(target=self._fetch_pending, daemon=True).start()
        return {"ok": True}

    def send(self, number, text):
        number = re.sub(r"[\s-]", "", str(number))
        if not NUMBER_RE.fullmatch(number):
            raise ValueError("전화번호 형식 오류")
        text = str(text)
        if not 1 <= len(text) <= 1000:
            raise ValueError("내용은 1~1000자")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write(text)
            tmp = f.name
        path, status, err = None, "failed", None
        try:
            out = _run(["mmcli", "-m", "any", f"--messaging-create-sms=number='{number}'",
                        f"--messaging-create-sms-with-text={tmp}"])
            m = re.search(r"/SMS/(\d+)", out)
            if not m:
                raise RuntimeError("SMS 생성 실패")
            path = m.group(1)
            _run(["mmcli", "-s", path, "--send"], timeout=120)
            status = "sent"
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
        finally:
            os.unlink(tmp)
            if path:
                _run(["mmcli", "-m", "any", f"--messaging-delete-sms={path}"], check=False)
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO messages(uid,direction,number,ts,kind,text,status,read,error) VALUES(?,?,?,?,?,?,?,1,?)",
                (f"out-{now}-{os.urandom(4).hex()}", "out", number, now, "sms", text, status, err))
            self.db.commit()
        if status != "sent":
            raise RuntimeError(f"발송 실패: {err}")
        return {"ok": True, "id": cur.lastrowid}

    # ----- collector
    def _loop(self):
        while True:
            try:
                self.poll()
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:300]
            time.sleep(POLL_SEC)

    def poll(self):
        modem = _mmcli_json("-m", "any").get("modem")
        if not modem:
            return
        self._ensure_ue_mode(modem)
        for path in (modem.get("generic", {}).get("sms") or []) or self._list_paths():
            self._collect(path)
        self._fetch_pending()
        self.last_poll = int(time.time())

    def _list_paths(self):
        try:
            out = _run(["mmcli", "-m", "any", "--messaging-list-sms"], timeout=20)
        except Exception:  # noqa: BLE001
            return []
        return re.findall(r"(/org/freedesktop/ModemManager1/SMS/\d+)", out)

    def _ensure_ue_mode(self, modem):
        mode = ((modem.get("3gpp") or {}).get("eps") or {}).get("ue-mode-operation")
        if mode in ("ps-1", "ps-2") and time.time() - self.last_ue_try > UE_RETRY_SEC:
            self.last_ue_try = time.time()
            try:
                _run(["mmcli", "-m", "any", f"--3gpp-set-eps-ue-mode-operation={UE_MODE}"], timeout=90)
                print(f"sms: EPS UE mode {mode} -> {UE_MODE} (SMS reception enabled)", flush=True)
            except Exception as e:  # noqa: BLE001 - modem busy right after boot; retried next minute
                print(f"sms: setting EPS UE mode failed: {e}", flush=True)

    def _collect(self, path):
        s = _mmcli_json("-s", path).get("sms")
        if not s:
            return
        c, p = s.get("content", {}), s.get("properties", {})
        if p.get("state") != "received":         # still receiving parts, or our own outgoing
            return
        if p.get("pdu-type") not in ("deliver", "status-report"):
            return
        if p.get("pdu-type") == "deliver":
            number, ts = _v(c.get("number")), parse_ts(_v(p.get("timestamp")))
            text, data = _v(c.get("text")), c.get("data")
            raw = None
            if isinstance(data, list) and data:
                raw = bytes.fromhex("".join(data))
            elif isinstance(data, str) and _v(data):
                raw = bytes.fromhex(data)
            uid = hashlib.sha1(f"{number}|{ts}|{text}|{raw.hex() if raw else ''}".encode()).hexdigest()
            row = None
            if raw:
                try:
                    notif = parse_wap_push(raw)
                except PduError:
                    notif = None
                if notif:
                    row = dict(kind="mms", text=None, subject=notif.get("subject"), status="pending",
                               mms_url=notif["location"], mms_tid=notif.get("tid"),
                               mms_expiry=notif.get("expiry"), number=notif.get("from") or number)
                    uid = "mms-" + hashlib.sha1(notif["location"].encode()).hexdigest()
                else:
                    row = dict(kind="sms", text="(지원하지 않는 바이너리 메시지)", status="received")
            else:
                row = dict(kind="sms", text=text or "", status="received")
            row.setdefault("number", number)
            with self.lock:
                self.db.execute(
                    "INSERT OR IGNORE INTO messages(uid,direction,number,ts,kind,text,subject,status,mms_url,mms_tid,mms_expiry)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, "in", _clean_number(row["number"]), ts, row["kind"], row.get("text"), row.get("subject"),
                     row["status"], row.get("mms_url"), row.get("mms_tid"), row.get("mms_expiry")))
                self.db.commit()
        _run(["mmcli", "-m", "any", f"--messaging-delete-sms={path}"], check=False)

    def _fetch_pending(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, mms_url, mms_tid, mms_expiry, retries FROM messages WHERE kind='mms' AND status IN ('pending','error')"
                " AND retries < ?", (MMS_MAX_RETRIES,)).fetchall()
        for r in rows:
            if r["mms_expiry"] and r["mms_expiry"] < time.time():
                self._set(r["id"], status="expired")
                continue
            try:
                self._fetch_one(r)
            except Exception as e:  # noqa: BLE001
                self._set(r["id"], status="error", error=str(e)[:300], retries=r["retries"] + 1)

    def _fetch_one(self, r):
        fd, tmp = tempfile.mkstemp(dir=self.dir)
        os.close(fd)
        try:
            code = _run(["curl", "-s", "-m", "90", "--interface", self.wan_if, "--max-filesize", str(MMS_MAX_BYTES),
                         "-A", "Android-Mms/2.0", "-H", "Accept: application/vnd.wap.mms-message, */*",
                         "-o", tmp, "-w", "%{http_code}", r["mms_url"]], timeout=120, check=False).strip()
            if code != "200":
                raise RuntimeError(f"MMSC HTTP {code or '연결 실패'}")
            with open(tmp, "rb") as f:
                data = f.read()
        finally:
            os.unlink(tmp)
        h, parts = parse_retrieve_conf(data)
        stored, texts = [], []
        for i, p in enumerate(parts):
            if p["type"] == "application/smil":
                continue
            if p["type"].startswith("text/"):
                texts.append(decode_text(p["data"], p["charset"] or 106).strip())
                continue
            fname = f"{r['id']}-{i}"
            with open(os.path.join(self.media_dir, fname), "wb") as f:
                f.write(p["data"])
            stored.append({"type": p["type"], "name": _safe_name(p["name"]), "file": fname, "size": len(p["data"])})
        sender = h.get("from")
        self._set(r["id"], status="retrieved", text="\n".join(t for t in texts if t), subject=h.get("subject"),
                  parts=json.dumps(stored), error=None,
                  **({"number": _clean_number(sender)} if sender else {}))
        if r["mms_tid"] and self.mmsc_url:             # best effort, tells the MMSC we have it
            fd, ack = tempfile.mkstemp(dir=self.dir)
            with os.fdopen(fd, "wb") as f:
                f.write(build_notifyresp(r["mms_tid"]))
            try:
                _run(["curl", "-s", "-m", "30", "--interface", self.wan_if, "-o", "/dev/null",
                      "-H", "Content-Type: application/vnd.wap.mms-message", "--data-binary", f"@{ack}",
                      self.mmsc_url], timeout=40, check=False)
            finally:
                os.unlink(ack)

    def _set(self, msg_id, **cols):
        keys = ", ".join(f"{k}=?" for k in cols)
        with self.lock:
            self.db.execute(f"UPDATE messages SET {keys} WHERE id=?", (*cols.values(), msg_id))
            self.db.commit()


def _clean_number(n):
    if not n:
        return n
    n = re.sub(r"/TYPE=.*$", "", n)            # MMS addresses look like +8210.../TYPE=PLMN
    return n


def _safe_name(n):
    if not n:
        return None
    return re.sub(r"[^\w.\- ()\[\]가-힣]", "_", n)[:80]
