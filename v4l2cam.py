"""Minimal V4L2 webcam access with zero dependencies (ioctl + mmap).

Only what an ambient-light meter needs: open the device, pick a small YUYV
mode, lock exposure/gain, grab a few frames and return their luma plane.
"""

import ctypes
import errno
import fcntl
import mmap
import os
import select
import struct

# --- ioctl encoding (asm-generic) -------------------------------------------

_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("V") << 8) | nr


def _iowr(nr, size):
    return _ioc(_IOC_READ | _IOC_WRITE, nr, size)


# --- structs -----------------------------------------------------------------

class Capability(ctypes.Structure):
    _fields_ = [("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32),
                ("bus_info", ctypes.c_char * 32), ("version", ctypes.c_uint32),
                ("capabilities", ctypes.c_uint32), ("device_caps", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 3)]


class FmtDesc(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("description", ctypes.c_char * 32),
                ("pixelformat", ctypes.c_uint32), ("mbus_code", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 3)]


class FrmSizeEnum(ctypes.Structure):
    # union of discrete {w,h} / stepwise {min_w,max_w,step_w,min_h,max_h,step_h}
    _fields_ = [("index", ctypes.c_uint32), ("pixel_format", ctypes.c_uint32),
                ("type", ctypes.c_uint32), ("u", ctypes.c_uint32 * 6),
                ("reserved", ctypes.c_uint32 * 2)]


class PixFormat(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
                ("pixelformat", ctypes.c_uint32), ("field", ctypes.c_uint32),
                ("bytesperline", ctypes.c_uint32), ("sizeimage", ctypes.c_uint32),
                ("colorspace", ctypes.c_uint32), ("priv", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("ycbcr_enc", ctypes.c_uint32),
                ("quantization", ctypes.c_uint32), ("xfer_func", ctypes.c_uint32)]


class _FmtUnion(ctypes.Union):
    # The real union holds pointers (v4l2_window), hence 8-byte alignment.
    _fields_ = [("pix", PixFormat), ("raw", ctypes.c_uint8 * 200), ("_align", ctypes.c_uint64)]


class Format(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("fmt", _FmtUnion)]


class RequestBuffers(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("memory", ctypes.c_uint32), ("capabilities", ctypes.c_uint32),
                ("flags", ctypes.c_uint8), ("reserved", ctypes.c_uint8 * 3)]


class Timeval(ctypes.Structure):
    _fields_ = [("sec", ctypes.c_long), ("usec", ctypes.c_long)]


class Timecode(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("frames", ctypes.c_uint8), ("seconds", ctypes.c_uint8),
                ("minutes", ctypes.c_uint8), ("hours", ctypes.c_uint8),
                ("userbits", ctypes.c_uint8 * 4)]


class _BufM(ctypes.Union):
    _fields_ = [("offset", ctypes.c_uint32), ("userptr", ctypes.c_ulong),
                ("planes", ctypes.c_void_p), ("fd", ctypes.c_int32)]


class Buffer(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("bytesused", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("field", ctypes.c_uint32), ("timestamp", Timeval),
                ("timecode", Timecode), ("sequence", ctypes.c_uint32),
                ("memory", ctypes.c_uint32), ("m", _BufM),
                ("length", ctypes.c_uint32), ("reserved2", ctypes.c_uint32),
                ("request_fd", ctypes.c_int32)]


class Control(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


class QueryCtrl(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("name", ctypes.c_char * 32), ("minimum", ctypes.c_int32),
                ("maximum", ctypes.c_int32), ("step", ctypes.c_int32),
                ("default_value", ctypes.c_int32), ("flags", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32 * 2)]


VIDIOC_QUERYCAP = _ioc(_IOC_READ, 0, ctypes.sizeof(Capability))
VIDIOC_ENUM_FMT = _iowr(2, ctypes.sizeof(FmtDesc))
VIDIOC_S_FMT = _iowr(5, ctypes.sizeof(Format))
VIDIOC_REQBUFS = _iowr(8, ctypes.sizeof(RequestBuffers))
VIDIOC_QUERYBUF = _iowr(9, ctypes.sizeof(Buffer))
VIDIOC_QBUF = _iowr(15, ctypes.sizeof(Buffer))
VIDIOC_DQBUF = _iowr(17, ctypes.sizeof(Buffer))
VIDIOC_STREAMON = _ioc(_IOC_WRITE, 18, ctypes.sizeof(ctypes.c_int))
VIDIOC_STREAMOFF = _ioc(_IOC_WRITE, 19, ctypes.sizeof(ctypes.c_int))
VIDIOC_G_CTRL = _iowr(27, ctypes.sizeof(Control))
VIDIOC_S_CTRL = _iowr(28, ctypes.sizeof(Control))
VIDIOC_QUERYCTRL = _iowr(36, ctypes.sizeof(QueryCtrl))
VIDIOC_ENUM_FRAMESIZES = _iowr(74, ctypes.sizeof(FrmSizeEnum))

BUF_TYPE_VIDEO_CAPTURE = 1
MEMORY_MMAP = 1
CAP_VIDEO_CAPTURE = 0x1
CAP_STREAMING = 0x04000000
FRMSIZE_TYPE_DISCRETE = 1
CTRL_FLAG_DISABLED = 0x1
CTRL_FLAG_NEXT_CTRL = 0x80000000


def fourcc(s):
    return struct.unpack("<I", s.encode())[0]


def fourcc_str(v):
    return struct.pack("<I", v).decode(errors="replace")


YUYV = fourcc("YUYV")

# Control ids (linux/v4l2-controls.h)
CID_BRIGHTNESS = 0x00980900
CID_CONTRAST = 0x00980901
CID_AUTO_WHITE_BALANCE = 0x0098090C
CID_GAIN = 0x00980913
CID_BACKLIGHT_COMPENSATION = 0x0098091C
CID_EXPOSURE_AUTO = 0x009A0901          # menu: 1 = manual, 3 = aperture priority (auto)
CID_EXPOSURE_ABSOLUTE = 0x009A0902      # units of 100 µs
CID_EXPOSURE_AUTO_PRIORITY = 0x009A0903
EXPOSURE_MANUAL = 1


class CameraBusy(Exception):
    """Another application holds the camera."""


class Camera:
    def __init__(self, path):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise CameraBusy(path) from e
            raise
        self._maps = []
        self._saved_ctrls = {}
        self.width = self.height = self.bytesperline = 0

    # -- info --
    def capability(self):
        cap = Capability()
        fcntl.ioctl(self.fd, VIDIOC_QUERYCAP, cap)
        return cap

    def is_capture_device(self):
        cap = self.capability()
        caps = cap.device_caps or cap.capabilities
        return bool(caps & CAP_VIDEO_CAPTURE) and bool(caps & CAP_STREAMING)

    def formats(self):
        out, i = [], 0
        while True:
            d = FmtDesc(index=i, type=BUF_TYPE_VIDEO_CAPTURE)
            try:
                fcntl.ioctl(self.fd, VIDIOC_ENUM_FMT, d)
            except OSError:
                return out
            out.append((fourcc_str(d.pixelformat), d.description.decode()))
            i += 1

    def frame_sizes(self, pixfmt=YUYV):
        out, i = [], 0
        while True:
            e = FrmSizeEnum(index=i, pixel_format=pixfmt)
            try:
                fcntl.ioctl(self.fd, VIDIOC_ENUM_FRAMESIZES, e)
            except OSError:
                return out
            if e.type != FRMSIZE_TYPE_DISCRETE:
                return [(e.u[0], e.u[3])]  # stepwise: smallest
            out.append((e.u[0], e.u[1]))
            i += 1

    def controls(self):
        """Yield (id, QueryCtrl) for every enabled control."""
        qid = CTRL_FLAG_NEXT_CTRL
        while True:
            q = QueryCtrl(id=qid)
            try:
                fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, q)
            except OSError:
                return
            if not q.flags & CTRL_FLAG_DISABLED:
                yield q.id, q
            qid = q.id | CTRL_FLAG_NEXT_CTRL

    def query(self, cid):
        q = QueryCtrl(id=cid)
        try:
            fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, q)
        except OSError:
            return None
        return None if q.flags & CTRL_FLAG_DISABLED else q

    # -- controls --
    def get(self, cid):
        c = Control(id=cid)
        fcntl.ioctl(self.fd, VIDIOC_G_CTRL, c)
        return c.value

    def set(self, cid, value, remember=True):
        """Set a control; returns False if the camera doesn't support it."""
        try:
            if remember and cid not in self._saved_ctrls:
                self._saved_ctrls[cid] = self.get(cid)
            fcntl.ioctl(self.fd, VIDIOC_S_CTRL, Control(id=cid, value=value))
            return True
        except OSError:
            return False

    def restore_controls(self):
        # Exposure mode last: setting absolute exposure while in auto mode fails.
        order = sorted(self._saved_ctrls, key=lambda c: c == CID_EXPOSURE_AUTO)
        for cid in order:
            try:
                fcntl.ioctl(self.fd, VIDIOC_S_CTRL, Control(id=cid, value=self._saved_ctrls[cid]))
            except OSError:
                pass
        self._saved_ctrls.clear()

    # -- streaming --
    def configure(self, width, height, nbuf=2):
        f = Format(type=BUF_TYPE_VIDEO_CAPTURE)
        f.fmt.pix.width, f.fmt.pix.height = width, height
        f.fmt.pix.pixelformat = YUYV
        f.fmt.pix.field = 1  # V4L2_FIELD_NONE
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, f)
        if f.fmt.pix.pixelformat != YUYV:
            raise RuntimeError("camera does not support YUYV")
        self.width, self.height = f.fmt.pix.width, f.fmt.pix.height
        self.bytesperline = f.fmt.pix.bytesperline or self.width * 2

        req = RequestBuffers(count=nbuf, type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
        fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)
        for i in range(req.count):
            b = Buffer(index=i, type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
            fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, b)
            self._maps.append(mmap.mmap(self.fd, b.length, mmap.MAP_SHARED,
                                        mmap.PROT_READ | mmap.PROT_WRITE, offset=b.m.offset))
            fcntl.ioctl(self.fd, VIDIOC_QBUF, b)

    def start(self):
        fcntl.ioctl(self.fd, VIDIOC_STREAMON, ctypes.c_int(BUF_TYPE_VIDEO_CAPTURE))

    def stop(self):
        try:
            fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, ctypes.c_int(BUF_TYPE_VIDEO_CAPTURE))
        except OSError:
            pass

    def grab(self, timeout=3.0):
        """Return one frame's luma plane (bytes, width*height)."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            raise TimeoutError("no frame from camera")
        b = Buffer(type=BUF_TYPE_VIDEO_CAPTURE, memory=MEMORY_MMAP)
        fcntl.ioctl(self.fd, VIDIOC_DQBUF, b)
        try:
            raw = self._maps[b.index][:b.bytesused]
        finally:
            fcntl.ioctl(self.fd, VIDIOC_QBUF, b)
        if self.bytesperline == self.width * 2:
            return raw[0::2]  # YUYV: every even byte is Y
        rows = (raw[y * self.bytesperline:y * self.bytesperline + self.width * 2]
                for y in range(self.height))
        return b"".join(r[0::2] for r in rows)

    def close(self):
        self.stop()
        self.restore_controls()
        for m in self._maps:
            m.close()
        self._maps.clear()
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def find_camera():
    """First /dev/videoN that is an actual capture device (skips metadata nodes)."""
    for name in sorted(os.listdir("/dev")):
        if not name.startswith("video"):
            continue
        path = "/dev/" + name
        try:
            with Camera(path) as cam:
                if cam.is_capture_device() and any(f == "YUYV" for f, _ in cam.formats()):
                    return path
        except (OSError, CameraBusy):
            continue
    return None
