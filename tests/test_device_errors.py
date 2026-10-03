"""A device that cuts responses short is reported as "didn't answer", so the tray retries soon."""
import http.client
import logging
import threading
import unittest

import geekmagic_claude as g


class _Truncating:
    """A server that promises a body and hangs up half way, the way the SmallTV does when it's overloaded."""

    def __init__(self):
        import socket
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.address = f"127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.settimeout(2)
            try:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\nonly a bit")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


class DeviceErrorTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)  # keep the app's log lines off the console
        self.addCleanup(logging.disable, logging.NOTSET)
        self.server = _Truncating()
        self.addCleanup(self.server.close)

    def test_truncated_responses_become_oserrors(self):
        for call in (
            lambda: g.show_image(self.server.address, "x.gif"),
            lambda: g.list_images(self.server.address),
            lambda: g.delete_image(self.server.address, "x.gif"),
        ):
            with self.assertRaises(OSError) as ctx:
                call()
            self.assertIn("incomplete or invalid response", str(ctx.exception))

    def test_an_upload_the_device_hangs_up_on_is_an_oserror_too(self):
        # here the server resets the connection (an OSError already) or cuts the reply (wrapped): either way
        with self.assertRaises(OSError):
            g.upload(self.server.address, b"GIF89a", "x.gif", show=False)

    def test_the_underlying_error_really_is_not_an_oserror(self):
        # why the wrapper exists: without it this slips past `except OSError`
        with self.assertRaises(http.client.HTTPException):
            import urllib.request
            urllib.request.urlopen(f"http://{self.server.address}/x", timeout=2).read()

    def test_the_tray_treats_it_as_the_device_being_unreachable(self):
        try:
            import tray
        except Exception as e:
            self.skipTest(f"tray can't be imported here: {e}")
        app = tray.App(self.server.address, 30, "idle")
        self.keep_alive = app  # pystray registers a Windows window class per icon: don't let it be recycled
        outcome = app._deliver("claude", {
            "title": "Claude", "current_pct": 1.0, "current_reset": None, "weekly_pct": 1.0,
            "weekly_reset": None, "now": g.datetime.now().astimezone(),
        })
        self.assertEqual(outcome, "offline")
        self.assertTrue(app.offline)


if __name__ == "__main__":
    unittest.main()
