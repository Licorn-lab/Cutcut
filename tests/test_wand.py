import sys,unittest
from pathlib import Path
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from wand import magic_wand,combine_selection,refine_selection
from editor import MaskEditor

class WandTests(unittest.TestCase):
    def setUp(self):
        a=np.zeros((5,7,3),np.uint8);a[:,:3]=[100,100,100];a[:,4:]=[100,100,100]
        self.image=Image.fromarray(a)
    def test_contiguous_excludes_disconnected_same_colour(self):
        s=magic_wand(self.image,(0,0),0,True)
        self.assertEqual(np.count_nonzero(s),15);self.assertEqual(s.getpixel((6,0)),0)
    def test_global_includes_disconnected_colour(self):
        self.assertEqual(np.count_nonzero(magic_wand(self.image,(0,0),0,False)),30)
    def test_fixed_seed_prevents_gradient_leak(self):
        im=Image.fromarray(np.repeat(np.array([[0,10,20,30]],np.uint8)[:,:,None],3,2))
        self.assertEqual(np.count_nonzero(magic_wand(im,(0,0),10)),2)
    def test_tolerance_and_boundary(self):
        self.assertEqual(np.count_nonzero(magic_wand(self.image,(-1,0))),0)
        self.assertEqual(np.count_nonzero(magic_wand(self.image,(0,0),255)),35)
        with self.assertRaises(ValueError):magic_wand(self.image,(0,0),256)
    def test_add_subtract_replace(self):
        a=magic_wand(self.image,(0,0),0);b=magic_wand(self.image,(6,0),0)
        both=combine_selection(a,b,'add');self.assertEqual(np.count_nonzero(both),30)
        self.assertEqual(np.count_nonzero(combine_selection(both,a,'subtract')),15)
        self.assertEqual(np.count_nonzero(combine_selection(None,a,'subtract')),0)
        self.assertEqual(combine_selection(a,b).tobytes(),b.tobytes())
    def test_refinement(self):
        s=Image.new('L',(15,15));s.paste(255,(5,5,10,10))
        self.assertGreater(np.count_nonzero(refine_selection(s,2)),25)
        self.assertLess(np.count_nonzero(refine_selection(s,-1)),25)
        self.assertTrue(any(0<x<255 for x in refine_selection(s,0,1).getdata()))
    def test_restore_remove_history_original_preserved(self):
        original=self.image.tobytes();s=magic_wand(self.image,(0,0),0)
        e=MaskEditor(Image.new('L',self.image.size,128));before=e.mask.tobytes()
        self.assertTrue(e.apply_mask(s,False));self.assertEqual(e.mask.getpixel((0,0)),0)
        self.assertEqual(e.mask.getpixel((6,0)),128)
        self.assertTrue(e.undo_one());self.assertEqual(e.mask.tobytes(),before)
        self.assertTrue(e.redo_one());self.assertTrue(e.apply_mask(s,True));self.assertEqual(e.mask.getpixel((0,0)),255)
        self.assertEqual(self.image.tobytes(),original)
    def test_soft_selection_blends_mask(self):
        e=MaskEditor(Image.new('L',(2,2),0));e.apply_mask(Image.new('L',(2,2),128),True)
        self.assertEqual(e.mask.getpixel((0,0)),128)
    def test_empty_and_no_change_have_no_history(self):
        e=MaskEditor(Image.new('L',(2,2),0))
        self.assertFalse(e.apply_mask(Image.new('L',(2,2)),True))
        self.assertFalse(e.apply_mask(Image.new('L',(2,2),255),False));self.assertFalse(e.undo)
    def test_sizes_rejected(self):
        with self.assertRaises(ValueError):MaskEditor(Image.new('L',(2,2))).apply_mask(Image.new('L',(3,3)))

if __name__=='__main__':unittest.main()
