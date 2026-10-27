"""Export naming and filename-only rework notes; tiny synthetic images only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from core import Options, digest
from workflow import BatchStore, rework_note


class ExportNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'WHITEBG_DATA_HOME':str(self.root/'cache')});self.env.start()
        self.source=self.root/'source';self.source.mkdir();self.output=self.root/'out'
        self.path=self.source/'原名.png';Image.new('RGB',(48,32),'red').save(self.path)
        self.before=digest(self.path);self.store=BatchStore(self.output,self.source)
        self.row=self.candidate(self.path)
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def candidate(self,path):
        row=self.store.prepare(path.name,digest(path),Options(model='u2net'))
        return self.store.finish(row,Image.new('L',(48,32),255),'')
    def decide(self,decision,note='',naming='original',name=''):
        self.row=self.store.decide(self.row['source'],self.row['revision'],decision,note,naming,name)
        return self.output/self.row['export']
    def test_custom_filename_and_note_reopen_without_separate_note(self):
        result=self.decide('redo','去掉/色卡\n保留流苏','custom','作品集主图.jpg')
        self.assertEqual(result.name,'作品集主图_返工-去掉_色卡_保留流苏.jpg')
        row=BatchStore(self.output,self.source).get('原名.png')
        self.assertEqual(rework_note(row),'去掉_色卡_保留流苏')
        self.assertNotIn('human',row);self.assertNotIn('task_context',row)
        self.assertNotIn('reason',row);self.assertNotIn('requirements',row)
        self.assertFalse(list(self.output.rglob('*.csv')));self.assertFalse(list(self.output.rglob('*.json')))
        self.assertEqual(digest(self.path),self.before)
    def test_approval_uses_original_name_ignores_note_and_retires_redo(self):
        redo=self.decide('redo','缺边')
        final=self.decide('approved','不可保存的通过备注')
        self.assertEqual(final.name,'原名.jpg');self.assertFalse(redo.exists())
        self.assertEqual(rework_note(self.row),'')
        self.assertNotIn('不可保存的通过备注',json.dumps(self.row,ensure_ascii=False))
    def test_custom_approval_and_collision_number_before_note(self):
        first=self.decide('redo','去掉色卡','custom','图一')
        other=self.source/'other.png';Image.new('RGB',(48,32),'blue').save(other)
        row=self.candidate(other)
        row=self.store.decide(row['source'],row['revision'],'redo','去掉色卡','custom','图一')
        self.assertEqual(Path(row['export']).name,'图一_2_返工-去掉色卡.jpg')
        self.assertEqual(rework_note(row),'去掉色卡');self.assertTrue(first.exists())
        row=self.store.decide(row['source'],row['revision'],'approved','应忽略','custom','成品')
        self.assertEqual(Path(row['export']).name,'成品.jpg')
    def test_changing_note_moves_only_owned_export(self):
        old=self.decide('redo','第一次')
        new=self.decide('redo','第二次')
        self.assertFalse(old.exists());self.assertTrue(new.exists())
        new.write_bytes(b'external')
        with self.assertRaisesRegex(ValueError,'外部修改'):self.decide('redo','第三次')
        self.assertEqual(new.read_bytes(),b'external')
    def test_invalid_names_and_notes_do_not_destroy_existing_export(self):
        old=self.decide('approved');before=digest(old)
        for name in ('../outside','/tmp/file','', 'x'*241,'bad:name'):
            with self.subTest(name=name),self.assertRaises(ValueError):self.decide('redo','删色卡','custom',name)
            self.assertEqual(digest(old),before)
        with self.assertRaises(ValueError):self.decide('redo','字'*100)
        self.assertEqual(digest(old),before)
    def test_readable_failed_image_can_be_exported_with_rework_note(self):
        row=self.store.prepare('原名.png',self.before,Options(model='u2net'),self.row['revision'])
        self.row=self.store.fail(row,'模型失败')
        target=self.decide('redo','需要重抠')
        self.assertTrue(target.exists())
        with Image.open(target) as im:self.assertEqual(im.size,(48,32))
    def test_legacy_notes_move_to_filename_and_old_fields_and_report_retire(self):
        row=self.row.copy();row['human']='去色卡';row['task_context']={'requirements':'旧要求'}
        with self.store.locked() as c:
            c.execute('UPDATE items SET data=? WHERE key=?',(json.dumps(row,ensure_ascii=False),row['source']))
            c.execute("INSERT INTO metadata VALUES ('report','重制原因.csv')")
        # A legacy redo without an export is also migrated using the readable original.
        with self.store.locked() as c:
            row['status']='redo';c.execute('UPDATE items SET data=? WHERE key=?',(json.dumps(row,ensure_ascii=False),row['source']))
        report=self.output/'待重制/重制原因.csv';report.write_text('原图相对路径,自动提示,人工原因,重制图片路径,处理错误\n原名.png,,去色卡,,',encoding='utf-8-sig')
        reopened=BatchStore(self.output,self.source);row=reopened.get(row['source'])
        self.assertEqual(Path(row['export']).name,'原名_返工-去色卡.jpg')
        self.assertNotIn('human',row);self.assertNotIn('task_context',row);self.assertFalse(report.exists())
        self.assertEqual(rework_note(row),'去色卡');self.assertEqual(digest(self.path),self.before)

if __name__=='__main__':unittest.main()
