from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.font import Font, rgb565
from gateway.textlayout import NativeText, clusters


class TextLayoutTests(unittest.TestCase):
    def test_flags_marks_joiners_and_modifiers_are_atomic(self):
        text = '🇭🇰🇺🇸e\u0301👩🏽\u200d💻1\ufe0f\u20e3'
        self.assertEqual(clusters(text), ['🇭🇰', '🇺🇸', 'e\u0301', '👩🏽\u200d💻', '1\ufe0f\u20e3'])
        self.assertEqual(clusters('🇭🇰🇺'), ['🇭🇰', '🇺'])
        self.assertEqual(''.join(clusters(text)), text)

    def test_fitting_never_splits_an_emoji_cluster(self):
        font = Font.__new__(Font)
        font.width = lambda text, size: len(clusters(text)) * size
        name = '🇭🇰👩🏽\u200d💻 香港 01'
        for limit in range(0, 100):
            fitted = font.fit(name, 10, limit)
            self.assertLessEqual(font.width(fitted, 10), limit)
            if fitted.endswith('…'):
                prefix = clusters(fitted[:-1])
                self.assertEqual(prefix, clusters(name)[:len(prefix)])

    def test_old_firmware_uses_country_codes_without_changing_names(self):
        font = Font.__new__(Font)
        font.native = None
        font.face = None
        font.needs_shaping = lambda text: True
        font.lib = Mock()
        font.lib.FT_Get_Char_Index.side_effect = lambda face, cp: 0 if cp > 0xFFFF else 1
        name = '🇭🇰 香港 🇺🇸 US 01'
        self.assertEqual(font.display_text(name), '[HK] 香港 [US] US 01')
        self.assertEqual(name, '🇭🇰 香港 🇺🇸 US 01')

    def test_native_measure_and_draw_use_same_complete_label(self):
        font = Font.__new__(Font)
        font.native = Mock()
        font.needs_shaping = lambda text: True
        font.native.metrics.return_value = (106, 0, 0, 106, 13, 13, 0)
        font.native.spans.return_value = (106, [(0, 1, 4, 123)])
        name = '🇭🇰 香港 Fusion 01'
        self.assertEqual(font.width(name, 13), 106)
        fb = Mock()
        font.draw(fb, 20, 30, name, 13, 1, 2)
        font.native.metrics.assert_called_once_with(name, 13)
        font.native.spans.assert_called_once_with(name, 13, 1, 2)
        fb.fill_rect.assert_called_once_with(20, 31, 4, 1, 123)

    def test_real_native_raster_bounds_and_rgb565_colors(self):
        try:
            renderer = NativeText('sans-serif')
        except (OSError, AttributeError):
            self.skipTest('Optional system Pango/Cairo not installed')
        try:
            width, runs = renderer.spans('Unicode', 16, rgb565('#FF0000'), 0)
            self.assertGreater(width, 0)
            self.assertTrue(runs)
            self.assertEqual(renderer.metrics('Unicode', 16)[-1], 0)
            self.assertTrue(all(0 <= shade <= 65535 and length > 0 for _, _, length, shade in runs))
            self.assertTrue(any(shade >> 11 > 0 for _, _, _, shade in runs))
            self.assertTrue(all(shade & 0x7FF == 0 for _, _, _, shade in runs))
        finally:
            renderer.close()


if __name__ == '__main__':
    unittest.main()
