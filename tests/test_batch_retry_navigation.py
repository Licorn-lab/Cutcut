import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from editor import review_target

class NavigationTests(unittest.TestCase):
    def test_middle_item_advances_instead_of_restarting(self):
        self.assertEqual(review_target(['a','b','c','d'],['a','b','d'],'c',True),'d')
    def test_all_filter_also_advances(self):
        self.assertEqual(review_target(['a','b','c'],['a','b','c'],'b',True),'c')
    def test_refresh_and_retry_keep_current_position(self):
        self.assertEqual(review_target(['a','b','c'],['a','b','c'],'b'),'b')
    def test_end_wraps_to_first_remaining_unreviewed(self):
        self.assertEqual(review_target(['a','b','c'],['a','b'],'c',True),'a')
        self.assertEqual(review_target(['a','b','c'],['b'],'c',True),'b')
    def test_no_unreviewed_items_returns_completion(self):
        self.assertIsNone(review_target(['a','b','c'],[],'c',True))
    def test_new_candidate_is_included_and_current_not_repeated(self):
        self.assertEqual(review_target(['a','b'],['c'],'b',True),'c')
        self.assertIsNone(review_target(['a'],['a'],'a',True))
    def test_removed_following_items_are_skipped(self):
        self.assertEqual(review_target(['a','b','c','d'],['a','d'],'b',True),'d')

class RefreshTests(unittest.TestCase):
    def view(self,rows,mode='全部'):
        from editor import ReviewWindow
        from types import SimpleNamespace
        class Value:
            def __init__(self,v):self.v=v
            def get(self):return self.v
            def set(self,v):self.v=v
        class List:
            def delete(self,*args):pass
            def insert(self,*args):pass
            def selection_set(self,i):self.i=i
            def see(self,i):pass
        v=ReviewWindow.__new__(ReviewWindow);v.busy=False;v.visible=list(rows);v.row=rows[-1]
        v.store=SimpleNamespace(entries=lambda:rows);v.filter=Value(mode);v.listbox=List()
        v.discard_selection=lambda:True;v.save=lambda:True;v.controls=lambda:None;v.schedule_render=lambda:None
        v.detail=Value('');v.state=Value('');v.generation=0;v.selected=[]
        v.select=lambda:v.selected.append(v.visible[v.listbox.i]['source'])
        return v
    def test_all_filter_wrap_skips_approved_and_redo(self):
        v=self.view([{'source':k,'status':state} for k,state in [('a','pending'),('b','redo'),('c','approved')]])
        v.refresh(advance=True);self.assertEqual(v.selected,['a'])
    def test_decided_filter_switches_to_pending(self):
        v=self.view([{'source':'a','status':'pending'},{'source':'c','status':'approved'}],'已通过')
        v.refresh(advance=True);self.assertEqual(v.selected,['a']);self.assertEqual(v.filter.get(),'待审核/编辑中/失败')
    def test_completion_when_only_decided_rows_remain(self):
        v=self.view([{'source':'a','status':'redo'},{'source':'c','status':'approved'}])
        v.refresh(advance=True);self.assertFalse(v.selected);self.assertIsNone(v.row);self.assertEqual(v.state.get(),'本批审核完成')
    def test_processing_rows_do_not_trigger_completion(self):
        v=self.view([{'source':'a','status':'processing'},{'source':'c','status':'approved'}])
        v.refresh(advance=True);self.assertNotEqual(v.state.get(),'本批审核完成');self.assertIn('仍在生成',v.detail.get())


if __name__=='__main__':unittest.main()
