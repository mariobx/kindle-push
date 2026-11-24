"""Smoke tests for kindle-push.

These deliberately avoid needing a Kindle attached: they exercise argument
parsing, the pure helpers, and the failure paths. Transport tests that need a
device live in the README checklist.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kindle_push as kp  # noqa: E402


def cli(*args, timeout=60):
    return subprocess.run([sys.executable, kp.__file__] + list(args),
                          capture_output=True, text=True, timeout=timeout)


class TestCli(unittest.TestCase):
    def test_version(self):
        r = cli("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn(kp.VERSION, r.stdout)

    def test_help_lists_commands(self):
        r = cli("--help")
        self.assertEqual(r.returncode, 0)
        for cmd in ("push", "pull", "ls", "rm", "mkdir", "serve", "watch",
                    "flush", "doctor", "info"):
            self.assertIn(cmd, r.stdout)

    def test_no_command_prints_help(self):
        r = cli()
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage:", r.stdout)

    def test_bad_option_is_usage_error(self):
        r = cli("push", "--nope")
        self.assertEqual(r.returncode, 2)

    def test_transport_flag_accepted_before_and_after_command(self):
        self.assertEqual(cli("--transport", "kio", "--help").returncode, 0)
        self.assertEqual(cli("ls", "--help").returncode, 0)

    def test_pull_requires_to(self):
        r = cli("pull", "documents/x.pdf")
        self.assertEqual(r.returncode, 2)


class TestHelpers(unittest.TestCase):
    def test_human(self):
        self.assertEqual(kp.human(0), "0 B")
        self.assertEqual(kp.human(1023), "1023 B")
        self.assertEqual(kp.human(1024), "1.0 KiB")
        self.assertEqual(kp.human(5 * 1024 * 1024), "5.0 MiB")

    def test_expand_inputs_recurses_and_skips_dotfiles(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sub"))
            for name in ("a.pdf", "sub/b.epub", ".hidden"):
                open(os.path.join(d, name), "w").close()
            got = sorted(os.path.basename(p) for p in kp.expand_inputs([d]))
            self.assertEqual(got, ["a.pdf", "b.epub"])

    def test_expand_inputs_rejects_missing(self):
        with self.assertRaises(kp.Fail):
            kp.expand_inputs(["/nonexistent/file.pdf"])

    def test_dir_exts_are_never_files(self):
        self.assertIn(".sdr", kp.DIR_EXTS)
        self.assertNotIn(".sdr", kp.FILE_EXTS)

    def test_file_exts_cover_documents(self):
        for ext in (".pdf", ".epub", ".kfx", ".azw3"):
            self.assertIn(ext, kp.FILE_EXTS)


class TestRemoteTransport(unittest.TestCase):
    """The HTTP client against a throwaway server, no Kindle involved."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        payload = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/status"):
                    body = json.dumps({"device": True, "read_only": False}).encode()
                elif self.path.startswith("/ls"):
                    body = json.dumps([{"name": "a.pdf", "dir": False, "size": 3}]).encode()
                elif self.path.startswith("/stat"):
                    body = json.dumps({"size": 3, "dir": False}).encode()
                elif self.path.startswith("/pull"):
                    body = b"abc"
                else:
                    body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                payload["body"] = self.rfile.read(n)
                payload["path"] = self.path
                body = json.dumps({"pushed": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.payload = payload
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_listdir_and_status(self):
        t = kp.RemoteTransport(f"http://127.0.0.1:{self.port}")
        self.assertTrue(t.available())
        self.assertEqual(t.listdir("documents")[0]["name"], "a.pdf")

    def test_get_writes_file(self):
        t = kp.RemoteTransport(f"http://127.0.0.1:{self.port}")
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "a.pdf")
            self.assertEqual(t.get("documents/a.pdf", dest), "ok")
            with open(dest, "rb") as fh:
                self.assertEqual(fh.read(), b"abc")

    def test_put_posts_body(self):
        t = kp.RemoteTransport(f"http://127.0.0.1:{self.port}")
        with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as fh:
            fh.write(b"xyz")
            name = fh.name
        try:
            self.assertEqual(t.put(name, "documents"), "ok")
            self.assertEqual(self.payload["body"], b"xyz")
            self.assertIn("name=", self.payload["path"])
        finally:
            os.unlink(name)


if __name__ == "__main__":
    unittest.main()
