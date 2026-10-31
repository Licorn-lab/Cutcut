"""Regression checks for the desktop mask editor's supported selection tools."""
import unittest
from PIL import Image
from editor import MaskEditor


class MaskEditorTests(unittest.TestCase):
    def test_erase_restore_and_undo_redo(self):
        editor = MaskEditor(Image.new("L", (20, 20), 128))
        self.assertTrue(editor.apply("rectangle", [(2, 2), (8, 8)], restore=False))
        self.assertEqual(editor.mask.getpixel((5, 5)), 0)
        self.assertEqual(editor.mask.getpixel((15, 15)), 128)
        self.assertTrue(editor.undo_one())
        self.assertEqual(editor.mask.getpixel((5, 5)), 128)
        self.assertTrue(editor.redo_one())
        self.assertTrue(editor.apply("brush", [(5, 5)], restore=True, radius=1))
        self.assertEqual(editor.mask.getpixel((5, 5)), 255)

    def test_empty_and_unchanged_selection_do_not_create_history(self):
        editor = MaskEditor(Image.new("L", (20, 20), 0))
        self.assertFalse(editor.apply("rectangle", [], restore=True))
        self.assertFalse(editor.apply("rectangle", [(1, 1), (5, 5)], restore=False))
        self.assertFalse(editor.undo)

    def test_unsupported_selection_is_rejected(self):
        editor = MaskEditor(Image.new("L", (20, 20), 0))
        with self.assertRaises(ValueError):
            editor.apply("wand", [(1, 1)])


if __name__ == "__main__":
    unittest.main()
