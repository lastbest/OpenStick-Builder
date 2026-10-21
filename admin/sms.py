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
- Sending: SMS through ModemManager (it splits long texts into concatenated SMS),
  MMS by POSTing an m-send-req to the MMSC over the same data connection the
  retrieval uses.

Only the Python standard library is used. MMS PDUs follow OMA MMS 1.x / WSP.
"""
import base64
import binascii
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
MMS_SEND_MAX_BYTES = 1024 * 1024        # attachments of one outgoing MMS; carriers cap lower
MMS_SEND_TIMEOUT = 120
MEDIA_CAP_BYTES = 500 * 1024 * 1024     # all MMS attachments together
MIN_FREE_BYTES = 300 * 1024 * 1024      # keep this much free on the rootfs
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
        elif f == 0x0B:                         # Message-ID (m-send-conf)
            h["message_id"] = r.text().decode("latin-1")
        elif f == 0x12:                         # X-Mms-Response-Status
            h["response_status"] = r.byte()
        elif f == 0x13:                         # X-Mms-Response-Text
            h["response_text"] = encoded_string(r)
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
                loc = decode_text(sub.text())   # always consume it: a part may carry both
                name = name or loc              # a name parameter and a Content-Location
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


# ---------------------------------------------------------------- WSP / MMS encoding
WK_CONTENT_IDS = {v: k for k, v in WK_CONTENT_TYPES.items()}
SMIL_CID = "smil"
SEND_STATUS = {                                 # X-Mms-Response-Status of an m-send-conf
    0x81: "알 수 없는 오류",
    0x82: "서비스 거부 (회선에 MMS 발신이 막혀 있을 수 있습니다)",
    0x83: "메시지 형식 오류",
    0x84: "발신 번호를 확인할 수 없음",
    0x85: "메시지를 찾을 수 없음",
    0x86: "네트워크 오류",
    0x87: "받는 번호 형식 오류",
    0x88: "받는 번호를 확인할 수 없음",
    0x89: "지원하지 않는 메시지",
    0x8A: "메시지가 너무 큽니다",
}


class Writer:
    """The write side of Reader: WSP primitives, same encoding rules."""

    def __init__(self):
        self.b = bytearray()

    def byte(self, v):
        self.b.append(v & 0xFF)
        return self

    def raw(self, data):
        self.b += data
        return self

    def uintvar(self, n):
        if n < 0:
            raise PduError("negative uintvar")
        out = bytearray([n & 0x7F])
        n >>= 7
        while n:
            out.insert(0, (n & 0x7F) | 0x80)
            n >>= 7
        return self.raw(out)

    def short(self, n):
        if not 0 <= n <= 0x7F:
            raise PduError("not a short-integer")
        return self.byte(0x80 | n)

    def long_int(self, n):
        raw = n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
        return self.byte(len(raw)).raw(raw)

    def integer(self, n):
        return self.short(n) if n <= 0x7F else self.long_int(n)

    def text(self, s):
        raw = s.encode("utf-8") if isinstance(s, str) else s
        if raw[:1] >= b"\x80":
            self.byte(0x7F)                     # Quote: not a plain token any more
        return self.raw(raw).byte(0)

    def quoted(self, s):
        return self.byte(0x22).raw(s.encode("utf-8")).byte(0)

    def value_length(self, n):
        return self.byte(n) if n <= 30 else self.byte(31).uintvar(n)

    def data(self, payload):
        return self.value_length(len(payload)).raw(payload)

    def encoded_string(self, s):
        """Charset-tagged text, so Korean subjects survive the trip."""
        raw = s.encode("utf-8")
        if raw.isascii():
            return self.text(raw)
        return self.data(bytes(Writer().integer(106).raw(raw).byte(0).b))


def _ct_value(media, params=None):
    """A content-type field value (header or multipart part header)."""
    wk = WK_CONTENT_IDS.get(media.lower())
    if not params:
        return bytes(Writer().short(wk).b) if wk is not None else bytes(Writer().text(media).b)
    inner = Writer()
    if wk is not None:
        inner.short(wk)
    else:
        inner.text(media)
    for k, v in params.items():
        if k == "charset":
            inner.byte(0x81).integer(v)
        elif k == "type":
            inner.byte(0x89).text(v)
        elif k == "start":
            inner.byte(0x8A).text(v)
        elif k == "name":
            inner.byte(0x97).encoded_string(v)
        else:
            raise PduError(f"unsupported content-type parameter {k}")
    return bytes(Writer().data(bytes(inner.b)).b)


def _xml(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_smil(parts):
    """A minimal slide layout. Without it many handsets show an attachment list
    instead of the picture, and some refuse the message outright."""
    media = [p for p in parts if p["type"].startswith(("image/", "video/", "audio/"))]
    txt = next((p for p in parts if p["type"] == "text/plain"), None)
    slides = []
    for i, p in enumerate(media or [None]):
        tag = ""
        if p is not None:
            kind = "img" if p["type"].startswith("image/") else p["type"].split("/", 1)[0]
            tag = f'<{kind} src="{_xml(p["name"])}" region="Image"/>'
        if txt is not None and i == 0:
            tag += f'<text src="{_xml(txt["name"])}" region="Text"/>'
        slides.append(f'<par dur="8000ms">{tag}</par>')
    return ('<smil><head><layout><root-layout width="320px" height="480px"/>'
            '<region id="Image" top="0" left="0" width="100%" height="60%" fit="meet"/>'
            '<region id="Text" top="60%" left="0" width="100%" height="40%"/>'
            f'</layout></head><body>{"".join(slides)}</body></smil>').encode("utf-8")


def build_multipart(parts):
    """application/vnd.wap.multipart.related body: count, then per part
    headers-length, data-length, headers, data."""
    w = Writer().uintvar(len(parts))
    for p in parts:
        params = {}
        if p.get("charset"):
            params["charset"] = p["charset"]
        if p.get("name"):
            params["name"] = p["name"]
        h = Writer().raw(_ct_value(p["type"], params))
        if p.get("name"):
            h.byte(0x8E).text(p["name"])            # Content-Location
        if p.get("cid"):
            h.byte(0xC0).quoted(f'<{p["cid"]}>')    # Content-ID, what `start` points at
        head = bytes(h.b)
        w.uintvar(len(head)).uintvar(len(p["data"])).raw(head).raw(p["data"])
    return bytes(w.b)


def build_send_req(tid, to, parts, subject=None):
    """m-send-req. `parts` is [{type, name, data[, charset, cid]}], SMIL part first.

    From carries the insert-address-token so the MMSC fills in our own number:
    a handset is not meant to claim its MSISDN itself and carriers reject the
    message when the claim does not match the SIM. Content-Type must be the last
    header - everything after it is the body."""
    w = Writer()
    w.byte(0x8C).byte(0x80)                       # X-Mms-Message-Type: m-send-req
    w.byte(0x98).text(tid)                        # Transaction-Id
    w.byte(0x8D).byte(0x92)                       # MMS-Version: 1.2
    w.byte(0x85).long_int(int(time.time()))       # Date
    w.byte(0x89).data(b"\x81")                    # From: insert-address-token
    for n in to:
        w.byte(0x97).encoded_string(f"{n}/TYPE=PLMN")   # To
    if subject:
        w.byte(0x96).encoded_string(subject)
    w.byte(0x8A).byte(0x80)                       # Message-Class: personal
    w.byte(0x86).byte(0x81)                       # Delivery-Report: no
    w.byte(0x90).byte(0x81)                       # Read-Reply: no
    body = build_multipart(parts)
    w.byte(0x84).raw(_ct_value("application/vnd.wap.multipart.related",
                               {"type": "application/smil", "start": f"<{SMIL_CID}>"}))
    return bytes(w.b) + body


def parse_send_conf(data):
    h = parse_mms_headers(Reader(data))
    if h.get("type") != 0x81:                     # m-send-conf (0x81; 0x85 is m-acknowledge-ind)
        raise PduError(f"unexpected MMS type {h.get('type')}")
    return h


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
  status TEXT NOT NULL,              -- received | sending | sent | failed | pending | retrieved | expired | error
  read INTEGER NOT NULL DEFAULT 0,
  parts TEXT,                        -- JSON [{type,name,file,size}]
  mms_url TEXT, mms_tid TEXT, mms_expiry INTEGER, retries INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
DROP INDEX IF EXISTS messages_ts;               -- v0.1 had (ts) only
CREATE INDEX IF NOT EXISTS messages_ts_id ON messages(ts, id);
CREATE INDEX IF NOT EXISTS messages_unread ON messages(direction, read);
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
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "pruned" not in cols:                # v0.2: attachments removed to free space
            self.db.execute("ALTER TABLE messages ADD COLUMN pruned INTEGER NOT NULL DEFAULT 0")
            self.db.commit()
        self.lock = threading.Lock()
        self.wan_if, self.mmsc_url = wan_if, mmsc_url
        self.last_ue_try = 0
        self.last_poll = None
        self.last_error = None
        if start:
            threading.Thread(target=self._loop, name="sms-collector", daemon=True).start()

    # ----- public API
    def list(self, limit=50, before=None):
        """Newest first. `before` is the cursor "ts:id" of the last row already shown.
        Messages queued at the carrier can arrive late (older ts, newer id), so the
        cursor must follow the sort order (ts, id), not the id alone."""
        limit = max(1, min(int(limit), 200))
        q, args = "SELECT * FROM messages", []
        if before:
            m = re.fullmatch(r"(\d+):(\d+)", str(before))
            if not m:
                raise ValueError("잘못된 커서")
            ts, mid = int(m.group(1)), int(m.group(2))
            q += " WHERE ts < ? OR (ts = ? AND id < ?)"
            args += [ts, ts, mid]
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

    def delete(self, ids=None, older_than_days=None):
        with self.lock:
            if older_than_days is not None:
                days = int(older_than_days)
                if days < 1:
                    raise ValueError("기간은 1일 이상")
                cutoff = int(time.time()) - days * 86400
                ids = [r[0] for r in self.db.execute("SELECT id FROM messages WHERE ts < ?", (cutoff,))]
            else:
                ids = [int(i) for i in (ids or [])]
                if not ids or len(ids) > 1000:
                    raise ValueError("삭제할 문자를 고르세요 (최대 1000건)")
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

    # ----- storage
    def storage(self):
        media = 0
        for e in os.scandir(self.media_dir):
            if e.is_file():
                media += e.stat().st_size
        st = os.statvfs(self.dir)
        with self.lock:
            count = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            pruned = self.db.execute("SELECT COUNT(*) FROM messages WHERE pruned=1").fetchone()[0]
        db_bytes = os.path.getsize(os.path.join(self.dir, "messages.db"))
        return {"messages": count, "media_bytes": media, "db_bytes": db_bytes, "pruned": pruned,
                "media_cap_bytes": MEDIA_CAP_BYTES, "min_free_bytes": MIN_FREE_BYTES,
                "disk_free_bytes": st.f_bavail * st.f_frsize, "disk_total_bytes": st.f_blocks * st.f_frsize}

    def prune(self):
        """Delete the oldest MMS attachments (text is kept) while attachments exceed
        MEDIA_CAP_BYTES or the disk has less than MIN_FREE_BYTES free."""
        s = self.storage()
        media, free = s["media_bytes"], s["disk_free_bytes"]
        if media <= MEDIA_CAP_BYTES and free >= MIN_FREE_BYTES:
            return 0
        with self.lock:
            rows = self.db.execute("SELECT id, parts FROM messages WHERE parts IS NOT NULL AND parts != '[]'"
                                   " ORDER BY ts ASC, id ASC").fetchall()
        n = 0
        for r in rows:
            if media <= MEDIA_CAP_BYTES and free >= MIN_FREE_BYTES:
                break
            for p in json.loads(r["parts"]):
                path = os.path.join(self.media_dir, p["file"])
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                    media, free = media - size, free + size
                except OSError:
                    pass
            self._set(r["id"], parts="[]", pruned=1)
            n += 1
        if n:
            print(f"sms: pruned attachments of {n} old MMS (media {media >> 20} MB, free {free >> 20} MB)", flush=True)
        return n

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
            self.db.execute("UPDATE messages SET status='pending', retries=0, error=NULL"
                            " WHERE id=? AND kind='mms' AND direction='in'",   # 보낸 MMS는 받아올 것이 없다
                            (int(msg_id),))
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

    def send_mms(self, number, text="", subject=None, attachments=()):
        """POST an m-send-req to the MMSC over wwan0 - the same path retrieval uses.

        The modem is not involved: MMS travels over the data connection, the MMSC
        answers with an m-send-conf and its response-status says whether the
        carrier took the message."""
        number = re.sub(r"[\s-]", "", str(number))
        if not NUMBER_RE.fullmatch(number):
            raise ValueError("전화번호 형식 오류")
        if not self.mmsc_url:
            raise ValueError("MMSC 주소가 설정되어 있지 않습니다")
        text = str(text or "")
        if len(text) > 4000:
            raise ValueError("내용은 4000자 이하")
        subject = str(subject).strip()[:120] if subject else None
        atts = []
        for a in attachments:
            ctype = str(a.get("type") or "application/octet-stream").lower()
            if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", ctype):
                raise ValueError("첨부 형식 오류")
            atts.append({"type": ctype, "name": _safe_name(a.get("name")) or f"file{len(atts)}",
                         "data": a["data"]})
        if not text and not atts:
            raise ValueError("내용이나 첨부가 필요합니다")
        total = sum(len(a["data"]) for a in atts)
        if total > MMS_SEND_MAX_BYTES:
            raise ValueError(f"첨부는 합계 {MMS_SEND_MAX_BYTES >> 10}KB 이하 (현재 {total >> 10}KB)")

        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO messages(uid,direction,number,ts,kind,text,subject,status,read)"
                " VALUES(?,?,?,?,?,?,?,'sending',1)",
                (f"out-{now}-{os.urandom(4).hex()}", "out", number, now, "mms", text, subject))
            self.db.commit()
        msg_id = cur.lastrowid

        if _free_bytes(self.dir) - total < MIN_FREE_BYTES // 2:
            self.prune()
        stored = []
        for i, a in enumerate(atts):             # keep a copy so the thread shows what was sent
            fname = f"{msg_id}-{i}"
            with open(os.path.join(self.media_dir, fname), "wb") as f:
                f.write(a["data"])
            stored.append({"type": a["type"], "name": a["name"], "file": fname, "size": len(a["data"])})

        parts = []
        if text:
            parts.append({"type": "text/plain", "name": "text.txt", "charset": 106,
                          "data": text.encode("utf-8")})
        used = {"text.txt", "smil.xml"}
        for i, a in enumerate(atts):
            wire = _wire_name(a["name"], i)
            while wire in used:
                wire = f"{i}-{wire}"
            used.add(wire)
            parts.append({"type": a["type"], "name": wire, "data": a["data"]})
        smil = {"type": "application/smil", "name": "smil.xml", "cid": SMIL_CID,
                "data": build_smil(parts)}
        tid = os.urandom(6).hex()
        pdu = build_send_req(tid, [number], [smil] + parts, subject=subject)

        status, err, mid = "failed", None, None
        try:
            h = self._post_mms(pdu)
            rs = h.get("response_status", 0x80)
            if rs != 0x80:
                raise RuntimeError(SEND_STATUS.get(rs, f"MMSC 거부 (0x{rs:02X})")
                                   + (f" - {h['response_text']}" if h.get("response_text") else ""))
            mid, status = h.get("message_id"), "sent"
        except Exception as e:  # noqa: BLE001 - the row keeps the reason either way
            err = str(e)[:300]
        self._set(msg_id, status=status, parts=json.dumps(stored), error=err, mms_tid=tid)
        if status != "sent":
            raise RuntimeError(f"발송 실패: {err}")
        return {"ok": True, "id": msg_id, "message_id": mid}

    def _post_mms(self, pdu):
        """Hand the m-send-req to the MMSC and return the m-send-conf headers.
        Its own method so the mock backend can build a real PDU without a modem."""
        fd, req = tempfile.mkstemp(dir=self.dir)
        with os.fdopen(fd, "wb") as f:
            f.write(pdu)
        resp = req + ".resp"
        try:
            code = _run(["curl", "-s", "-m", str(MMS_SEND_TIMEOUT), "--interface", self.wan_if,
                         "-H", "Content-Type: application/vnd.wap.mms-message",
                         "-H", "Accept: application/vnd.wap.mms-message",
                         "--data-binary", f"@{req}", "-o", resp, "-w", "%{http_code}",
                         self.mmsc_url], timeout=MMS_SEND_TIMEOUT + 30, check=False).strip()
            if code != "200":
                raise RuntimeError(f"MMSC HTTP {code or '연결 실패'}")
            with open(resp, "rb") as f:
                return parse_send_conf(f.read())
        finally:
            for p in (req, resp):
                try:
                    os.remove(p)
                except OSError:
                    pass

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
        self.prune()
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
        stored, texts, pruned = [], [], False
        self.prune()
        for i, p in enumerate(parts):
            if p["type"] == "application/smil":
                continue
            if p["type"].startswith("text/"):
                texts.append(decode_text(p["data"], p["charset"] or 106).strip())
                continue
            fname = f"{r['id']}-{i}"
            st = os.statvfs(self.dir)
            if st.f_bavail * st.f_frsize - len(p["data"]) < MIN_FREE_BYTES // 2:
                pruned = True                   # disk nearly full even after pruning: keep text only
                continue
            with open(os.path.join(self.media_dir, fname), "wb") as f:
                f.write(p["data"])
            stored.append({"type": p["type"], "name": _safe_name(p["name"]), "file": fname, "size": len(p["data"])})
        sender = h.get("from")
        self._set(r["id"], status="retrieved", text="\n".join(t for t in texts if t), subject=h.get("subject"),
                  parts=json.dumps(stored), error=None, pruned=int(pruned),
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


def _free_bytes(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except (AttributeError, OSError):          # 모의 모드로 돌리는 PC에는 statvfs가 없을 수 있다
        return 1 << 62


def decode_attachments(items):
    """웹에서 온 MMS 첨부 [{name, type, data(base64)}] -> 바이트."""
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 10:
        raise ValueError("첨부는 최대 10개")
    out = []
    for a in items:
        if not isinstance(a, dict) or not isinstance(a.get("data"), str):
            raise ValueError("첨부 형식 오류")
        try:
            data = base64.b64decode(a["data"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("첨부 디코드 실패")
        if not data:
            raise ValueError("빈 첨부")
        out.append({"name": a.get("name"), "type": a.get("type"), "data": data})
    return out


def _wire_name(name, i):
    """Content-Location on the wire. The SMIL src and the Content-Location have to
    match as strings, and handsets routinely drop a part whose name carries spaces
    or Hangul, so anything but plain ASCII gets a generated name."""
    if name and re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name):
        return name
    ext = re.search(r"\.[A-Za-z0-9]{1,8}$", name or "")
    return f"part{i}{ext.group(0).lower() if ext else ''}"
