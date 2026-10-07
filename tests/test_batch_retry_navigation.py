import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Options,digest
from workflow import BatchStore,run_batch,retry_unapproved
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

class BulkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.source=self.root/'source';self.source.mkdir();self.output=self.root/'output'
        self.env=patch.dict(os.environ,{'WHITEBG_DATA_HOME':str(self.root/'cache')});self.env.start()
        for name in ['a.png','b.png','c.png']:
            im=Image.new('RGB',(100,80),'white');ImageDraw.Draw(im).rectangle((25,15,75,65),fill='red');im.save(self.source/name)
        run_batch(self.source,self.output,Options(model='u2net'),session_factory=lambda m:object(),remover=self.remove)
        self.store=BatchStore(self.output)
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def remove(self,image,session,options):
        mask=Image.new('L',image.size,0);ImageDraw.Draw(mask).rectangle((25,15,75,65),fill=255)
        result=image.copy();result.putalpha(mask);return result
    def approve(self,key='a.png'):
        row=self.store.get(key);return self.store.decide(key,row['revision'],'approved','')
    def retry(self,**kw):return retry_unapproved(self.output,'isnet-general-use',session_factory=kw.pop('session_factory',lambda m:object()),remover=kw.pop('remover',self.remove),**kw)
    def test_approved_record_and_export_unchanged_others_reprocessed(self):
        approved=self.approve();before=digest(self.output/approved['export'])
        row=self.store.get('b.png');redo=self.store.decide('b.png',row['revision'],'redo','需修整');old=self.output/redo['export']
        sessions=[];result=self.retry(session_factory=lambda m:sessions.append(m) or object())
        self.assertEqual(result['success'],2);self.assertEqual(result['skipped_approved'],1);self.assertEqual(sessions,['isnet-general-use'])
        self.assertEqual(self.store.get('a.png'),approved);self.assertEqual(digest(self.output/approved['export']),before)
        self.assertFalse(old.exists())
        for key in ['b.png','c.png']:
            row=self.store.get(key);self.assertEqual(row['status'],'pending');self.assertEqual(row['options']['model'],'isnet-general-use');self.assertEqual(row['human'],'')
        row=self.store.get('b.png');export=self.store.decide('b.png',row['revision'],'approved','')['export']
        self.assertTrue((self.output/export).exists());self.assertEqual(Path(export).name,'bw.jpg')
    def test_stop_preserves_unprocessed_rows(self):
        before=self.store.get('b.png');cancel=threading.Event()
        def remove(*args):cancel.set();return self.remove(*args)
        stats=self.retry(cancel=cancel,remover=remove)
        self.assertTrue(stats['cancelled']);self.assertEqual(stats['success'],1);self.assertEqual(self.store.get('b.png'),before)
    def test_model_preparation_failure_preserves_all_edits(self):
        before=self.store.entries()
        with self.assertRaises(RuntimeError):self.retry(session_factory=lambda m:(_ for _ in ()).throw(RuntimeError('download error')))
        self.assertEqual(self.store.entries(),before)
    def test_approval_during_model_preparation_is_protected(self):
        saved=[]
        def factory(model):saved.append(self.approve());return object()
        result=self.retry(session_factory=factory)
        self.assertEqual(self.store.get('a.png'),saved[0]);self.assertEqual(result['skipped_approved'],1)
    def test_targets_do_not_include_new_source_photos(self):
        Image.new('RGB',(10,10),'white').save(self.source/'new.png')
        result=self.retry(targets=['b.png'])
        self.assertEqual(result['total'],1);self.assertIsNone(self.store.get('new.png'));self.assertEqual(self.store.get('a.png')['options']['model'],'u2net')
    def test_active_batch_lock_rejects_retry(self):
        import fcntl
        before=self.store.entries()
        with (self.store.cache/'batch.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'仍在生成'):self.retry()
        self.assertEqual(self.store.entries(),before)
    def test_external_redo_modification_is_protected(self):
        row=self.store.get('b.png');row=self.store.decide('b.png',row['revision'],'redo','')
        path=self.output/row['export'];path.write_bytes(b'externally edited')
        result=self.retry(targets=['b.png']);self.assertEqual(result['failed'],1)
        self.assertEqual(path.read_bytes(),b'externally edited');self.assertEqual(self.store.get('b.png'),row)
    def test_later_resume_keeps_bulk_selected_model(self):
        self.retry()
        def unexpected(*args):raise AssertionError('should reuse candidates')
        stats=run_batch(self.source,self.output,Options(model='u2net'),session_factory=unexpected,remover=unexpected)
        self.assertEqual(stats['skipped'],3);self.assertEqual(self.store.get('b.png')['options']['model'],'isnet-general-use')

if __name__=='__main__':unittest.main()
