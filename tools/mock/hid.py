"""Stand-in for the `hid` package: talks to mockdeck_server.py instead of
USB. Put tools/mock first on PYTHONPATH and set XDJ_MOCK_SOCK."""
import os

import mocksock  # noqa: E402 (tools/mock is on the path with this file)

PIONEER_VID = 0x2B73
_SOCK = os.environ.get("XDJ_MOCK_SOCK", "/tmp/xdj_mockdeck.sock")


def enumerate(vid=0, pid=0):
    return [{"vendor_id": PIONEER_VID, "product_id": 0x0008, "usage": 0x01,
             "usage_page": 0xFF00, "path": b"mock", "product_string": "XDJ-700 (mock)",
             "serial_number": "MOCK0001"}]


class Device:
    def __init__(self, vid=None, pid=None, path=None):
        self.s = mocksock.connect(_SOCK)
        self.f = self.s.makefile("rwb")
        self.buf = bytearray()
        self.nonblocking = True
        self.manufacturer, self.product, self.serial = "Pioneer DJ", "XDJ-700 (mock)", "MOCK0001"

    def get_report_descriptor(self):
        return b""

    def write(self, data):
        cmd = bytes(data).split(b"\r")[0].lstrip(b"\0")
        if os.environ.get("MOCK_REALTIME") == "1":   # real-deck pace (measured)
            import time
            time.sleep(0.066 if cmd.upper().endswith((b"BR", b"LR")) else 0.004)
        self.f.write(cmd + b"\n")
        self.f.flush()
        self.buf += bytes.fromhex(self.f.readline().decode().strip())
        return len(data)

    def read(self, n, timeout=None):
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def close(self):
        try:
            self.f.close()        # the makefile keeps the fd alive otherwise
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass
