"""Small FreeType binding for the CJK font already installed on NanoKVM.

No Pillow, NumPy, font downloads or private SDK APIs are required on device.
"""
import ctypes as C
import ctypes.util
from functools import lru_cache
import os
import unicodedata

from .textlayout import NativeText, clusters

L = C.c_long
P = C.c_void_p


class Generic(C.Structure):
    _fields_ = [("data", P), ("finalizer", P)]


class Vector(C.Structure):
    _fields_ = [("x", L), ("y", L)]


class BBox(C.Structure):
    _fields_ = [("xMin", L), ("yMin", L), ("xMax", L), ("yMax", L)]


class Bitmap(C.Structure):
    _fields_ = [("rows", C.c_uint), ("width", C.c_uint), ("pitch", C.c_int),
                ("buffer", C.POINTER(C.c_ubyte)), ("num_grays", C.c_ushort),
                ("pixel_mode", C.c_ubyte), ("palette_mode", C.c_ubyte), ("palette", P)]


class Metrics(C.Structure):
    _fields_ = [(name, L) for name in ("width", "height", "horiBearingX", "horiBearingY", "horiAdvance", "vertBearingX", "vertBearingY", "vertAdvance")]


class Glyph(C.Structure):
    _fields_ = [("library", P), ("face", P), ("next", P), ("glyph_index", C.c_uint),
                ("generic", Generic), ("metrics", Metrics), ("linearHoriAdvance", L),
                ("linearVertAdvance", L), ("advance", Vector), ("format", C.c_uint),
                ("bitmap", Bitmap), ("bitmap_left", C.c_int), ("bitmap_top", C.c_int)]


class Face(C.Structure):
    _fields_ = [(name, L) for name in ("num_faces", "face_index", "face_flags", "style_flags", "num_glyphs")] + [
        ("family_name", C.c_char_p), ("style_name", C.c_char_p), ("num_fixed_sizes", C.c_int),
        ("available_sizes", P), ("num_charmaps", C.c_int), ("charmaps", P), ("generic", Generic),
        ("bbox", BBox), ("units_per_EM", C.c_ushort), ("ascender", C.c_short),
        ("descender", C.c_short), ("height", C.c_short), ("max_advance_width", C.c_short),
        ("max_advance_height", C.c_short), ("underline_position", C.c_short),
        ("underline_thickness", C.c_short), ("glyph", C.POINTER(Glyph))]


def rgb565(value):
    value = value.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def blend565(foreground, background, alpha, steps=7):
    channels = []
    for shift, mask in ((11, 31), (5, 63), (0, 31)):
        fg, bg = (foreground >> shift) & mask, (background >> shift) & mask
        channels.append(((fg * alpha + bg * (steps - alpha) + steps // 2) // steps) << shift)
    return channels[0] | channels[1] | channels[2]


class Font:
    def __init__(self, path=None):
        self.lib = C.CDLL(ctypes.util.find_library("freetype") or "libfreetype.so.6")
        self.handle = P()
        self.face = C.POINTER(Face)()
        self.lib.FT_Init_FreeType.argtypes = [C.POINTER(P)]
        self.lib.FT_New_Face.argtypes = [P, C.c_char_p, L, C.POINTER(C.POINTER(Face))]
        self.lib.FT_Set_Pixel_Sizes.argtypes = [C.POINTER(Face), C.c_uint, C.c_uint]
        self.lib.FT_Load_Char.argtypes = [C.POINTER(Face), C.c_ulong, C.c_int32]
        self.lib.FT_Get_Char_Index.argtypes = [C.POINTER(Face), C.c_ulong]
        self.lib.FT_Get_Char_Index.restype = C.c_uint
        self.lib.FT_Done_Face.argtypes = [C.POINTER(Face)]
        self.lib.FT_Done_FreeType.argtypes = [P]
        path = path or os.environ.get("DIARY_FONT", "/usr/share/fonts/SourceHanSansSC-Regular.otf")
        if self.lib.FT_Init_FreeType(C.byref(self.handle)):
            raise RuntimeError("无法初始化字体")
        if self.lib.FT_New_Face(self.handle, os.fsencode(path), 0, C.byref(self.face)):
            self.lib.FT_Done_FreeType(self.handle)
            raise RuntimeError("缺少系统中文字体")
        try:
            self.native = NativeText(self.face.contents.family_name.decode('utf-8'))
        except (OSError, AttributeError):
            self.native = None

    @lru_cache(maxsize=256)
    def needs_shaping(self, text):
        return any(not self.lib.FT_Get_Char_Index(self.face, ord(c)) or
                   unicodedata.category(c).startswith('M') or
                   unicodedata.bidirectional(c) in ('R', 'AL') for c in text)

    def display_text(self, text):
        if self.native or not self.needs_shaping(text):
            return text
        # Older firmware without Pango still gets readable country codes.
        parts = []
        for unit in clusters(text):
            if len(unit) == 2 and all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in unit):
                parts.append('[' + ''.join(chr(ord(c) - 0x1F1E6 + 65) for c in unit) + ']')
            else:
                parts.append(''.join(c if self.lib.FT_Get_Char_Index(self.face, ord(c)) else '?' for c in unit))
        return ''.join(parts)

    @lru_cache(maxsize=512)
    def glyph(self, character, size):
        if self.lib.FT_Set_Pixel_Sizes(self.face, 0, size) or self.lib.FT_Load_Char(self.face, ord(character), 4):
            return size, 0, 0, 0, 0, b""
        slot = self.face.contents.glyph.contents
        bitmap = slot.bitmap
        width, height, pitch = bitmap.width, bitmap.rows, bitmap.pitch
        raw = C.string_at(bitmap.buffer, abs(pitch) * height) if bitmap.buffer else b""
        rows = []
        for y in range(height):
            offset = (y if pitch >= 0 else height - y - 1) * abs(pitch)
            if bitmap.pixel_mode == 1:
                rows.append(bytes(255 if raw[offset + x // 8] & (128 >> (x % 8)) else 0 for x in range(width)))
            elif bitmap.pixel_mode == 2:
                rows.append(raw[offset:offset + width])
            else:
                rows.append(bytes(width))
        return max(1, slot.advance.x >> 6), slot.bitmap_left, slot.bitmap_top, width, height, b"".join(rows)

    def width(self, text, size):
        text = self.display_text(text)
        if self.native and self.needs_shaping(text):
            return self.native.metrics(text, size)[0]
        return sum(self.glyph(char, size)[0] for char in text)

    def fit(self, text, size, max_width):
        if self.width(text, size) <= max_width:
            return text
        parts = clusters(text)
        while parts and self.width(''.join(parts) + "…", size) > max_width:
            parts.pop()
        return ''.join(parts) + "…" if parts else ""

    @lru_cache(maxsize=768)
    def spans(self, char, size, color, background):
        advance, left, top, width, height, data = self.glyph(char, size)
        colors = [blend565(color, background, alpha) for alpha in range(8)]
        runs = []
        for row in range(height):
            start, previous = 0, 0
            for col in range(width + 1):
                alpha = min(7, (data[row * width + col] * 7 + 127) // 255) if col < width else 0
                if alpha != previous:
                    if previous:
                        runs.append((left + start, size - top + row, col - start, colors[previous]))
                    start, previous = col, alpha
        return advance, runs

    def draw(self, fb, x, y, text, size, color, background):
        text = self.display_text(text)
        if self.native and self.needs_shaping(text):
            _, runs = self.native.spans(text, size, color, background)
            for dx, dy, width, shade in runs:
                fb.fill_rect(int(x + dx), int(y + dy), width, 1, shade)
            return
        # Cached, quantized coverage gives smooth CJK without per-frame blending.
        for char in text:
            advance, runs = self.spans(char, size, color, background)
            for dx, dy, width, shade in runs:
                fb.fill_rect(int(x + dx), int(y + dy), width, 1, shade)
            x += advance

    def close(self):
        if self.native:
            self.native.close()
        self.needs_shaping.cache_clear()
        self.glyph.cache_clear()
        self.spans.cache_clear()
        self.lib.FT_Done_Face(self.face)
        self.lib.FT_Done_FreeType(self.handle)
