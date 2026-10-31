import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Options,digest,inspect_cutout,render_white
from workflow import BatchStore,run_batch,safe_path
from editor import MaskEditor,viewport_image

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'原图';self.source.mkdir();self.output=self.root/'输出'
        self.env=patch.dict(os.environ,{'WHITEBG_DATA_HOME':str(self.root/'internal')});self.env.start();self.calls=0
        self.photo('子目录/样图.png')
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def photo(self,name,color='red'):
        path=self.source/name;path.parent.mkdir(parents=True,exist_ok=True)
        im=Image.new('RGB',(120,80),'#ddd');ImageDraw.Draw(im).rectangle((20,10,99,69),fill=color);im.save(path);return path
    def remover(self,original,session,options):
        self.calls+=1;mask=Image.new('L',original.size,0);ImageDraw.Draw(mask).rectangle((20,10,99,69),fill=255)
        result=original.copy();result.putalpha(mask);return result
    def job(self,**kwargs):
        return run_batch(self.source,self.output,kwargs.pop('options',Options()),session_factory=lambda m:object(),remover=self.remover,**kwargs)
    def row(self):return BatchStore(self.output).get('子目录/样图.png')
    def approve(self):
        store=BatchStore(self.output);row=self.row();return store.decide(row['source'],row['revision'],'approved','确认无问题')
    def test_redo_reason_is_optional(self):
        self.job();store=BatchStore(self.output);row=self.row()
        row=store.decide(row['source'],row['revision'],'redo','')
        self.assertEqual(row['status'],'redo');self.assertTrue((self.output/row['export']).exists())
        self.assertEqual(Path(row['export']).name,'样图.jpg')
        self.assertFalse(list(self.output.rglob('*.csv')))
    def test_single_model_override_survives_resume_and_source_change(self):
        self.job();store=BatchStore(self.output);row=self.row();base=Options()
        selected=Options(model='birefnet-general')
        row=store.prepare(row['source'],row['source_hash'],selected,row['revision'],base.signature())
        mask=Image.new('L',(120,80),0);ImageDraw.Draw(mask).rectangle((20,10,99,69),fill=255)
        store.finish(row,mask,[])
        before=self.calls;self.job();self.assertEqual(self.calls,before)
        self.assertEqual(self.row()['options']['model'],'birefnet-general')
        self.photo('子目录/样图.png','blue');seen=[]
        run_batch(self.source,self.output,base,session_factory=lambda m:seen.append(m) or object(),remover=self.remover)
        self.assertEqual(seen,['birefnet-general'])
        self.job(options=Options(model='u2netp'));self.assertEqual(self.row()['options']['model'],'u2netp')
        self.assertFalse(self.row().get('override_base'))
    def test_export_name_keeps_input_stem(self):
        self.photo('照片 1479.jpg');self.photo('DSC_6952.png');self.job();store=BatchStore(self.output)
        for key,expected in [('照片 1479.jpg','照片 1479.jpg'),('DSC_6952.png','DSC_6952.jpg')]:
            row=store.get(key);row=store.decide(key,row['revision'],'approved','')
            self.assertEqual(Path(row['export']).name,expected)
            row=store.decide(key,row['revision'],'redo','');self.assertEqual(Path(row['export']).name,expected)
    def test_same_stem_different_extensions_get_unique_names(self):
        self.photo('同名.jpg');self.photo('同名.png');self.job();store=BatchStore(self.output)
        exported=[]
        for key in ['同名.jpg','同名.png']:
            row=store.get(key);row=store.decide(key,row['revision'],'approved','');exported.append(row['export'])
            self.assertEqual(store.decide(key,row['revision'],'approved','')['export'],row['export'])
        self.assertEqual({Path(x).name for x in exported},{'同名.jpg','同名_2.jpg'})
    def test_same_stem_in_different_folders_is_allowed(self):
        self.photo('甲/a.jpg');self.photo('乙/a.png');self.job();store=BatchStore(self.output)
        for key in ['甲/a.jpg','乙/a.png']:
            row=store.get(key);row=store.decide(key,row['revision'],'approved','')
            self.assertEqual(Path(row['export']).name,'a.jpg');self.assertTrue((self.output/row['export']).exists())
    def test_existing_owned_old_name_changes_on_reconfirmation(self):
        self.job();store=BatchStore(self.output);row=self.approve();target=self.output/row['export']
        old=target.with_name('样图.png.white.jpg');target.rename(old)
        row['export']=old.relative_to(self.output).as_posix()
        with store.locked() as c:store._put(c,row)
        row=store.decide(row['source'],row['revision'],'approved','')
        self.assertFalse(old.exists());self.assertEqual(Path(row['export']).name,'样图.jpg')
    def test_every_image_pending_and_nothing_exported(self):
        original=digest(self.source/'子目录/样图.png');result=self.job();row=self.row()
        self.assertEqual(row['status'],'pending');self.assertEqual(result['auto_pass'],0)
        self.assertEqual({p.name for p in self.output.iterdir()},{'白底图','待重制'})
        self.assertEqual([p.name for p in self.output.rglob('*') if p.is_file()],[])
        self.assertEqual(digest(self.source/'子目录/样图.png'),original)
    def test_manual_approval_exports_full_size_white_jpeg(self):
        self.job();row=self.approve();self.assertEqual(row['status'],'approved')
        with Image.open(self.output/row['export']) as im:
            self.assertEqual(im.size,(120,80));self.assertEqual(im.format,'JPEG');self.assertEqual(im.getpixel((0,0)),(255,255,255))
    def test_redo_keeps_one_candidate_and_reason_then_approval_moves_it(self):
        self.job();store=BatchStore(self.output);row=self.row()
        row=store.decide(row['source'],row['revision'],'redo','右下角缺边')
        redo=self.output/row['export'];self.assertTrue(redo.exists())
        self.assertEqual(redo.name,'样图_返工-右下角缺边.jpg')
        self.assertNotIn('human',row)
        row=store.decide(row['source'],row['revision'],'approved','已确认')
        self.assertFalse(redo.exists());self.assertTrue((self.output/row['export']).exists())
        self.assertFalse(list(self.output.rglob('*.csv')))
        self.assertEqual(Path(row['export']).name,'样图.jpg')
    def test_edit_invalidates_approval_saves_lossless_mask_and_reopens(self):
        self.job();row=self.approve();export=self.output/row['export'];store=BatchStore(self.output)
        row=store.begin_edit(row['source'],row['revision']);self.assertEqual(row['status'],'editing');self.assertFalse(export.exists())
        mask=store.load_mask(row);mask.putpixel((30,20),0)
        row=store.save_edit(row['source'],row['revision'],mask,'修改一处')
        reopened=BatchStore(self.output);saved=reopened.get(row['source'])
        self.assertEqual(reopened.load_mask(saved).getpixel((30,20)),0)
        self.assertEqual(reopened.load_mask(saved,base=True).getpixel((30,20)),255)
        self.assertNotIn('human',saved);self.assertEqual(saved['status'],'editing')
        self.assertEqual(len(list(store.masks.glob('*.png'))),2)
    def test_original_change_blocks_old_approval_and_rerun_invalidates_it(self):
        self.job();row=self.approve();old=self.output/row['export'];self.photo('子目录/样图.png','blue')
        with self.assertRaisesRegex(ValueError,'原图已经改变'):BatchStore(self.output).decide(row['source'],row['revision'],'approved','')
        self.job();self.assertEqual(self.row()['status'],'pending');self.assertFalse(old.exists())
    def test_model_or_layout_change_requires_new_review(self):
        self.job();self.approve();self.job(options=Options(layout='square',size=1000))
        self.assertEqual(self.row()['status'],'pending');self.assertIn('尺寸变化',self.row()['automatic'])
    def test_resume_preserves_manual_edit_and_decision(self):
        self.job();row=self.approve();self.assertEqual(self.job()['skipped'],1);self.assertEqual(self.calls,1)
        self.assertEqual(self.row()['status'],'approved')
    def test_missing_internal_mask_regenerates_and_invalidates_approval(self):
        self.job();row=self.approve();store=BatchStore(self.output);(store.masks/row['mask']).unlink()
        self.assertEqual(self.job()['success'],1);self.assertEqual(self.row()['status'],'pending')
    def test_external_edits_are_not_overwritten_or_deleted(self):
        self.job();row=self.approve();path=self.output/row['export'];path.write_bytes(b'manual result')
        with self.assertRaisesRegex(ValueError,'外部修改'):BatchStore(self.output).begin_edit(row['source'],row['revision'])
        self.assertEqual(path.read_bytes(),b'manual result')
    def test_export_failure_keeps_pending_and_mask(self):
        self.job();row=self.row();store=BatchStore(self.output)
        with patch('workflow.save_image_atomic',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):store.decide(row['source'],row['revision'],'approved','')
        self.assertEqual(self.row()['status'],'pending');self.assertFalse(list((self.output/'白底图').rglob('*.jpg')))
        self.assertEqual(store.load_mask(self.row()).size,(120,80))
    def test_bad_saved_dimensions_do_not_mark_approved(self):
        import workflow
        self.job();store=BatchStore(self.output);row=self.row();save=workflow.save_image_atomic
        def wrong(image,path,fmt,**kwargs):return save(image.resize((20,20)),path,fmt,**kwargs)
        with patch('workflow.save_image_atomic',wrong):
            with self.assertRaisesRegex(ValueError,'尺寸异常'):store.decide(row['source'],row['revision'],'approved','')
        self.assertEqual(self.row()['status'],'pending')
    def test_interrupted_export_recovers_without_duplicate(self):
        self.job();store=BatchStore(self.output);row=self.row();row=store.decide(row['source'],row['revision'],'redo','需修')
        old=self.output/row['export'];original_report=store._report
        with patch.object(store,'_report',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):store.decide(row['source'],row['revision'],'approved','已修')
        self.assertTrue(old.exists())
        recovered=BatchStore(self.output);row=recovered.get(row['source'])
        self.assertEqual(row['status'],'approved');self.assertTrue((self.output/row['export']).exists());self.assertFalse(old.exists())
    def test_stale_window_revision_cannot_overwrite_new_edit(self):
        self.job();store=BatchStore(self.output);row=self.row();new=store.begin_edit(row['source'],row['revision'])
        with self.assertRaisesRegex(ValueError,'刷新'):store.decide(row['source'],row['revision'],'approved','')
        self.assertEqual(self.row()['revision'],new['revision'])
    def test_failed_images_can_be_manually_recorded_without_candidate(self):
        (self.source/'坏图.jpg').write_bytes(b'broken');self.job();store=BatchStore(self.output);row=store.get('坏图.jpg')
        self.assertEqual(row['status'],'failed')
        with self.assertRaisesRegex(ValueError,'没有候选'):store.decide(row['source'],row['revision'],'approved','')
        with self.assertRaisesRegex(ValueError,'无法解码'):store.decide(row['source'],row['revision'],'redo','原图损坏')
        self.assertEqual(store.get(row['source'])['status'],'failed')
        self.assertFalse(list(self.output.rglob('*.csv')))
    def test_stop_and_resume(self):
        self.photo('第二张.png');event=threading.Event()
        result=self.job(cancel=event,emit=lambda kind,value:event.set() if kind=='progress' else None)
        self.assertTrue(result['cancelled']);result=self.job();self.assertEqual((result['success'],result['skipped']),(1,1))
    def test_missing_export_preserves_manual_edits_and_requires_reconfirmation(self):
        self.job();store=BatchStore(self.output);row=self.row();row=store.begin_edit(row['source'],row['revision'])
        mask=store.load_mask(row);mask.putpixel((30,20),0);row=store.save_edit(row['source'],row['revision'],mask,'保留编辑')
        row=store.decide(row['source'],row['revision'],'approved','保留编辑');(self.output/row['export']).unlink()
        self.assertEqual(self.job()['skipped'],1);row=self.row()
        self.assertEqual(row['status'],'editing');self.assertEqual(store.load_mask(row).getpixel((30,20)),0)
        self.assertEqual(self.calls,1)
    def test_stopped_model_preparation_does_not_leave_processing_state(self):
        event=threading.Event()
        def session(model):event.set();return object()
        result=run_batch(self.source,self.output,Options(),session_factory=session,remover=self.remover,cancel=event)
        self.assertTrue(result['cancelled']);self.assertEqual(self.row()['status'],'failed')
        self.assertIn('已停止',self.row()['error'])
    def test_unreadable_source_continues_to_next_image(self):
        self.photo('第二张.png');import workflow
        original_digest=workflow.digest
        def sometimes(path):
            if Path(path).resolve()==(self.source/'子目录/样图.png').resolve():raise PermissionError('no read permission')
            return original_digest(path)
        with patch('workflow.digest',sometimes):result=self.job()
        self.assertEqual((result['failed'],result['success']),(1,1));self.assertEqual(self.row()['status'],'failed')
    def test_repeated_edits_do_not_accumulate_orphan_masks(self):
        self.job();store=BatchStore(self.output);row=self.row();row=store.begin_edit(row['source'],row['revision'])
        for n in range(5):
            mask=store.load_mask(row);mask.putpixel((30+n,20),0);row=store.save_edit(row['source'],row['revision'],mask,'')
        self.assertEqual(len(list(store.masks.glob('*.png'))),2)
    def test_automatic_warning_does_not_export_or_decide(self):
        self.job(options=Options(layout='square',size=1000));row=self.row()
        self.assertIn('尺寸变化',row['automatic']);self.assertEqual(row['status'],'pending')
        self.assertFalse(list(self.output.rglob('*.jpg')))
    def test_staged_failure_does_not_destroy_previous_redo_image(self):
        self.job();store=BatchStore(self.output);row=self.row();row=store.decide(row['source'],row['revision'],'redo','有问题')
        path=self.output/row['export'];before=digest(path)
        with patch('workflow.save_image_atomic',side_effect=OSError('full')):
            with self.assertRaises(OSError):store.decide(row['source'],row['revision'],'approved','已修')
        self.assertEqual(digest(path),before);self.assertEqual(self.row()['status'],'redo')
    def test_synthetic_edge_checks_remain_informational(self):
        from PIL import ImageFilter
        original=Image.new('RGBA',(256,256),'#ddd');ImageDraw.Draw(original).rectangle((60,60,190,190),fill='red')
        mask=Image.new('L',original.size,0);ImageDraw.Draw(mask).rectangle((60,60,190,190),fill=255)
        cutout=original.copy();cutout.putalpha(mask.filter(ImageFilter.GaussianBlur(8)))
        white,_=render_white(cutout,Options())
        self.assertTrue(any('边缘不清晰' in n for n in inspect_cutout(original,cutout,white,Options())))
    def test_startup_import_does_not_load_rembg(self):
        import subprocess
        path=str(Path(__file__).resolve().parents[1])
        result=subprocess.run([sys.executable,'-c',"import sys;sys.path.insert(0,"+repr(path)+");import web_server;assert 'rembg' not in sys.modules;assert 'editor' not in sys.modules"],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_existing_output_allowed_without_deletion(self):
        self.output.mkdir();old=self.output/'待人工复核';old.mkdir();(old/'old.jpg').write_bytes(b'old')
        self.job()
        self.assertEqual((old/'old.jpg').read_bytes(),b'old')
    def test_shared_output_batches_are_isolated_and_names_reserved(self):
        self.job();first=BatchStore(self.output,self.source);row=self.approve();first_export=self.output/row['export'];before=digest(first_export)
        other=self.root/'other';other.mkdir();(other/'子目录').mkdir()
        Image.new('RGB',(120,80),'blue').save(other/'子目录/样图.png')
        run_batch(other,self.output,Options(),session_factory=lambda _:object(),remover=self.remover)
        second=BatchStore(self.output,other);row=second.get('子目录/样图.png')
        row=second.decide(row['source'],row['revision'],'redo','second batch')
        row=second.decide(row['source'],row['revision'],'approved','second batch')
        self.assertEqual(Path(row['export']).name,'样图_2.jpg');self.assertEqual(digest(first_export),before)
        self.assertNotIn('human',first.get('子目录/样图.png'))
        self.assertNotIn('human',second.get('子目录/样图.png'))
        self.assertFalse(list(self.output.rglob('*.csv')))
        row=second.begin_edit(row['source'],row['revision']);self.assertTrue(first_export.exists())
        row=second.decide(row['source'],row['revision'],'approved','edited');self.assertEqual(Path(row['export']).name,'样图_2.jpg')
        self.assertEqual(BatchStore(self.output,self.source).cache,first.cache)
        self.assertEqual(BatchStore(self.output,other).cache,second.cache)

    def test_paths_do_not_escape_or_follow_external_symlink(self):
        for path in ['../x',str(self.root/'x'),'.']:
            with self.assertRaises(ValueError):safe_path(self.source,path)
        (self.source/'link').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):safe_path(self.source,'link/x')
    def test_no_empty_mask_approval_after_edit(self):
        self.job();store=BatchStore(self.output);row=self.row();row=store.begin_edit(row['source'],row['revision'])
        row=store.save_edit(row['source'],row['revision'],Image.new('L',(120,80),0),'误擦')
        with self.assertRaises(ValueError):store.decide(row['source'],row['revision'],'approved','')
        self.assertEqual(self.row()['status'],'editing')
    def test_square_export_is_intentional_and_validated(self):
        self.job(options=Options(layout='square',size=1000));row=self.approve()
        with Image.open(self.output/row['export']) as im:self.assertEqual(im.size,(1000,1000))

class EditorTests(unittest.TestCase):
    def test_lasso_restores_irregular_area_and_can_undo(self):
        edit=MaskEditor(Image.new('L',(30,30),0));before=edit.mask.tobytes()
        self.assertTrue(edit.apply('lasso',[(2,2),(20,2),(20,5),(5,5),(5,20),(2,20)],True))
        self.assertEqual(edit.mask.getpixel((3,15)),255);self.assertEqual(edit.mask.getpixel((15,15)),0)
        edit.undo_one();self.assertEqual(edit.mask.tobytes(),before)
    def test_small_brush_restores_fine_line(self):
        edit=MaskEditor(Image.new('L',(30,30),0));edit.apply('brush',[(15,3),(15,26)],True,1)
        self.assertEqual(edit.mask.getpixel((15,15)),255);self.assertEqual(edit.mask.getpixel((18,15)),0)
    def test_wheel_zoom_is_capped_and_reversible(self):
        from editor import wheel_zoom_factor,scroll_distance
        self.assertAlmostEqual(wheel_zoom_factor(1000),1.03)
        self.assertAlmostEqual(wheel_zoom_factor(1000)*wheel_zoom_factor(-1000),1)
        self.assertEqual(wheel_zoom_factor(0),1)
        self.assertLess(wheel_zoom_factor(.1),wheel_zoom_factor(1))
        self.assertEqual(scroll_distance(1000,'darwin'),48)
        self.assertEqual(scroll_distance(-1,'darwin'),-2)
    def test_pan_zoom_anchor_and_text_focus(self):
        from editor import ReviewWindow
        from types import SimpleNamespace
        view=ReviewWindow.__new__(ReviewWindow);view.zoom=2;view.center=(50,40);view.schedule_render=lambda:None
        view.pan(40,-40);self.assertEqual(view.center,(70,20))
        anchor=(80,60,100,100);before=(view.center[0]+15,view.center[1]+5)
        view.set_zoom(4,anchor);self.assertEqual((view.center[0]+7.5,view.center[1]+2.5),before)
        entry=SimpleNamespace(winfo_class=lambda:'TEntry');old=view.center
        self.assertIsNone(view.key_pan(SimpleNamespace(widget=entry),40,0));self.assertEqual(view.center,old)
        canvas=SimpleNamespace(winfo_class=lambda:'Canvas')
        self.assertEqual(view.key_pan(SimpleNamespace(widget=canvas),0,40),'break')
    def test_brush_rectangle_polygon_restore_remove(self):
        edit=MaskEditor(Image.new('L',(100,100),0))
        edit.apply('rectangle',[(10,10),(50,50)],True);self.assertEqual(edit.mask.getpixel((20,20)),255)
        edit.apply('brush',[(20,20),(30,30)],False,3);self.assertEqual(edit.mask.getpixel((25,25)),0)
        edit.apply('polygon',[(60,60),(90,60),(75,90)],True);self.assertEqual(edit.mask.getpixel((75,70)),255)
        self.assertEqual(edit.mask.getpixel((99,99)),0)
    def test_undo_redo_exact_and_new_edit_clears_redo(self):
        edit=MaskEditor(Image.new('L',(100,100),0));before=edit.mask.tobytes()
        edit.apply('rectangle',[(20,20),(40,40)],True);changed=edit.mask.tobytes()
        self.assertTrue(edit.undo_one());self.assertEqual(edit.mask.tobytes(),before)
        self.assertTrue(edit.redo_one());self.assertEqual(edit.mask.tobytes(),changed)
        edit.undo_one();edit.apply('brush',[(70,70)],True,2);self.assertFalse(edit.redo_one())
    def test_viewport_100_percent_pixel_exact(self):
        im=Image.new('RGB',(100,80))
        for x in range(100):
            for y in range(80):im.putpixel((x,y),(x,y,120))
        before=im.tobytes();view=viewport_image(im,(30,20),1,(50,40))
        self.assertEqual(view.tobytes(),im.crop((35,30,65,50)).tobytes());self.assertEqual(im.tobytes(),before)
    def test_viewport_outside_and_zoom(self):
        im=Image.new('RGB',(40,30),(20,40,60))
        self.assertEqual(viewport_image(im,(20,20),2,(20,15)).getpixel((10,10)),(20,40,60))
        self.assertEqual(viewport_image(im,(20,20),1,(500,500)).getpixel((10,10)),(237,240,243))

if __name__=='__main__':unittest.main(verbosity=2)
