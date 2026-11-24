#!/usr/bin/env python3
"""kindle-push — move documents to and from a Kindle over USB (MTP) or the network.

One CLI, two transports:

  USB     the device as mounted by gvfs (what Nautilus/Dolphin use). No root, no
          libmtp bindings needed. Reads go through the mount, writes go through
          `gio` (creating a file over the FUSE mount fails with EOPNOTSUPP).
  Remote  a `kindle-push serve` instance on the machine the Kindle is plugged
          into. Same commands, add --url/--token. Works both directions.

Examples
  kindle-push doctor
  kindle-push ls -l documents
  kindle-push push paper.pdf notes.epub                    # -> documents/
  kindle-push push ~/papers/ --dir documents/Papers        # whole folder, recursive
  kindle-push push thesis.docx --convert                   # calibre -> PDF first
  kindle-push pull documents/report.pdf --to ~/out
  kindle-push pull --all documents --to ~/kindle-backup    # bulk, recursive, resumable
  kindle-push pull --all documents --to ~/b --since 30 --match '*.pdf'
  kindle-push rm documents/old.pdf
  kindle-push mkdir documents/Papers
  kindle-push watch ~/inbox --dir documents                # daemon: push what appears
  kindle-push serve --port 8737 --token SECRET             # remote endpoint
  kindle-push --url http://box:8737 --token SECRET pull --all documents --to ~/b
  kindle-push flush                                        # send the offline queue

Exit codes: 0 all good, 1 something failed, 2 usage/environment problem.
Nothing is ever uploaded to Amazon. The device is only read except for explicit
push/rm/mkdir.
"""

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
UID = os.getuid()
STATE = os.path.expanduser("~/.local/state/kindle-push")
QUEUE = os.path.join(STATE, "queue")
DEFAULT_DIR = "documents"
DOC_EXTS = {".pdf", ".epub", ".txt", ".kfx", ".azw", ".azw3", ".mobi", ".prc",
            ".cbz", ".cbr", ".htmlz", ".docx", ".odt", ".rtf", ".md"}
NATIVE_EXTS = {".pdf", ".epub", ".txt", ".kfx", ".azw3", ".mobi", ".prc", ".cbz", ".cbr"}
CHUNK = 256 * 1024
DIR_EXTS = {".sdr", ".cache", ".notebook"}   # always folders on a Kindle
FILE_EXTS = {".pdf", ".epub", ".txt", ".kfx", ".azw", ".azw3", ".mobi", ".prc", ".cbz",
             ".cbr", ".mf", ".meta", ".yjr", ".yjf", ".mbp1", ".mbs", ".mbp", ".phl",
             ".asc", ".bad_file", ".jpg", ".jpeg", ".png", ".gif", ".zip", ".json"}


# ------------------------------------------------------------------ utilities
class Fail(Exception):
    pass


def _mounted_mtp():
    """Host name of an already-mounted gvfs MTP volume, else None."""
    base = f"/run/user/{UID}/gvfs"
    if not os.path.isdir(base):
        return None
    for d in sorted(os.listdir(base)):
        if d.startswith("mtp:host="):
            return d[len("mtp:host="):]
    return None


def _usb_uri_from_sysfs():
    """Build the gvfs MTP URI from the plugged-in Amazon device (sysfs)."""
    import glob
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        try:
            with open(os.path.join(dev, "idVendor")) as fh:
                if fh.read().strip() != "1949":
                    continue
            def rd(n):
                try:
                    with open(os.path.join(dev, n)) as fh:
                        return fh.read().strip()
                except OSError:
                    return ""
            parts = [rd("manufacturer"), rd("product"), rd("serial")]
        except OSError:
            continue
        host = "_".join(p.replace(" ", "_") for p in parts if p)
        if host:
            return "mtp://" + host + "/"
    return None


def _usb_uri_candidates(host):
    out = ["mtp://" + host + "/"]
    guess = _usb_uri_from_sysfs()
    if guess and guess not in out:
        out.append(guess)
    return out


def _usb_present():
    import glob
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(dev, "idVendor")) as fh:
                if fh.read().strip() == "1949":
                    return True
        except OSError:
            continue
    return False


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def progress(label, done, total):
    if total <= 0:
        return
    pct = done * 100.0 / total
    bar = "#" * int(pct / 4)
    sys.stderr.write(f"\r  {label[:44]:44} [{bar:<25}] {pct:5.1f}%  {human(done)}")
    if done >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def run(cmd, timeout=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


# ------------------------------------------------------------------ USB backend
class UsbTransport:
    name = "usb"

    def __init__(self, mount_wait=20):
        self.wait = mount_wait

    # -- discovery
    def uri(self):
        """gvfs MTP URI: from an existing mount, else derived from the USB device."""
        mounted = _mounted_mtp()
        if mounted:
            return "mtp://" + mounted + "/"
        r = run(["gio", "mount", "-l"])
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if "mtp://" in line:
                    host = line.split("mtp://", 1)[1].split()[0].strip("/")
                    if host.startswith("host="):
                        host = host[5:]
                    return "mtp://" + host + "/"
        return _usb_uri_from_sysfs()

    def host(self):
        u = self.uri()
        return u.split("mtp://", 1)[1].strip("/") if u else None

    def storage(self):
        h = self.host()
        if not h:
            return None
        base = f"/run/user/{UID}/gvfs/mtp:host={h}"
        roots = sorted(os.listdir(base)) if os.path.isdir(base) else []
        if not roots:
            return None
        return "Internal Storage" if "Internal Storage" in roots else roots[0]

    def root(self, mount=True):
        h = self.host()
        if not h:
            return None
        base = f"/run/user/{UID}/gvfs/mtp:host={h}"
        if not os.path.isdir(base):
            if not mount:
                return None
            for candidate in _usb_uri_candidates(h):
                run(["gio", "mount", candidate])
                if os.path.isdir(base):
                    break
            for _ in range(self.wait * 2):
                if os.path.isdir(base):
                    break
                time.sleep(0.5)
        if not os.path.isdir(base):
            return None
        st = self.storage()
        return os.path.join(base, st) if st else None

    def available(self):
        return self.root() is not None

    def local(self, rel):
        r = self.root()
        if r is None:
            raise Fail("no Kindle connected (plug it in, unlock it, accept the prompt)")
        return os.path.join(r, rel.strip("/")) if rel else r

    def gio_uri(self, rel=""):
        h, st = self.host(), self.storage()
        if not h or not st:
            return None
        parts = [p for p in rel.strip("/").split("/") if p]
        return "mtp://" + h + "/" + urllib.parse.quote(st) + (
            "/" + "/".join(urllib.parse.quote(p) for p in parts) if parts else "")

    # -- operations
    def listdir(self, rel):
        p = self.local(rel)
        if not os.path.isdir(p):
            raise Fail(f"not a folder on the device: {rel or '/'}")
        out = []
        for n in sorted(os.listdir(p)):
            full = os.path.join(p, n)
            isdir = os.path.isdir(full)
            size = 0
            try:
                size = 0 if isdir else os.path.getsize(full)
            except OSError:
                pass
            out.append({"name": n, "dir": isdir, "size": size})
        return out

    def get(self, rel, dest, overwrite=False, show=True):
        src = self.local(rel)
        if not os.path.isfile(src):
            raise Fail(f"not a file on the device: {rel}")
        size = os.path.getsize(src)
        if os.path.exists(dest) and not overwrite and os.path.getsize(dest) == size:
            return "skipped"
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".part"
        r = run(["gio", "copy", self.gio_uri(rel), tmp], timeout=None)
        if r.returncode != 0 or not os.path.exists(tmp):
            try:                                     # fallback: read through the mount
                with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
                    done = 0
                    while True:
                        b = fsrc.read(CHUNK)
                        if not b:
                            break
                        fdst.write(b)
                        done += len(b)
                        if show and size > 20 * 1024 * 1024:
                            progress(os.path.basename(dest), done, size)
            except OSError as e:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise Fail(f"read failed for {rel}: {e}")
        if os.path.getsize(tmp) != size:
            os.remove(tmp)
            raise Fail(f"short read for {rel} ({os.path.getsize(tmp)} of {size})")
        os.replace(tmp, dest)
        return "ok"

    def put(self, local_path, remote_dir, name=None, show=True):
        name = name or os.path.basename(local_path)
        size = os.path.getsize(local_path)
        rel = f"{remote_dir.strip('/')}/{name}" if remote_dir.strip("/") else name
        dest_dir = self.local(remote_dir) if remote_dir.strip("/") else self.root()
        if dest_dir and not os.path.isdir(dest_dir):
            run(["gio", "mkdir", "-p", self.gio_uri(remote_dir)])
        for attempt in range(3):
            r = run(["gio", "copy", local_path, self.gio_uri(rel)], timeout=None)
            ok = r.returncode == 0 and os.path.exists(self.local(rel))
            if ok:
                return "ok"
            time.sleep(1.5)
            if attempt == 1 and not self.available():
                self.root()
        raise Fail(f"write failed for {name}: {r.stderr.strip() or 'unknown error'}")

    def remove(self, rel):
        r = run(["gio", "remove", self.gio_uri(rel)])
        if r.returncode != 0:
            raise Fail(f"remove failed for {rel}: {r.stderr.strip()}")
        return "ok"

    def mkdir(self, rel):
        r = run(["gio", "mkdir", "-p", self.gio_uri(rel)])
        if r.returncode != 0 and not os.path.isdir(self.local(rel)):
            raise Fail(f"mkdir failed for {rel}: {r.stderr.strip()}")
        return "ok"

    def exists(self, rel):
        return os.path.exists(self.local(rel))


# ------------------------------------------------------------- KIO fallback
class KioTransport:
    """Fallback when KDE's kiod holds the MTP device and gvfs cannot claim it.

    KIO gives no file sizes, so pulls cannot skip by size and bulk pulls are
    slower (one process per probe).
    """

    name = "kio"

    def __init__(self):
        self.exe = (shutil.which("kioclient5") or shutil.which("kioclient6")
                    or shutil.which("kioclient"))
        self._stor = None

    def available(self):
        return bool(self.exe) and (_mounted_mtp() is not None or _usb_present())

    def _storage(self):
        """Return the KIO path prefix that contains documents/ (e.g. 'Kindle Scribe/Internal Storage')."""
        if self._stor:
            return self._stor
        r = run([self.exe, "ls", "mtp:/"], timeout=90)
        tops = [ln.strip() for ln in r.stdout.splitlines() if ln.strip() and ln.strip() != "."]
        for top in tops:
            r2 = run([self.exe, "ls", f"mtp:/{top}/"], timeout=90)
            kids = [ln.strip() for ln in r2.stdout.splitlines() if ln.strip() and ln.strip() != "."]
            if "documents" in kids:
                self._stor = top
                return self._stor
            if "Internal Storage" in kids:
                self._stor = f"{top}/Internal Storage"
                return self._stor
        self._stor = tops[0] if tops else None
        return self._stor

    def url(self, rel=""):
        st = self._storage()
        if not st:
            raise Fail("KIO: cannot see the Kindle over MTP (unlock it and accept the prompt)")
        parts = [p for p in rel.strip("/").split("/") if p]
        return "mtp:/" + st + ("/" + "/".join(parts) if parts else "")

    def _ls(self, rel, timeout=300):
        """(ok, names). kioclient5 exits 0 even when it cannot enter a folder, so
        an empty listing with stderr output counts as failure."""
        r = run([self.exe, "ls", self.url(rel).rstrip("/") + "/"], timeout=timeout)
        names = [ln.strip() for ln in r.stdout.splitlines() if ln.strip() and ln.strip() != "."]
        ok = r.returncode == 0 and bool(names)
        return ok, names

    def listdir(self, rel):
        ok, names = self._ls(rel)
        if not ok:
            raise Fail(f"not a folder on the device: {rel or '/'}")
        return [{"name": n, "dir": None, "size": 0} for n in sorted(names)]

    def is_dir(self, rel):
        return self._ls(rel, timeout=90)[0]

    def get(self, rel, dest, overwrite=False, show=True):
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        if os.path.exists(dest) and not overwrite:
            return "skipped"
        tmp = dest + ".part"
        r = run([self.exe, "copy", self.url(rel), tmp], timeout=None)
        if r.returncode != 0 or not os.path.exists(tmp):
            raise Fail(f"read failed for {rel}: {r.stderr.strip()}")
        os.replace(tmp, dest)
        return "ok"

    def put(self, local_path, remote_dir, name=None, show=True):
        name = name or os.path.basename(local_path)
        rel = f"{remote_dir.strip('/')}/{name}" if remote_dir.strip("/") else name
        r = run([self.exe, "copy", local_path, self.url(rel)], timeout=None)
        if r.returncode != 0:
            raise Fail(f"write failed for {name}: {r.stderr.strip()}")
        return "ok"

    def remove(self, rel):
        r = run([self.exe, "rm", self.url(rel)], timeout=120)
        if r.returncode != 0:
            raise Fail(f"remove failed for {rel}: {r.stderr.strip()}")
        return "ok"

    def mkdir(self, rel):
        r = run([self.exe, "mkdir", self.url(rel)], timeout=120)
        if r.returncode != 0 and not self.is_dir(rel):
            raise Fail(f"mkdir failed for {rel}: {r.stderr.strip()}")
        return "ok"

    def exists(self, rel):
        rel = rel.strip("/")
        if not rel:
            return self.is_dir("")
        if self.is_dir(rel):
            return True
        parent, _, name = rel.rpartition("/")
        try:
            return any(e["name"] == name for e in self.listdir(parent))
        except Fail:
            return False


# --------------------------------------------------------------- HTTP backend
class RemoteTransport:
    name = "remote"

    def __init__(self, url, token="", timeout=30):
        self.base = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, path, query=None, data=None, stream=False):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        if self.token:
            req.add_header("X-Token", self.token)
        if data is not None:
            req.add_header("Content-Type", "application/octet-stream")
            req.add_header("Content-Length", str(len(data)))
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode(errors="replace")).get("error", "")
            except Exception:
                body = ""
            raise Fail(f"{path}: HTTP {e.code}{' — ' + body if body else ''}")
        except urllib.error.URLError as e:
            raise Fail(f"cannot reach {self.base}: {e.reason}")

    def _json(self, path, query=None, data=None):
        with self._req(path, query, data) as r:
            return json.loads(r.read().decode())

    def available(self):
        try:
            return bool(self._json("/status").get("device"))
        except Fail:
            return False

    def status(self):
        return self._json("/status")

    def listdir(self, rel):
        return self._json("/ls", {"path": rel.strip("/")})

    def get(self, rel, dest, overwrite=False, show=True):
        head = self._json("/stat", {"path": rel.strip("/")})
        size = int(head.get("size") or 0)
        if os.path.exists(dest) and not overwrite and os.path.getsize(dest) == size:
            return "skipped"
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".part"
        with self._req("/pull", {"path": rel.strip("/")}) as r, open(tmp, "wb") as fh:
            done = 0
            while True:
                b = r.read(CHUNK)
                if not b:
                    break
                fh.write(b)
                done += len(b)
                if show and size > 20 * 1024 * 1024:
                    progress(os.path.basename(dest), done, max(size, done))
        if size and os.path.getsize(tmp) != size:
            os.remove(tmp)
            raise Fail(f"short read for {rel}")
        os.replace(tmp, dest)
        return "ok"

    def put(self, local_path, remote_dir, name=None, show=True):
        name = name or os.path.basename(local_path)
        with open(local_path, "rb") as fh:
            data = fh.read()
        res = self._json("/push", {"name": name, "dir": remote_dir.strip("/")}, data)
        if not res.get("pushed"):
            raise Fail(f"remote refused {name}: {res.get('error', res)}")
        return "ok"

    def remove(self, rel):
        self._json("/rm", {"path": rel.strip("/")}, b"")
        return "ok"

    def mkdir(self, rel):
        self._json("/mkdir", {"path": rel.strip("/")}, b"")
        return "ok"

    def exists(self, rel):
        try:
            self._json("/stat", {"path": rel.strip("/")})
            return True
        except Fail:
            return False


# ------------------------------------------------------------------- helpers
def transport(args):
    mode = getattr(args, "transport", "auto")
    if getattr(args, "url", None) or mode == "remote":
        return RemoteTransport(getattr(args, "url", None) or "http://127.0.0.1:8737",
                               getattr(args, "token", "") or "")
    if mode == "kio":
        return KioTransport()
    if mode == "usb":
        return UsbTransport()
    usb = UsbTransport()
    if usb.available():
        return usb
    kio = KioTransport()
    if kio.available():
        print("note: gvfs cannot claim the device (KDE's kiod is holding it) — "
              "using the KIO fallback; close Dolphin and remount for full speed",
              file=sys.stderr)
        return kio
    return usb


def wait_device(t, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if t.available():
            return True
        time.sleep(2)
    return t.available()


def convert_to_pdf(path):
    exe = shutil.which("ebook-convert")
    if not exe:
        raise Fail("need calibre (ebook-convert) to convert documents")
    out = os.path.splitext(path)[0] + ".pdf"
    print(f"  converting {os.path.basename(path)} -> {os.path.basename(out)}", file=sys.stderr)
    r = run([exe, path, out], timeout=600)
    if r.returncode != 0 or not os.path.exists(out):
        raise Fail(f"conversion failed for {os.path.basename(path)}")
    return out


def expand_inputs(items):
    files = []
    for it in items:
        if os.path.isdir(it):
            for root, _, names in os.walk(it):
                for n in sorted(names):
                    if not n.startswith("."):
                        files.append(os.path.join(root, n))
        elif os.path.isfile(it):
            files.append(it)
        else:
            raise Fail(f"no such file or folder: {it}")
    return files


def queue_push(path, dest_dir):
    os.makedirs(QUEUE, exist_ok=True)
    stamp = f"{time.time():.3f}"
    tgt = os.path.join(QUEUE, f"{stamp}__{os.path.basename(path)}")
    shutil.copy2(path, tgt)
    json.dump({"dest": dest_dir, "name": os.path.basename(path)}, open(tgt + ".json", "w"))
    print(f"  queued {os.path.basename(path)} (no device attached)")


def walk_remote(t, rel, match=None, since=None, out=None, include_sdr=False):
    out = out if out is not None else []
    cutoff = time.time() - since * 86400 if since else None
    entries = t.listdir(rel)
    if any(e["dir"] is None for e in entries):
        for e in entries:                     # cheap pass: obvious names need no probe
            ext = os.path.splitext(e["name"])[1].lower()
            if e["dir"] is None and ext in FILE_EXTS:
                e["dir"] = False
            elif e["dir"] is None and ext in DIR_EXTS:
                e["dir"] = True
    unknown = [e for e in entries if e["dir"] is None]
    if unknown and hasattr(t, "is_dir"):
        from concurrent.futures import ThreadPoolExecutor
        def probe(e):
            child = f"{rel.strip('/')}/{e['name']}" if rel.strip("/") else e["name"]
            try:
                return t.is_dir(child)
            except Exception:
                return False
        with ThreadPoolExecutor(max_workers=8) as ex:
            for e, isd in zip(unknown, ex.map(probe, unknown)):
                e["dir"] = isd
    for e in entries:
        child = f"{rel.strip('/')}/{e['name']}" if rel.strip("/") else e["name"]
        if e["dir"]:
            if not include_sdr and os.path.splitext(e["name"])[1].lower() in DIR_EXTS:
                continue
            walk_remote(t, child, match, since, out, include_sdr)
        else:
            if match and not fnmatch.fnmatch(e["name"], match):
                continue
            if cutoff and isinstance(t, UsbTransport):
                try:
                    if os.path.getmtime(t.local(child)) < cutoff:
                        continue
                except OSError:
                    pass
            out.append({"path": child, "name": e["name"], "size": e["size"]})
    return out


# -------------------------------------------------------------------- commands
def cmd_ls(args):
    t = transport(args)
    rel = (args.path or "").strip("/")
    entries = t.listdir(rel)
    if args.json:
        print(json.dumps(entries, indent=1))
        return 0
    for e in entries:
        if args.long:
            print(f"{'dir ' if e['dir'] else 'file'}  {e['size']:>12}  {e['name']}")
        else:
            print(("[dir]  " if e["dir"] else "       ") + e["name"])
    return 0


def cmd_push(args):
    t = transport(args)
    files = expand_inputs(args.files)
    if not files:
        raise Fail("nothing to push")
    if not wait_device(t, 0 if args.no_wait else args.wait):
        if not args.queue:
            raise Fail("no Kindle connected (use --queue to park the files)")
        for f in files:
            queue_push(f, args.dir)
        return 0
    failures = 0
    for f in files:
        try:
            ext = os.path.splitext(f)[1].lower()
            if ext not in NATIVE_EXTS:
                if not args.convert:
                    raise Fail(f"{ext or 'no extension'} is not native to Kindle; use --convert")
                f = convert_to_pdf(f)
            if args.dry_run:
                print(f"  would push {os.path.basename(f)} -> {args.dir}/")
                continue
            t.put(f, args.dir)
            print(f"  ok  {os.path.basename(f)} -> {args.dir}/  ({human(os.path.getsize(f))})")
        except Fail as e:
            failures += 1
            print(f"  FAIL {os.path.basename(f)}: {e}", file=sys.stderr)
    return 1 if failures else 0


def cmd_pull(args):
    t = transport(args)
    if not wait_device(t, args.wait):
        raise Fail("no Kindle connected")
    targets = []
    if args.all:
        for base in args.all:
            targets.extend(walk_remote(t, base.strip("/"), args.match, args.since,
                                       include_sdr=args.include_sdr))
    for p in args.paths:
        if t.exists(p.strip("/")):
            try:
                entries = t.listdir(p.strip("/"))
                targets.extend(walk_remote(t, p.strip("/"), args.match, args.since,
                                           include_sdr=args.include_sdr))
                del entries
            except Fail:
                targets.append({"path": p.strip("/"), "name": os.path.basename(p), "size": 0})
    if not targets:
        raise Fail("nothing to pull (check the path, or use --all documents)")
    os.makedirs(args.to, exist_ok=True)
    ok = skipped = failed = 0
    total_bytes = 0
    for item in targets:
        dest = os.path.join(args.to, item["path"])
        try:
            if args.dry_run:
                print(f"  would pull {item['path']}")
                continue
            r = t.get(item["path"], dest, overwrite=args.overwrite)
            if r == "skipped":
                skipped += 1
            else:
                ok += 1
                total_bytes += os.path.getsize(dest)
            if not args.quiet:
                print(f"  {r:7} {item['path']}")
        except Fail as e:
            failed += 1
            print(f"  FAIL {item['path']}: {e}", file=sys.stderr)
    print(f"pulled {ok}, skipped {skipped}, failed {failed} ({human(total_bytes)})")
    return 1 if failed else 0


def cmd_rm(args):
    t = transport(args)
    rc = 0
    for p in args.paths:
        try:
            t.remove(p.strip("/"))
            print(f"  removed {p}")
        except Fail as e:
            rc = 1
            print(f"  FAIL {p}: {e}", file=sys.stderr)
    return rc


def cmd_mkdir(args):
    t = transport(args)
    t.mkdir(args.path.strip("/"))
    print(f"  created {args.path}")
    return 0


def cmd_info(args):
    t = transport(args)
    payload = t.status() if isinstance(t, RemoteTransport) else {
        "device": t.available(), "root": t.root(mount=False), "transport": "usb", "version": VERSION}
    if args.json:
        print(json.dumps(payload, indent=1))
    else:
        for k, v in payload.items():
            print(f"{k:10} {v}")
    return 0


def cmd_flush(args):
    if not os.path.isdir(QUEUE):
        print("queue empty")
        return 0
    items = sorted(f for f in os.listdir(QUEUE) if not f.endswith(".json"))
    if not items:
        print("queue empty")
        return 0
    t = transport(args)
    if not wait_device(t, args.wait):
        print(f"{len(items)} item(s) queued, no device attached")
        return 1
    rc = 0
    for it in items:
        p = os.path.join(QUEUE, it)
        meta = json.load(open(p + ".json")) if os.path.exists(p + ".json") else {}
        name = meta.get("name") or it.split("__", 1)[-1]
        try:
            t.put(p, meta.get("dest", DEFAULT_DIR), name=name)
            print(f"  sent {name}")
            os.remove(p)
            if os.path.exists(p + ".json"):
                os.remove(p + ".json")
        except Fail as e:
            rc = 1
            print(f"  FAIL {name}: {e}", file=sys.stderr)
    return rc


def cmd_watch(args):
    print(f"watching {args.folder} -> {args.dir}/  (ctrl-c to stop)")
    seen = set()
    while True:
        try:
            for n in sorted(os.listdir(args.folder)):
                p = os.path.join(args.folder, n)
                if not os.path.isfile(p) or n.startswith("."):
                    continue
                key = (p, os.path.getsize(p), os.path.getmtime(p))
                if key in seen:
                    continue
                seen.add(key)
                ns = argparse.Namespace(files=[p], dir=args.dir, convert=args.convert,
                                        wait=args.wait, queue=True, dry_run=False,
                                        url=args.url, token=args.token, no_wait=False)
                try:
                    if cmd_push(ns) == 0 and args.delete_after:
                        done = os.path.join(args.folder, ".sent")
                        os.makedirs(done, exist_ok=True)
                        shutil.move(p, os.path.join(done, n))
                except Fail as e:
                    print(f"  ! {n}: {e}", file=sys.stderr)
        except FileNotFoundError:
            print(f"{args.folder} vanished; waiting", file=sys.stderr)
        time.sleep(args.interval)
        if args.once:
            return 0


def cmd_doctor(args):
    t = transport(args)
    print(f"kindle-push {VERSION}")
    checks = []
    if isinstance(t, RemoteTransport):
        try:
            st = t.status()
            checks.append(("remote reachable", True, t.base))
            checks.append(("device attached", bool(st.get("device")), st.get("root") or "-"))
        except Fail as e:
            checks.append(("remote reachable", False, str(e)))
    elif isinstance(t, KioTransport):
        checks.append(("kio client", bool(t.exe), t.exe or "missing"))
        checks.append(("mtp storage", bool(t._storage()), t._storage() or "not visible"))
        try:
            n = len(t.listdir(DEFAULT_DIR))
            checks.append(("documents/", True, f"{n} entries"))
        except Fail as e:
            checks.append(("documents/", False, str(e)))
        checks.append(("note", True, "gvfs mount is held by KDE; using KIO fallback"))
    else:
        checks.append(("gio present", bool(shutil.which("gio")), shutil.which("gio") or "missing"))
        checks.append(("mtp device", bool(t.uri()), t.uri() or "not in MTP mode"))
        root = t.root()
        checks.append(("mount", bool(root), root or "-"))
        if root:
            try:
                t.mkdir(".__kp_probe")
                t.remove(".__kp_probe")
                writable = True
            except Fail:
                writable = False
            checks.append(("writable", writable, "gio copy ok" if writable else "device read-only/locked"))
            try:
                n = len(t.listdir(DEFAULT_DIR))
                checks.append(("documents/", True, f"{n} entries"))
            except Fail as e:
                checks.append(("documents/", False, str(e)))
        checks.append(("calibre (--convert)", bool(shutil.which("ebook-convert")),
                       shutil.which("ebook-convert") or "optional, not installed"))
    for name, ok, detail in checks:
        print(f"  [{'ok' if ok else '!!'}] {name:22} {detail}")
    return 0 if all(c[1] for c in checks) else 1


# ---------------------------------------------------------------------- serve
def cmd_serve(args):
    os.makedirs(STATE, exist_ok=True)

    def make_handler():
        class H(BaseHTTPRequestHandler):
            server_version = "kindle-push/" + VERSION

            def _auth(self):
                if not args.token:
                    return True
                got = (self.headers.get("X-Token") or
                       (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token") or [""])[0])
                return got == args.token

            def _send(self, code, payload):
                body = json.dumps(payload, indent=1).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _q(self):
                return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

            def _t(self):
                return UsbTransport(mount_wait=2)

            def do_GET(self):
                if not self._auth():
                    return self._send(401, {"error": "bad token"})
                q = self._q()
                path = urllib.parse.urlparse(self.path).path
                t = self._t()
                try:
                    if path == "/status":
                        return self._send(200, {"device": t.available(), "root": t.root(mount=False),
                                                "version": VERSION, "read_only": args.read_only,
                                                "dest": args.dir})
                    if path == "/ls":
                        return self._send(200, t.listdir((q.get("path") or [""])[0]))
                    if path == "/stat":
                        rel = (q.get("path") or [""])[0].strip("/")
                        p = t.local(rel)
                        if not os.path.exists(p):
                            return self._send(404, {"error": "not found"})
                        return self._send(200, {"path": rel, "dir": os.path.isdir(p),
                                                "size": 0 if os.path.isdir(p) else os.path.getsize(p)})
                    if path == "/pull":
                        rel = (q.get("path") or [""])[0].strip("/")
                        p = t.local(rel)
                        if not os.path.isfile(p):
                            return self._send(404, {"error": "not a file"})
                        size = os.path.getsize(p)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(size))
                        self.send_header("X-File-Name", os.path.basename(rel))
                        self.end_headers()
                        with open(p, "rb") as fh:
                            while True:
                                b = fh.read(CHUNK)
                                if not b:
                                    break
                                self.wfile.write(b)
                        return
                    return self._send(404, {"error": "unknown endpoint"})
                except Fail as e:
                    return self._send(500, {"error": str(e)})

            def do_POST(self):
                if not self._auth():
                    return self._send(401, {"error": "bad token"})
                q = self._q()
                path = urllib.parse.urlparse(self.path).path
                t = self._t()
                try:
                    if path == "/push":
                        if args.read_only:
                            return self._send(403, {"error": "server is read-only"})
                        name = os.path.basename((q.get("name") or [""])[0])
                        d = (q.get("dir") or [args.dir])[0]
                        if not name:
                            return self._send(400, {"error": "name= required"})
                        n = int(self.headers.get("Content-Length") or 0)
                        if n <= 0:
                            return self._send(400, {"error": "empty body"})
                        tmp = os.path.join(STATE, f"recv-{int(time.time())}-{name}")
                        with open(tmp, "wb") as fh:
                            left = n
                            while left:
                                b = self.rfile.read(min(CHUNK, left))
                                if not b:
                                    break
                                fh.write(b)
                                left -= len(b)
                        try:
                            t.put(tmp, d, name=name)
                        finally:
                            os.remove(tmp)
                        return self._send(200, {"name": name, "pushed": True})
                    if path == "/rm":
                        if args.read_only:
                            return self._send(403, {"error": "server is read-only"})
                        t.remove((q.get("path") or [""])[0].strip("/"))
                        return self._send(200, {"removed": True})
                    if path == "/mkdir":
                        if args.read_only:
                            return self._send(403, {"error": "server is read-only"})
                        t.mkdir((q.get("path") or [""])[0].strip("/"))
                        return self._send(200, {"created": True})
                    return self._send(404, {"error": "unknown endpoint"})
                except Fail as e:
                    return self._send(500, {"error": str(e)})

            def log_message(self, fmt, *a):
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))
        return H

    srv = ThreadingHTTPServer((args.bind, args.port), make_handler())
    print(f"kindle-push {VERSION} serving on http://{args.bind}:{args.port}")
    print(f"  token: {'set' if args.token else 'NONE (loopback only!)'}   read-only: {args.read_only}")
    print(f"  client: kindle-push --url http://HOST:{args.port}"
          + (" --token ***" if args.token else "") + " ls")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


# ----------------------------------------------------------------------- main
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    for tgt in (common,):
        tgt.add_argument("--url", default=argparse.SUPPRESS,
                         help="remote kindle-push server; omit for USB")
        tgt.add_argument("--token", default=argparse.SUPPRESS, help="token for the remote server")
        tgt.add_argument("--transport", choices=["auto", "usb", "kio", "remote"],
                         default=argparse.SUPPRESS,
                         help="force a backend (default: usb if possible, then kio)")

    ap = argparse.ArgumentParser(prog="kindle-push", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"kindle-push {VERSION}")
    ap.add_argument("--url", default=argparse.SUPPRESS,
                    help="remote kindle-push server; omit for USB")
    ap.add_argument("--token", default=argparse.SUPPRESS, help="token for the remote server")
    ap.add_argument("--transport", choices=["auto", "usb", "kio", "remote"],
                    default=argparse.SUPPRESS,
                    help="force a backend (default: usb if possible, then kio)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("ls", parents=[common], help="list a device folder")
    p.add_argument("path", nargs="?", default="")
    p.add_argument("-l", "--long", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("push", parents=[common], help="send files/folders to the Kindle")
    p.add_argument("files", nargs="+")
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--convert", action="store_true", help="convert non-native docs via calibre")
    p.add_argument("--wait", type=int, default=30)
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--queue", action="store_true", help="park files if no device")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("pull", parents=[common], help="copy files/folders off the Kindle")
    p.add_argument("paths", nargs="*")
    p.add_argument("--all", nargs="+", metavar="DIR", help="recursive pull of whole folders")
    p.add_argument("--to", required=True)
    p.add_argument("--match", help="glob filter, e.g. '*.pdf'")
    p.add_argument("--since", type=int, help="only files modified in the last N days")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--include-sdr", action="store_true",
                   help="also descend into *.sdr sidecar folders (annotation data)")
    p.add_argument("--wait", type=int, default=30)
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("rm", parents=[common], help="delete from the device")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("mkdir", parents=[common], help="create a device folder")
    p.add_argument("path")
    p.set_defaults(func=cmd_mkdir)

    p = sub.add_parser("info", parents=[common], help="device or remote server status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("flush", parents=[common], help="send the offline queue")
    p.add_argument("--wait", type=int, default=10)
    p.set_defaults(func=cmd_flush)

    p = sub.add_parser("watch", parents=[common], help="push files as they appear")
    p.add_argument("folder")
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--convert", action="store_true")
    p.add_argument("--delete-after", action="store_true")
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--wait", type=int, default=60)
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("serve", help="HTTP endpoint for remote push/pull")
    p.add_argument("--port", type=int, default=8737)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--token", default=os.environ.get("KINDLE_PUSH_TOKEN", ""))
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--read-only", action="store_true", help="refuse push/rm/mkdir")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("doctor", parents=[common], help="diagnose the setup")
    p.set_defaults(func=cmd_doctor)

    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    for attr, env, dflt in (("url", "KINDLE_PUSH_URL", None),
                            ("token", "KINDLE_PUSH_TOKEN", ""),
                            ("transport", "KINDLE_PUSH_TRANSPORT", "auto")):
        if not hasattr(a, attr):
            setattr(a, attr, os.environ.get(env, dflt))
    if not getattr(a, "cmd", None):
        ap.print_help()
        return 0
    try:
        return a.func(a)
    except Fail as e:
        print(f"kindle-push: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
