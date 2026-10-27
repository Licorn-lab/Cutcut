"""Geometry regression tests use small images only, without segmentation models."""
import sys
import unittest
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_geometry import apply_geometry, geometry_size, validate_geometry, on_white


class GeometryTests(unittest.TestCase):
    def image(self):
        im=Image.new('RGBA',(80,60),(0,0,0,0));draw=ImageDraw.Draw(im)
        draw.rectangle((5,5,30,25), fill='red');draw.rectangle((45,35,70,55),fill='blue')
        return im

    def test_flip_and_clockwise_rotation_preserve_pixels(self):
        image=self.image();before=image.tobytes()
        result=apply_geometry(image,{'turns':1,'flip':True})
        expected=image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).transpose(Image.Transpose.ROTATE_270)
        self.assertEqual(result.size,(60,80));self.assertEqual(result.tobytes(),expected.tobytes())
        self.assertEqual(image.tobytes(),before)

    def test_straightening_retains_canvas_and_uses_white_fill(self):
        image=self.image()
        for turns in range(4):
            for angle in [-45,-5,.1,5,45]:
                params={'turns':turns,'angle':angle}
                result=apply_geometry(image,params)
                self.assertEqual(result.size,geometry_size(image.size,params))
                self.assertEqual(on_white(result).getpixel((0,0)),(255,255,255))
                self.assertTrue(result.getchannel('A').getbbox())

    def test_perspective_is_bounded_and_changes_pixels(self):
        image=self.image();original=np.array(image)
        for h,v in [(100,100),(-100,-100),(80,-80),(30,0),(0,30)]:
            result=apply_geometry(image,{'horizontal':h,'vertical':v})
            self.assertEqual(result.size,image.size)
            self.assertTrue(result.getchannel('A').getbbox())
            self.assertFalse(np.array_equal(original,np.array(result)))
        self.assertEqual(apply_geometry(image,None).tobytes(),image.tobytes())

    def test_rejects_invalid_and_nonfinite_settings(self):
        for value in [[],{'angle':float('nan')},{'angle':float('inf')},{'angle':46},
                      {'vertical':101},{'turns':-1},{'turns':True},{'flip':1},{'unknown':0}]:
            with self.assertRaises(ValueError):validate_geometry(value)


if __name__=='__main__':unittest.main()
