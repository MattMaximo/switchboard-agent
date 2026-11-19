"""Files between an owner's agents (0.7): end-to-end encrypted, the relay stores only ciphertext.

Sending a file
    1. POST /api/agent/file/new {to, name, size}        -> {id, n, chunk}
    2. PUT  /api/agent/file/chunk?id=&i=  (raw bytes)  each chunk encrypted with a fresh random
       AES-256-GCM key for this file: nonce = 4 zero bytes || i (8 bytes), AAD = LABEL || id || i || n.
    3. POST /api/agent/file/send {to, files: [id...], blob}: blob is a normal HPKE-sealed, signed message
       {"type": "file", "text": note, "files": [{id, name, size, sha256, key, n, chunk}]}.
The key never reaches the relay in the clear: it travels only inside the sealed message. The relay sees the
file name and size (it shows them on the owner's dashboard), never the contents.

Receiving: the recipient's client reads the sealed manifest, downloads and decrypts every chunk, checks the
size and SHA-256, and saves the file under a safe local name (never a path from the sender, never over an
existing file). Then it tells the relay to delete its copy (POST /api/agent/file/done).
"""
import base64
import hashlib
import math
import os
import re
import secrets
import tempfile
import time
import unicodedata

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHUNK = 1024 * 1024
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 10
TAG = 16
LABEL = b"switchboard-v1/file"
EXPIRES_DAYS = 7
_FID = re.compile(r"f[0-9a-f]{24}")
_SHA = re.compile(r"[0-9a-f]{64}")
_RESERVED = re.compile(r"(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", re.I)


class FileError(ValueError):
    """Safe to show."""


def human(n):
    if n < 1024:
        return "%d bytes" % n
    if n < 1024 * 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


def parts(size, chunk=CHUNK):
    return max(1, int(math.ceil(size / float(chunk))))


def _nonce(i):
    return b"\x00\x00\x00\x00" + i.to_bytes(8, "big")


def _aad(fid, i, n):
    return LABEL + fid.encode() + i.to_bytes(4, "big") + n.to_bytes(4, "big")


def encrypt_chunk(key, fid, i, n, data):
    return AESGCM(key).encrypt(_nonce(i), data, _aad(fid, i, n))


def decrypt_chunk(key, fid, i, n, data):
    try:
        return AESGCM(key).decrypt(_nonce(i), data, _aad(fid, i, n))
    except Exception:
        raise FileError("a part of the file was tampered with or is corrupt")


def safe_name(name):
    """A local file name from a sender-supplied one: no folders, no hidden or reserved names, no control chars."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(u"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069<>:\"|?*]", "_", name)
    name = name.strip().strip(".").strip()
    if not name:
        name = "file"
    if _RESERVED.fullmatch(name):
        name = "_" + name
    if len(name.encode("utf-8")) > 120:
        stem, ext = os.path.splitext(name)
        ext = ext if len(ext.encode("utf-8")) <= 16 else ""
        while len((stem + ext).encode("utf-8")) > 120:
            stem = stem[:-1]
        name = (stem or "file") + ext
    return name


def claim_path(folder, name):
    """Create an empty file in `folder` for `name` without overwriting anything: name, then 'name (2).ext', ...
    Returns the path; the caller atomically replaces the placeholder with the finished file."""
    stem, ext = os.path.splitext(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for k in range(1, 10000):
        cand = os.path.join(folder, name if k == 1 else "%s (%d)%s" % (stem, k, ext))
        try:
            os.close(os.open(cand, flags, 0o600))
            return cand
        except FileExistsError:
            continue
    raise FileError("too many files called %s in %s" % (name, folder))


def check_manifest(files):
    """Validate the file list inside a sealed message. Raises FileError."""
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise FileError("bad file list")
    out = []
    for f in files:
        if not isinstance(f, dict):
            raise FileError("bad file entry")
        fid, size, n, chunk = f.get("id"), f.get("size"), f.get("n"), f.get("chunk")
        if not (isinstance(fid, str) and _FID.fullmatch(fid)):
            raise FileError("bad file id")
        if not (isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= MAX_FILE):
            raise FileError("bad file size")
        if chunk != CHUNK or n != parts(size):
            raise FileError("bad file layout")
        if not (isinstance(f.get("sha256"), str) and _SHA.fullmatch(f["sha256"])):
            raise FileError("bad file checksum")
        try:
            key = base64.b64decode(f.get("key") or "", validate=True)
        except Exception:
            key = b""
        if len(key) != 32:
            raise FileError("bad file key")
        out.append({"id": fid, "name": str(f.get("name") or "file")[:400], "size": size, "n": n,
                    "chunk": chunk, "sha256": f["sha256"], "key": f["key"]})
    return out


def _inside_home(p):
    """True for a path in Switchboard's own folder (keys, tokens), following symlinks and ~ on both sides.
    Files other agents sent (home/files/...) can still be passed on."""
    from . import profiles
    root = os.path.realpath(os.path.expanduser(profiles.home()))

    def under(x, d):
        x, d = os.path.normcase(x), os.path.normcase(d)
        return x == d or x.startswith(d.rstrip(os.sep) + os.sep)
    real = os.path.realpath(p)
    return under(real, root) and not under(real, os.path.join(root, "files"))


def read_upload(path):
    """(name, size) for a local file to send, or FileError with a message for the person."""
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        raise FileError("%s isn't a file on this computer." % path)
    if _inside_home(p):
        raise FileError("Switchboard's own files can't be sent.")
    size = os.path.getsize(p)
    if size > MAX_FILE:
        raise FileError("%s is %s. Files can be up to %s each." % (os.path.basename(p), human(size), human(MAX_FILE)))
    return os.path.basename(p) or "file", size


def upload(put, path, fid, n, key):
    """Encrypt and upload a file's chunks with put(i, bytes). Returns its SHA-256 (hex) and size.
    The size is re-checked while reading, so a file that grows mid-upload is refused."""
    h, total = hashlib.sha256(), 0
    with open(os.path.expanduser(path), "rb") as f:
        for i in range(n):
            data = f.read(CHUNK)
            if i < n - 1 and len(data) != CHUNK:
                raise FileError("%s changed while it was being sent." % os.path.basename(path))
            h.update(data)
            total += len(data)
            put(i, encrypt_chunk(key, fid, i, n, data))
        if f.read(1):
            raise FileError("%s changed while it was being sent." % os.path.basename(path))
    return h.hexdigest(), total


def new_key():
    return secrets.token_bytes(32)


PARTIAL_TTL = 3600


def sweep_partials(folder, now=None):
    """Remove partial downloads left by a crashed process (older than an hour), never a live one."""
    now = now or time.time()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for nm in names:
        if not nm.startswith(".sb-partial-"):
            continue
        p = os.path.join(folder, nm)
        try:
            st = os.lstat(p)
            if now - st.st_mtime > PARTIAL_TTL:
                os.remove(p)
        except OSError:
            pass


def download(get, rec, folder):
    """Download, decrypt and verify one file (manifest rec) into `folder` with get(i) -> bytes.
    Returns the saved path. Nothing is left behind on failure."""
    os.makedirs(folder, mode=0o700, exist_ok=True)
    key = base64.b64decode(rec["key"])
    fid, n, size = rec["id"], rec["n"], rec["size"]
    sweep_partials(folder)
    # a temp file of its own per attempt: two downloads of the same file never share (or delete) one
    fd, tmp = tempfile.mkstemp(prefix=".sb-partial-%s-" % fid, dir=folder)
    os.chmod(tmp, 0o600)
    h, total = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as out:
            for i in range(n):
                want = (CHUNK if i < n - 1 else size - (n - 1) * CHUNK) + TAG
                ct = get(i)
                if len(ct) != want:
                    raise FileError("a part of the file has the wrong size")
                data = decrypt_chunk(key, fid, i, n, ct)
                h.update(data)
                total += len(data)
                out.write(data)
        if total != size or h.hexdigest() != rec["sha256"]:
            raise FileError("the file doesn't match its checksum")
        final = claim_path(folder, safe_name(rec.get("name")))
        os.replace(tmp, final)
        return final
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise

