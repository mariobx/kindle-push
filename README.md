# kindle-push

Move documents **to and from** a Kindle over USB (MTP) or the network — no
Send-to-Kindle, no Amazon account, no cloud round-trip. One Python file, stdlib
only, no root.

```
kindle-push push paper.pdf            # -> documents/ on the device
kindle-push pull --all documents --to ~/backup
kindle-push serve --port 8737 --token S3CRET    # then push from any machine
```

## Why

Amazon's own path for getting a file onto a Kindle is "Send to Kindle": upload to
Amazon, wait, let Amazon convert and email/deliver it back. This does the direct
thing — write the file into the device's `documents/` folder like a USB stick,
from a shell, over USB or over your network.

## Install

Any of these:

```bash
pipx install git+https://github.com/mariobx/kindle-push      # recommended
pip install --user git+https://github.com/mariobx/kindle-push
# or just copy the script
install -Dm755 kindle_push.py ~/.local/bin/kindle-push
```

Requires Python 3.8+. Optional: `calibre` for `--convert` (docx/odt/rtf/html → PDF).

## Transports

| transport | when | notes |
|---|---|---|
| `usb` | Kindle plugged in | gvfs MTP mount; needs `gio` (glib2) |
| `kio` | USB, but KDE's `kiod` holds the device | automatic fallback, uses `kioclient5`; no sizes, slower |
| `remote` | Kindle is plugged into another machine that runs `serve` | `--url` + `--token` |

`--transport auto|usb|kio|remote` forces one; `KINDLE_PUSH_URL`,
`KINDLE_PUSH_TOKEN`, `KINDLE_PUSH_TRANSPORT` do the same via environment.

## Commands

```
ls [PATH] [-l] [--json]                 list a device folder
push FILE|DIR... [--dir D]              send files; folders recurse
     [--convert] [--dry-run] [--wait N] [--queue]
pull [PATH...] --to DIR                 copy off the device
     [--all DIR...] [--match GLOB] [--since DAYS]
     [--overwrite] [--include-sdr] [--dry-run] [--quiet]
rm PATH...                              delete on the device
mkdir PATH                              create a folder
info [--json]                           device/server status
watch DIR [--dir D] [--once]            push files as they appear
serve [--port] [--bind] [--token]       HTTP endpoint (--read-only to forbid writes)
flush                                   send the offline queue
doctor                                  diagnose: gio, mount, writability, calibre
```

Exit codes: `0` success, `1` partial failure, `2` usage or environment problem.

### Examples

```bash
# bulk push a folder of papers into a sub-folder
kindle-push push ~/papers/ --dir documents/Papers

# everything off the device, resumable and idempotent (skips unchanged files)
kindle-push pull --all documents --to ~/kindle-backup

# only recent PDFs
kindle-push pull --all documents --to ~/b --match '*.pdf' --since 30

# keep a drop folder mirrored onto the device
kindle-push watch ~/inbox --dir documents --delete-after
```

## Remote use

On the machine the Kindle is plugged into:

```bash
KINDLE_PUSH_TOKEN=$(head -c 24 /dev/urandom | base64) kindle-push serve --port 8737
```

From anywhere that can reach it (VPN/Tailscale/ssh tunnel — do not expose it to the
open internet; there is no TLS in the built-in server):

```bash
export KINDLE_PUSH_URL=http://box:8737 KINDLE_PUSH_TOKEN=...
kindle-push push thesis.pdf
kindle-push pull --all documents --to ./backup
```

`serve` also speaks plain HTTP if you'd rather use curl:

```bash
curl -H "X-Token: $T" --data-binary @book.pdf "$URL/push?name=book.pdf&dir=documents"
curl -H "X-Token: $T" -o book.pdf "$URL/pull?path=documents/book.pdf"
curl -H "X-Token: $T" "$URL/ls?path=documents"
```

Run it as a user service with `contrib/kindle-push.service`.

## Gotchas this tool already handles

- **MTP writes cannot go through the gvfs FUSE mount.** Creating a file there fails
  with `Operation not supported` even though `mkdir` works; writes must go through
  `gio copy`. This bit hard enough to be the reason the tool exists.
- **KDE's `kiod` claims the MTP device** as soon as a file manager looks at it, after
  which gvfs cannot open it. The `kio` transport is the fallback; closing Dolphin and
  re-running gets the fast path back.
- **Kindles drop MTP when they sleep**, which kills a long transfer mid-file. Retries
  and remounts are built in, and `pull` skips files that already match.
- **The library only rescans at the end of the USB session**, so a pushed file shows up
  after you unplug (or toggle "connect to computer").
- Filenames are sanitised for MTP, and pulls verify byte counts before `rename(2)`ing
  the `.part` file into place.

## What it does not do

- No DRM removal, no KFX conversion, no Amazon APIs. It moves bytes.
- No TLS in `serve` — tunnel it, and always use `--token` off loopback.

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

MIT. See LICENSE.
