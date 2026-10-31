import json
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Options,digest,run_batch
from workflow import BatchStore
from task_cards import TaskCards,context_hash

class TaskCardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir();self.image=self.source/'sample.png'
        Image.new('RGB',(40,30),'red').save(self.image)
        self.output=self.root/'output';self.before=digest(self.image)
        self.env=patch.dict('os.environ',{'WHITEBG_DATA_HOME':str(self.root/'runtime')});self.env.start()
        self.cards=TaskCards(self.source,self.root/'cards')
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def job(self,context):
        self.models=[]
        def session(model):self.models.append(model);return object()
        def remove(image,session,options):
            image=image.copy();image.putalpha(Image.new('L',image.size,255));return image
        return run_batch(self.source,self.output,Options(model='u2net'),session_factory=session,remover=remove,task_contexts=context)
    def test_save_reload_and_original_change_discards_old_context(self):
        item=self.cards.item(self.image);item.update(model='isnet-general-use',requirements='保留流苏',features=['流苏与细带'],features_confirmed=True)
        self.cards.save_item(self.image,item)
        loaded=TaskCards(self.source,self.root/'cards');self.assertEqual(loaded.item(self.image),item)
        Image.new('RGB',(40,30),'blue').save(self.image)
        self.assertEqual(loaded.item(self.image)['requirements'],'')
        with self.assertRaises(ValueError):loaded.save_item(self.image,item)
    def test_historical_exception_uses_hash_and_preserves_review(self):
        historical={'features':['旧标签'],'featuresConfirmed':True,'preferred':'u2net','confirmed':True}
        self.cards.seeds[self.before]=('11',deepcopy(historical))
        value=self.cards.item(self.image)
        self.assertIn('只保留包身',value['requirements'])
        self.assertEqual(value['model'],'') # Historical quality choice is not an automatic speed rule.
        self.assertEqual(value['historical_review'],historical)
        value['historical_review']['preferred']=''
        self.assertEqual(self.cards.item(self.image)['historical_review'],historical)
    def test_routing_resume_context_change_and_pending_decision(self):
        item=self.cards.item(self.image);item.update(model='isnet-general-use',requirements='保留流苏')
        self.cards.save_item(self.image,item);snapshot=self.cards.snapshot([self.image])
        self.assertEqual(self.job(snapshot)['success'],1);self.assertEqual(self.models,['isnet-general-use'])
        store=BatchStore(self.output);row=store.get('sample.png')
        self.assertNotIn('task_context',row);self.assertEqual(row['status'],'pending')
        self.assertEqual(self.job(snapshot)['skipped'],1);self.assertEqual(self.models,[])
        snapshot['sample.png']['requirements']='去掉色卡，保留流苏'
        self.assertEqual(self.job(snapshot)['success'],1)
        self.assertEqual(BatchStore(self.output).get('sample.png')['status'],'pending')
        self.assertEqual(digest(self.image),self.before)
        self.assertEqual(list((self.output/'白底图').iterdir()),[])
    def test_manual_retry_keeps_task_context_and_resume(self):
        snapshot=self.cards.snapshot([self.image]);self.job(snapshot)
        store=BatchStore(self.output);row=store.get('sample.png')
        selected=replace(Options(**row['options']),model='birefnet-general')
        retry=store.prepare(row['source'],row['source_hash'],selected,row['revision'],row['config'])
        self.assertNotIn('task_context',retry)
        store.finish(retry,Image.new('L',(40,30),255),'')
        self.assertEqual(self.job(snapshot)['skipped'],1)
        self.assertEqual(self.models,[])

    def test_stale_or_disabled_model_stops_before_inference(self):
        snapshot=self.cards.snapshot([self.image]);snapshot['sample.png']['sha256']='changed'
        with self.assertRaises(ValueError):self.job(snapshot)
        snapshot=self.cards.snapshot([self.image]);snapshot['sample.png']['model']='birefnet-hr'
        with self.assertRaises(ValueError):self.job(snapshot)
        self.assertEqual(self.models,[])

if __name__=='__main__':unittest.main()
