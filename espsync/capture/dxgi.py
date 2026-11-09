"""Windows DXGI Desktop Duplication capture with HDR support.

Why a custom backend?
  * Latency: AcquireNextFrame wakes us the moment DWM presents a new frame.
  * Bandwidth: the frame never leaves the GPU at full size.  We copy it into a
    mip-mapped texture, let the GPU box-filter it down (GenerateMips) and read
    back only a ~160-320 px wide mip level (a few hundred KB instead of 66 MB
    for a 4K FP16 frame).
  * HDR: with IDXGIOutput5::DuplicateOutput1 we ask for an FP16 scRGB surface.
    The classic 8-bit DuplicateOutput path (used by most capture libraries)
    gives a clipped, washed-out image when Windows HDR is on.  FP16 gives us
    linear light that we tone-map ourselves (see espsync.color).

Everything is driven through raw COM vtables via ctypes, so there is no
dependency beyond the system DLLs (d3d11.dll, dxgi.dll, user32.dll).
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
import uuid
from ctypes import POINTER, byref, c_float, c_int64, c_uint, c_uint32, c_void_p, wintypes

import numpy as np

from .base import TARGET_WIDTH, CaptureBackend, CaptureError, Frame, MonitorInfo, bgra_to_linear

if sys.platform != "win32":  # pragma: no cover - import guard
    raise ImportError("DXGI capture is only available on Windows")

log = logging.getLogger(__name__)

HRESULT = ctypes.c_long

DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ERROR_UNSUPPORTED = 0x887A0004
DXGI_ERROR_NOT_CURRENTLY_AVAILABLE = 0x887A0022
DXGI_ERROR_ACCESS_LOST = 0x887A0026
DXGI_ERROR_WAIT_TIMEOUT = 0x887A0027
DXGI_ERROR_SESSION_DISCONNECTED = 0x887A0028
E_ACCESSDENIED = 0x80070005

DXGI_FORMAT_R16G16B16A16_FLOAT = 10
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 = 12  # HDR10 desktop

D3D11_USAGE_DEFAULT = 0
D3D11_USAGE_STAGING = 3
D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_BIND_RENDER_TARGET = 0x20
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_RESOURCE_MISC_GENERATE_MIPS = 0x1
D3D11_MAP_READ = 1
D3D11_SDK_VERSION = 7
D3D_DRIVER_TYPE_UNKNOWN = 0
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20

DXGI_MODE_ROTATION_ROTATE90 = 2
DXGI_MODE_ROTATION_ROTATE180 = 3
DXGI_MODE_ROTATION_ROTATE270 = 4


def _u32(hr: int) -> int:
    return hr & 0xFFFFFFFF


# --------------------------------------------------------------------------- COM


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IDXGIFactory1 = GUID.parse("770aae78-f26f-4dba-a829-253c83d1b387")
IID_IDXGIOutput1 = GUID.parse("00cddea8-939b-4b83-a340-a685226666cc")
IID_IDXGIOutput5 = GUID.parse("80a07424-ab52-42eb-833c-0c42fd282d98")
IID_IDXGIOutput6 = GUID.parse("068346e8-aaec-4b84-add7-137f513f77a1")
IID_ID3D11Texture2D = GUID.parse("6f15aaf2-d208-4e89-9ab4-489535d34f9c")

_PP = POINTER(c_void_p)


class Com:
    """Minimal COM pointer wrapper calling methods by vtable index."""

    __slots__ = ("ptr", "_fns")

    def __init__(self, ptr: int | None) -> None:
        if not ptr:
            raise ValueError("null COM pointer")
        self.ptr = ptr
        self._fns: dict[int, object] = {}

    def fn(self, index: int, restype, *argtypes):
        f = self._fns.get(index)
        if f is None:
            vtbl = ctypes.cast(self.ptr, POINTER(POINTER(c_void_p))).contents
            f = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])
            self._fns[index] = f
        return f

    def hr(self, index: int, argtypes: tuple, *args) -> int:
        return self.fn(index, HRESULT, *argtypes)(self.ptr, *args)

    def void(self, index: int, argtypes: tuple, *args) -> None:
        self.fn(index, None, *argtypes)(self.ptr, *args)

    def query(self, iid: GUID) -> Com | None:
        out = c_void_p()
        hr = self.hr(0, (POINTER(GUID), _PP), byref(iid), byref(out))
        return Com(out.value) if hr >= 0 and out.value else None

    def release(self) -> None:
        if self.ptr:
            self.fn(2, ctypes.c_ulong)(self.ptr)
            self.ptr = None


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise CaptureError(f"{what} failed: HRESULT 0x{_u32(hr):08X}")


# --------------------------------------------------------------------- structs


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [
        ("DeviceName", wintypes.WCHAR * 32),
        ("DesktopCoordinates", wintypes.RECT),
        ("AttachedToDesktop", wintypes.BOOL),
        ("Rotation", c_uint),
        ("Monitor", wintypes.HMONITOR),
    ]


class DXGI_OUTPUT_DESC1(ctypes.Structure):
    _fields_ = DXGI_OUTPUT_DESC._fields_ + [
        ("BitsPerColor", c_uint),
        ("ColorSpace", c_uint),
        ("RedPrimary", c_float * 2),
        ("GreenPrimary", c_float * 2),
        ("BluePrimary", c_float * 2),
        ("WhitePoint", c_float * 2),
        ("MinLuminance", c_float),
        ("MaxLuminance", c_float),
        ("MaxFullFrameLuminance", c_float),
    ]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", c_uint), ("Quality", c_uint)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", c_uint),
        ("Height", c_uint),
        ("MipLevels", c_uint),
        ("ArraySize", c_uint),
        ("Format", c_uint),
        ("SampleDesc", DXGI_SAMPLE_DESC),
        ("Usage", c_uint),
        ("BindFlags", c_uint),
        ("CPUAccessFlags", c_uint),
        ("MiscFlags", c_uint),
    ]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]


class DXGI_OUTDUPL_POINTER_POSITION(ctypes.Structure):
    _fields_ = [("Position", wintypes.POINT), ("Visible", wintypes.BOOL)]


class DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [
        ("LastPresentTime", c_int64),
        ("LastMouseUpdateTime", c_int64),
        ("AccumulatedFrames", c_uint),
        ("RectsCoalesced", wintypes.BOOL),
        ("ProtectedContentMaskedOut", wintypes.BOOL),
        ("PointerPosition", DXGI_OUTDUPL_POINTER_POSITION),
        ("TotalMetadataBufferSize", c_uint),
        ("PointerShapeBufferSize", c_uint),
    ]


# DisplayConfig structs (for the "SDR content brightness" white level)
class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", c_uint32), ("Denominator", c_uint32)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", c_uint32),
        ("modeInfoIdx", c_uint32),
        ("statusFlags", c_uint32),
    ]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", c_uint32),
        ("modeInfoIdx", c_uint32),
        ("outputTechnology", c_uint32),
        ("rotation", c_uint32),
        ("scaling", c_uint32),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", c_uint32),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", c_uint32),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", c_uint32),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("infoType", c_uint32),
        ("id", c_uint32),
        ("adapterId", LUID),
        ("_union", ctypes.c_uint64 * 6),
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", c_uint32), ("size", c_uint32), ("adapterId", LUID), ("id", c_uint32)]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("viewGdiDeviceName", wintypes.WCHAR * 32)]


class DISPLAYCONFIG_SDR_WHITE_LEVEL(ctypes.Structure):
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("SDRWhiteLevel", c_uint32)]


_d3d11 = ctypes.WinDLL("d3d11")
_dxgi = ctypes.WinDLL("dxgi")
_user32 = ctypes.WinDLL("user32")

_dxgi.CreateDXGIFactory1.argtypes = [POINTER(GUID), _PP]
_dxgi.CreateDXGIFactory1.restype = HRESULT
_d3d11.D3D11CreateDevice.argtypes = [
    c_void_p, c_uint, c_void_p, c_uint, c_void_p, c_uint, c_uint, _PP, POINTER(c_uint), _PP
]
_d3d11.D3D11CreateDevice.restype = HRESULT


_gpu_priority_set = False


def raise_gpu_priority() -> int | None:
    """Ask the GPU scheduler to run our (tiny) copy/mip work ahead of a game.

    Without this, while a game keeps the GPU busy our readback waits behind
    its queued frames: measured 30 ms median / 140 ms p95 on an RTX 3070,
    versus ~10 / 20 ms with REALTIME priority.  OBS does the same.  Returns
    the class that was applied (5 realtime, 4 high) or None.
    """
    global _gpu_priority_set
    if _gpu_priority_set:
        return None
    try:
        gdi32 = ctypes.WinDLL("gdi32")
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = c_void_p
        fn = gdi32.D3DKMTSetProcessSchedulingPriorityClass
        fn.argtypes = [c_void_p, ctypes.c_int]
        fn.restype = ctypes.c_long
    except (OSError, AttributeError):
        return None
    for cls in (5, 4):  # D3DKMT_SCHEDULINGPRIORITYCLASS_REALTIME, _HIGH
        if fn(kernel32.GetCurrentProcess(), cls) == 0:
            _gpu_priority_set = True
            log.info("GPU scheduling priority class set to %s", "realtime" if cls == 5 else "high")
            return cls
    return None


def _ensure_dpi_awareness() -> None:
    # DuplicateOutput1 requires a per-monitor DPI aware process.  Qt already
    # sets this; for the CLI we do it here.  Failure (already set) is harmless.
    try:
        _user32.SetProcessDpiAwarenessContext(c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        pass


def sdr_white_nits(gdi_device_name: str) -> float | None:
    """Windows' "SDR content brightness" for a display, in nits (HDR mode only)."""
    QDC_ONLY_ACTIVE_PATHS = 0x2
    n_paths, n_modes = c_uint32(), c_uint32()
    if _user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, byref(n_paths), byref(n_modes)):
        return None
    paths = (DISPLAYCONFIG_PATH_INFO * n_paths.value)()
    modes = (DISPLAYCONFIG_MODE_INFO * n_modes.value)()
    if _user32.QueryDisplayConfig(
        QDC_ONLY_ACTIVE_PATHS, byref(n_paths), paths, byref(n_modes), modes, None
    ):
        return None
    for path in paths[: n_paths.value]:
        src = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
        src.header.type = 1  # DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
        src.header.size = ctypes.sizeof(src)
        src.header.adapterId = path.sourceInfo.adapterId
        src.header.id = path.sourceInfo.id
        if _user32.DisplayConfigGetDeviceInfo(byref(src)) or src.viewGdiDeviceName != gdi_device_name:
            continue
        white = DISPLAYCONFIG_SDR_WHITE_LEVEL()
        white.header.type = 11  # DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL
        white.header.size = ctypes.sizeof(white)
        white.header.adapterId = path.targetInfo.adapterId
        white.header.id = path.targetInfo.id
        if _user32.DisplayConfigGetDeviceInfo(byref(white)):
            return None
        return white.SDRWhiteLevel / 1000.0 * 80.0
    return None


# ------------------------------------------------------------------ enumeration


class _Output:
    def __init__(self, adapter: Com, output: Com) -> None:
        self.adapter = adapter
        self.output = output
        desc = DXGI_OUTPUT_DESC()
        output.void(7, (POINTER(DXGI_OUTPUT_DESC),), byref(desc))
        self.device_name = desc.DeviceName
        r = desc.DesktopCoordinates
        self.rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)
        self.attached = bool(desc.AttachedToDesktop)
        self.rotation = desc.Rotation
        self.hdr = False
        self.max_nits = 0.0
        self.bits = 8
        o6 = output.query(IID_IDXGIOutput6)
        if o6 is not None:
            d1 = DXGI_OUTPUT_DESC1()
            if o6.hr(27, (POINTER(DXGI_OUTPUT_DESC1),), byref(d1)) >= 0:
                self.hdr = d1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020
                self.max_nits = float(d1.MaxLuminance)
                self.bits = int(d1.BitsPerColor)
            o6.release()

    def release(self) -> None:
        self.output.release()


def _enumerate_outputs() -> tuple[Com, list[_Output]]:
    factory_ptr = c_void_p()
    _check(_dxgi.CreateDXGIFactory1(byref(IID_IDXGIFactory1), byref(factory_ptr)), "CreateDXGIFactory1")
    factory = Com(factory_ptr.value)
    outputs: list[_Output] = []
    i = 0
    while True:
        ap = c_void_p()
        hr = factory.hr(12, (c_uint, _PP), i, byref(ap))  # EnumAdapters1
        if _u32(hr) == DXGI_ERROR_NOT_FOUND or not ap.value:
            break
        adapter = Com(ap.value)
        j = 0
        has_output = False
        while True:
            op = c_void_p()
            hr = adapter.hr(7, (c_uint, _PP), j, byref(op))  # EnumOutputs
            if _u32(hr) == DXGI_ERROR_NOT_FOUND or not op.value:
                break
            out = _Output(adapter, Com(op.value))
            if out.attached:
                outputs.append(out)
                has_output = True
            else:
                out.release()
            j += 1
        if not has_output:
            adapter.release()
        i += 1
    return factory, outputs


def list_monitors() -> list[MonitorInfo]:
    factory, outputs = _enumerate_outputs()
    try:
        return [
            MonitorInfo(
                index=i,
                name=o.device_name.replace("\\\\.\\", ""),
                x=o.rect[0],
                y=o.rect[1],
                width=o.rect[2],
                height=o.rect[3],
                primary=(o.rect[0] == 0 and o.rect[1] == 0),
                hdr=o.hdr,
            )
            for i, o in enumerate(outputs)
        ]
    finally:
        adapters = {id(o.adapter): o.adapter for o in outputs}
        for o in outputs:
            o.release()
        for a in adapters.values():
            a.release()
        factory.release()


# ---------------------------------------------------------------------- backend


class DxgiBackend(CaptureBackend):
    name = "dxgi"

    def __init__(
        self,
        monitor: int = 0,
        hdr_mode: str = "auto",
        sdr_white_override: float = 0.0,
        peak_override: float = 0.0,
        target_width: int = TARGET_WIDTH,
        gpu_priority: bool = True,
    ) -> None:
        super().__init__(monitor)
        _ensure_dpi_awareness()
        if gpu_priority:
            raise_gpu_priority()
        self.hdr_mode = hdr_mode
        self.sdr_white_override = sdr_white_override
        self.peak_override = peak_override
        self.target_width = target_width

        self._factory: Com | None = None
        self._outputs: list[_Output] = []
        self._out: _Output | None = None
        self._device: Com | None = None
        self._ctx: Com | None = None
        self._dupl: Com | None = None
        self._holding = False
        self._mip_tex: Com | None = None
        self._srv: Com | None = None
        self._staging: Com | None = None
        self._tex_key: tuple | None = None
        self._level = 0
        self._small = (0, 0)
        self._format = DXGI_FORMAT_B8G8R8A8_UNORM
        self._white_nits = 80.0
        self._white_checked = 0.0
        self._have_frame = False
        self.hdr_active = False
        self._init()

    # -- setup ---------------------------------------------------------------
    def _init(self) -> None:
        self._factory, self._outputs = _enumerate_outputs()
        if not self._outputs:
            raise CaptureError("no displays attached")
        self._out = self._outputs[min(self.monitor, len(self._outputs) - 1)]

        dev, ctx, level = c_void_p(), c_void_p(), c_uint()
        hr = _d3d11.D3D11CreateDevice(
            self._out.adapter.ptr,
            D3D_DRIVER_TYPE_UNKNOWN,
            None,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            None,
            0,
            D3D11_SDK_VERSION,
            byref(dev),
            byref(level),
            byref(ctx),
        )
        _check(hr, "D3D11CreateDevice")
        self._device = Com(dev.value)
        self._ctx = Com(ctx.value)
        self._duplicate()

    def _duplicate(self) -> None:
        assert self._out is not None and self._device is not None
        self._release_dupl()
        dupl = c_void_p()
        want_fp16 = self.hdr_mode != "off"
        hr = -1
        o5 = self._out.output.query(IID_IDXGIOutput5) if want_fp16 else None
        if o5 is not None:
            formats = (c_uint * 2)(DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_B8G8R8A8_UNORM)
            hr = o5.hr(26, (c_void_p, c_uint, c_uint, POINTER(c_uint), _PP),
                       self._device.ptr, 0, 2, formats, byref(dupl))
            o5.release()
            if hr < 0:
                log.info("DuplicateOutput1 failed (0x%08X); falling back to 8-bit", _u32(hr))
        if hr < 0:
            o1 = self._out.output.query(IID_IDXGIOutput1)
            if o1 is None:
                raise CaptureError("IDXGIOutput1 not supported (Windows 8+ required)")
            hr = o1.hr(22, (c_void_p, _PP), self._device.ptr, byref(dupl))
            o1.release()
        code = _u32(hr)
        if code in (E_ACCESSDENIED, DXGI_ERROR_NOT_CURRENTLY_AVAILABLE, DXGI_ERROR_SESSION_DISCONNECTED):
            raise CaptureError(f"desktop duplication unavailable right now (0x{code:08X})")
        _check(hr, "DuplicateOutput")
        self._dupl = Com(dupl.value)
        # Re-read HDR state: it may have been toggled since we enumerated.
        refreshed = _Output(self._out.adapter, self._out.output)
        self._out.hdr, self._out.max_nits = refreshed.hdr, refreshed.max_nits
        self._out.rotation = refreshed.rotation
        self._white_checked = 0.0
        log.info(
            "DXGI duplication on %s (HDR=%s, peak=%.0f nits)",
            self._out.device_name, self._out.hdr, self._out.max_nits,
        )

    def _make_textures(self, desc: D3D11_TEXTURE2D_DESC) -> None:
        assert self._device is not None
        self._release_textures()
        w, h, fmt = desc.Width, desc.Height, desc.Format
        level = 0
        while (w >> (level + 1)) >= self.target_width and (h >> (level + 1)) >= 8:
            level += 1
        self._level = level
        self._small = (max(1, w >> level), max(1, h >> level))
        self._format = fmt

        if level > 0:
            md = D3D11_TEXTURE2D_DESC(
                Width=w, Height=h, MipLevels=level + 1, ArraySize=1, Format=fmt,
                SampleDesc=DXGI_SAMPLE_DESC(1, 0), Usage=D3D11_USAGE_DEFAULT,
                BindFlags=D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET,
                CPUAccessFlags=0, MiscFlags=D3D11_RESOURCE_MISC_GENERATE_MIPS,
            )
            tex = c_void_p()
            _check(self._device.hr(5, (POINTER(D3D11_TEXTURE2D_DESC), c_void_p, _PP),
                                   byref(md), None, byref(tex)), "CreateTexture2D(mips)")
            self._mip_tex = Com(tex.value)
            srv = c_void_p()
            _check(self._device.hr(7, (c_void_p, c_void_p, _PP), self._mip_tex.ptr, None, byref(srv)),
                   "CreateShaderResourceView")
            self._srv = Com(srv.value)

        sw, sh = self._small
        sd = D3D11_TEXTURE2D_DESC(
            Width=sw, Height=sh, MipLevels=1, ArraySize=1, Format=fmt,
            SampleDesc=DXGI_SAMPLE_DESC(1, 0), Usage=D3D11_USAGE_STAGING,
            BindFlags=0, CPUAccessFlags=D3D11_CPU_ACCESS_READ, MiscFlags=0,
        )
        st = c_void_p()
        _check(self._device.hr(5, (POINTER(D3D11_TEXTURE2D_DESC), c_void_p, _PP),
                               byref(sd), None, byref(st)), "CreateTexture2D(staging)")
        self._staging = Com(st.value)
        self._tex_key = (w, h, fmt)

    # -- capture ---------------------------------------------------------------
    def _refresh_white_level(self) -> None:
        now = time.monotonic()
        if now - self._white_checked < 2.0:
            return
        self._white_checked = now
        if self.sdr_white_override > 0:
            self._white_nits = self.sdr_white_override
            return
        assert self._out is not None
        nits = sdr_white_nits(self._out.device_name) if self._out.hdr else None
        self._white_nits = nits if nits and nits > 0 else 80.0

    def grab(self, timeout_ms: int = 100) -> Frame | None:
        if self._dupl is None:
            self._duplicate()
        assert self._dupl is not None and self._ctx is not None
        if self._holding:
            self._dupl.hr(14, ())  # ReleaseFrame
            self._holding = False

        info = DXGI_OUTDUPL_FRAME_INFO()
        res = c_void_p()
        hr = self._dupl.hr(8, (c_uint, POINTER(DXGI_OUTDUPL_FRAME_INFO), _PP),
                           int(timeout_ms), byref(info), byref(res))
        code = _u32(hr)
        if code == DXGI_ERROR_WAIT_TIMEOUT:
            return None
        if code == DXGI_ERROR_ACCESS_LOST:
            # Mode change, HDR toggle, fullscreen-exclusive switch, UAC prompt...
            log.info("DXGI access lost; re-creating duplication")
            self._release_dupl()
            return None
        _check(hr, "AcquireNextFrame")
        self._holding = True
        acquired = time.perf_counter()
        resource = Com(res.value)
        try:
            if info.LastPresentTime == 0 and self._have_frame:
                return None  # only the mouse moved
            tex = resource.query(IID_ID3D11Texture2D)
            if tex is None:
                raise CaptureError("frame is not a texture")
            try:
                return self._read(tex, acquired)
            finally:
                tex.release()
        finally:
            resource.release()

    def _read(self, frame_tex: Com, acquired: float) -> Frame:
        assert self._ctx is not None and self._out is not None
        desc = D3D11_TEXTURE2D_DESC()
        frame_tex.void(10, (POINTER(D3D11_TEXTURE2D_DESC),), byref(desc))
        if self._tex_key != (desc.Width, desc.Height, desc.Format):
            self._make_textures(desc)
        assert self._staging is not None
        ctx = self._ctx
        copy_args = (c_void_p, c_uint, c_uint, c_uint, c_uint, c_void_p, c_uint, c_void_p)
        if self._level > 0:
            assert self._mip_tex is not None and self._srv is not None
            ctx.void(46, copy_args, self._mip_tex.ptr, 0, 0, 0, 0, frame_tex.ptr, 0, None)
            ctx.void(54, (c_void_p,), self._srv.ptr)  # GenerateMips
            ctx.void(46, copy_args, self._staging.ptr, 0, 0, 0, 0, self._mip_tex.ptr, self._level, None)
        else:
            ctx.void(46, copy_args, self._staging.ptr, 0, 0, 0, 0, frame_tex.ptr, 0, None)

        mapped = D3D11_MAPPED_SUBRESOURCE()
        _check(ctx.hr(14, (c_void_p, c_uint, c_uint, c_uint, POINTER(D3D11_MAPPED_SUBRESOURCE)),
                      self._staging.ptr, 0, D3D11_MAP_READ, 0, byref(mapped)), "Map")
        try:
            sw, sh = self._small
            pitch = mapped.RowPitch
            raw = np.ctypeslib.as_array(
                ctypes.cast(mapped.pData, POINTER(ctypes.c_ubyte)), shape=(sh, pitch)
            )
            if self._format == DXGI_FORMAT_R16G16B16A16_FLOAT:
                px = raw.view(np.float16).reshape(sh, pitch // 8, 4)[:, :sw, :3]
                image = px.astype(np.float32)  # copies before Unmap
                is_fp16 = True
            else:
                image = bgra_to_linear(raw.reshape(sh, pitch // 4, 4)[:, :sw])
                is_fp16 = False
        finally:
            ctx.void(15, (c_void_p, c_uint), self._staging.ptr, 0)  # Unmap

        rot = self._out.rotation
        if rot == DXGI_MODE_ROTATION_ROTATE90:
            image = np.rot90(image, -1)
        elif rot == DXGI_MODE_ROTATION_ROTATE180:
            image = np.rot90(image, 2)
        elif rot == DXGI_MODE_ROTATION_ROTATE270:
            image = np.rot90(image, 1)

        hdr = False
        peak = 1.0
        if is_fp16:
            # scRGB: 1.0 == 80 nits.  Normalise so SDR white == 1.0.
            hdr = self._out.hdr if self.hdr_mode == "auto" else self.hdr_mode == "on"
            if hdr:
                self._refresh_white_level()
                scale = 80.0 / self._white_nits
                image *= scale
                peak_nits = self.peak_override or self._out.max_nits or 1000.0
                peak = max(1.0, peak_nits / self._white_nits)
            np.maximum(image, 0.0, out=image)  # drop out-of-gamut negatives
        self.hdr_active = hdr
        self._have_frame = True
        w, h = self._out.rect[2], self._out.rect[3]
        return Frame(
            image=np.ascontiguousarray(image), source_size=(w, h), hdr=hdr, peak=peak, timestamp=acquired
        )

    # -- teardown ---------------------------------------------------------------
    @property
    def white_nits(self) -> float:
        return self._white_nits

    def _release_textures(self) -> None:
        for name in ("_srv", "_mip_tex", "_staging"):
            obj = getattr(self, name)
            if obj is not None:
                obj.release()
                setattr(self, name, None)
        self._tex_key = None

    def _release_dupl(self) -> None:
        if self._dupl is not None:
            if self._holding:
                self._dupl.hr(14, ())
                self._holding = False
            self._dupl.release()
            self._dupl = None

    def close(self) -> None:
        self._release_dupl()
        self._release_textures()
        for name in ("_ctx", "_device"):
            obj = getattr(self, name)
            if obj is not None:
                obj.release()
                setattr(self, name, None)
        adapters = {id(o.adapter): o.adapter for o in self._outputs}
        for o in self._outputs:
            o.release()
        for a in adapters.values():
            a.release()
        self._outputs = []
        if self._factory is not None:
            self._factory.release()
            self._factory = None
