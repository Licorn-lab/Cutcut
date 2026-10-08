import sys,tempfile,unittest
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import load_image

class MpoTests(unittest.TestCase):
    def test_mpo_primary_dimensions_and_original_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'photo.JPG'
            Image.new('RGB',(60,40),'red').save(p,format='MPO',save_all=True,append_images=[Image.new('RGB',(30,20),'blue')])
            before=p.read_bytes();im=load_image(p)
            self.assertEqual(im.size,(60,40));self.assertGreater(im.getpixel((10,10))[0],240)
            self.assertEqual(p.read_bytes(),before)
    def test_multipage_tiff_rejected_even_named_jpg(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'photo.JPG'
            Image.new('RGB',(20,20)).save(p,format='TIFF',save_all=True,append_images=[Image.new('RGB',(20,20),'red')])
            with self.assertRaisesRegex(ValueError,'多页'):load_image(p)
    def test_animated_gif_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'animated.gif'
            Image.new('RGB',(20,20)).save(p,save_all=True,append_images=[Image.new('RGB',(20,20),'red')])
            with self.assertRaisesRegex(ValueError,'动画'):load_image(p)

if __name__=='__main__':unittest.main()
