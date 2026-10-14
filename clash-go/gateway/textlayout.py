"""Optional system Pango/Cairo renderer for shaped Unicode and color emoji.

Plain CJK/ASCII stays on the small FreeType path. No font files are installed
or downloaded; font selection uses the device's existing fontconfig setup.
"""
import ctypes as C
import ctypes.util
from functools import lru_cache
import sys
import unicodedata


def clusters(text):
    """Keep flags, combining marks and emoji joiner sequences intact when fitting."""
    result = []
    i = 0
    while i < len(text):
        start = i
        first = ord(text[i])
        i += 1
        if 0x1F1E6 <= first <= 0x1F1FF and i < len(text) and 0x1F1E6 <= ord(text[i]) <= 0x1F1FF:
            i += 1
        while i < len(text):
            code = ord(text[i])
            if unicodedata.category(text[i]).startswith('M') or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F:
                i += 1
            elif text[i] == '\u200d' and i + 1 < len(text):
                i += 2
            else:
                break
        result.append(text[start:i])
    return result


class Rect(C.Structure):
    _fields_ = [(name, C.c_int) for name in ('x', 'y', 'width', 'height')]


class NativeText:
    def __init__(self, family):
        self.family = family
        self.cairo = C.CDLL(ctypes.util.find_library('cairo') or 'libcairo.so.2')
        self.pango = C.CDLL(ctypes.util.find_library('pango-1.0') or 'libpango-1.0.so.0')
        self.pc = C.CDLL(ctypes.util.find_library('pangocairo-1.0') or 'libpangocairo-1.0.so.0')
        self.obj = C.CDLL(ctypes.util.find_library('gobject-2.0') or 'libgobject-2.0.so.0')
        P, I = C.c_void_p, C.c_int
        bindings = [
            (self.cairo, 'cairo_image_surface_create', P, [I, I, I]),
            (self.cairo, 'cairo_create', P, [P]),
            (self.cairo, 'cairo_destroy', None, [P]),
            (self.cairo, 'cairo_surface_destroy', None, [P]),
            (self.cairo, 'cairo_surface_flush', None, [P]),
            (self.cairo, 'cairo_surface_status', I, [P]),
            (self.cairo, 'cairo_image_surface_get_stride', I, [P]),
            (self.cairo, 'cairo_image_surface_get_data', P, [P]),
            (self.cairo, 'cairo_set_source_rgb', None, [P, C.c_double, C.c_double, C.c_double]),
            (self.cairo, 'cairo_paint', None, [P]),
            (self.cairo, 'cairo_move_to', None, [P, C.c_double, C.c_double]),
            (self.pc, 'pango_cairo_create_layout', P, [P]),
            (self.pc, 'pango_cairo_show_layout', None, [P, P]),
            (self.pango, 'pango_font_description_from_string', P, [C.c_char_p]),
            (self.pango, 'pango_font_description_set_absolute_size', None, [P, C.c_double]),
            (self.pango, 'pango_font_description_free', None, [P]),
            (self.pango, 'pango_layout_set_font_description', None, [P, P]),
            (self.pango, 'pango_layout_set_text', None, [P, C.c_char_p, I]),
            (self.pango, 'pango_layout_set_single_paragraph_mode', None, [P, I]),
            (self.pango, 'pango_layout_get_pixel_extents', None, [P, C.POINTER(Rect), C.POINTER(Rect)]),
            (self.pango, 'pango_layout_get_baseline', I, [P]),
            (self.pango, 'pango_layout_get_unknown_glyphs_count', I, [P]),
            (self.obj, 'g_object_unref', None, [P]),
        ]
        for lib, name, result, args in bindings:
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = result, args

    def layout(self, cr, text, size):
        layout = self.pc.pango_cairo_create_layout(cr)
        desc = self.pango.pango_font_description_from_string(self.family.encode())
        try:
            self.pango.pango_font_description_set_absolute_size(desc, size * 1024)
            self.pango.pango_layout_set_font_description(layout, desc)
            self.pango.pango_layout_set_single_paragraph_mode(layout, 1)
            raw = text.encode('utf-8')
            self.pango.pango_layout_set_text(layout, raw, len(raw))
        finally:
            self.pango.pango_font_description_free(desc)
        return layout

    @lru_cache(maxsize=256)
    def metrics(self, text, size):
        surface = self.cairo.cairo_image_surface_create(0, 1, 1)
        cr = self.cairo.cairo_create(surface)
        layout = self.layout(cr, text, size)
        try:
            ink, logical = Rect(), Rect()
            self.pango.pango_layout_get_pixel_extents(layout, C.byref(ink), C.byref(logical))
            return (logical.width, ink.x, ink.y, ink.width, ink.height,
                    self.pango.pango_layout_get_baseline(layout) / 1024,
                    self.pango.pango_layout_get_unknown_glyphs_count(layout))
        finally:
            self.obj.g_object_unref(layout)
            self.cairo.cairo_destroy(cr)
            self.cairo.cairo_surface_destroy(surface)

    def source(self, cr, color):
        self.cairo.cairo_set_source_rgb(cr, (color >> 11) / 31, ((color >> 5) & 63) / 63, (color & 31) / 31)

    @lru_cache(maxsize=96)
    def spans(self, text, size, color, background):
        advance, x, y, width, height, baseline, _ = self.metrics(text, size)
        if not width or not height:
            return advance, []
        # Screen labels must be fitted before drawing. Bound raster allocation.
        width, height = min(width, 2048), min(height, 256)
        surface = self.cairo.cairo_image_surface_create(0, width, height)
        cr = self.cairo.cairo_create(surface)
        layout = self.layout(cr, text, size)
        try:
            if self.cairo.cairo_surface_status(surface):
                raise RuntimeError('无法绘制 Unicode 字体')
            self.source(cr, background)
            self.cairo.cairo_paint(cr)
            self.source(cr, color)
            self.cairo.cairo_move_to(cr, -x, -y)
            self.pc.pango_cairo_show_layout(cr, layout)
            self.cairo.cairo_surface_flush(surface)
            stride = self.cairo.cairo_image_surface_get_stride(surface)
            raw = C.string_at(self.cairo.cairo_image_surface_get_data(surface), stride * height)
            runs = []
            for row in range(height):
                start, previous = 0, None
                for col in range(width + 1):
                    shade = None
                    if col < width:
                        i = row * stride + col * 4
                        pixel = int.from_bytes(raw[i:i + 4], sys.byteorder)
                        shade = ((pixel >> 19) & 31) << 11 | ((pixel >> 10) & 63) << 5 | ((pixel >> 3) & 31)
                    if shade != previous:
                        if previous is not None and previous != background:
                            runs.append((x + start, round(size - baseline + y) + row, col - start, previous))
                        start, previous = col, shade
            return advance, runs
        finally:
            self.obj.g_object_unref(layout)
            self.cairo.cairo_destroy(cr)
            self.cairo.cairo_surface_destroy(surface)

    def close(self):
        self.metrics.cache_clear()
        self.spans.cache_clear()
