"""mocksock.py -- the mock deck's local connection, on every platform.

The mock deck (mockdeck_server.py) and its clients (hid.py stand-in,
mock_install.py, mock_run.py) talk over a local stream socket named by a
PATH. Where Python has Unix sockets (macOS, Linux) that path IS the socket.
Windows Python has no socket.AF_UNIX (cpython issue 77589; GitHub issue #9:
"module 'socket' has no attribute 'AF_UNIX'" while writing the USB-stick
mods file), so there the server listens on 127.0.0.1 at a free port and
writes that port number into the path as a small text file; clients read it.
Loopback only: nothing is reachable from another machine.
"""
import os
import socket

# XDJ_MOCK_TCP=1 forces the Windows path (to test it on a Mac)
UNIX = hasattr(socket, "AF_UNIX") and os.environ.get("XDJ_MOCK_TCP") != "1"


def listen(path, backlog=4):
    """a listening socket for `path` (call before announcing readiness)"""
    try:
        os.unlink(path)
    except OSError:
        pass
    if UNIX:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(s.getsockname()[1]))
        os.replace(tmp, path)                  # a reader never sees half a number
    s.listen(backlog)
    return s


def connect(path):
    """a connected stream socket to the server listening on `path`"""
    if UNIX:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        return s
    with open(path) as f:
        port = int(f.read().strip())
    s = socket.create_connection(("127.0.0.1", port))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # one line per round trip
    return s
