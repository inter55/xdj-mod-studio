"""Start the emulator in the scheduling class the deck needs.

macOS decides which cores a process may use from its scheduling (QoS) class,
and a process started from a background context -- a server thread answering
a request, say -- is given "utility": the system keeps it on the efficiency
cores and takes slices away whenever a foreground app wants one.

The deck has to produce audio on a clock, so a slice taken away is a moment
where nothing is produced; when the thread comes back it races to catch up and
hands the Mac several seconds of audio at once. That is heard as a stutter
followed by a slow-down. The class is a property of the process and survives
exec, so set it here and hand the emulator its argv unchanged.
"""
import ctypes
import ctypes.util
import os
import sys

QOS_CLASS_USER_INTERACTIVE = 0x21


def raise_qos():
    if sys.platform != "darwin":
        return 0
    debug = os.environ.get("XDJ_QOS_DEBUG")
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("System")
                          or "/usr/lib/libSystem.dylib")
        lib.pthread_set_qos_class_self_np.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.pthread_set_qos_class_self_np.restype = ctypes.c_int
        rc = lib.pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)
        if debug:
            lib.pthread_self.restype = ctypes.c_void_p
            cls, rel = ctypes.c_int(), ctypes.c_int()
            lib.pthread_get_qos_class_np(ctypes.c_void_p(lib.pthread_self()),
                                         ctypes.byref(cls), ctypes.byref(rel))
            print("qos rc=%d class=0x%x rel=%d" % (rc, cls.value, rel.value),
                  file=sys.stderr)
        return rc
    except Exception as e:                      # never stop the deck over this
        if debug:
            print("qos failed: %s" % e, file=sys.stderr)
        return -1


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: qemu_launch.py <command> [args...]")
    raise_qos()
    try:
        os.execv(sys.argv[1], sys.argv[1:])     # same pid: the watchdog's
    except OSError as e:                        # kill still lands on QEMU
        sys.exit("qemu_launch: %s: %s" % (sys.argv[1], e))


main()
