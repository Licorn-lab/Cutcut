import json
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_cards import TaskCards

class CategoryTests(unittest.TestCase):
    def test_prediction_confirmation_uncertainty_export_and_changed_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'images';source.mkdir();path=source/'one.png'
            Image.new('RGB',(20,20),'white').save(path)
            cards=TaskCards(source,root/'cards');item=cards.item(path)
            result={'sha256':item['sha256'],'suggested_features':[], 'category_candidates':[{'label':'包袋','similarity':.3}], 'category_uncertain':False}
            value=cards.save_classification(path,result)
            self.assertEqual(value['category'],'包袋');self.assertFalse(value['category_confirmed'])
            result['category_uncertain']=True
            self.assertEqual(cards.save_classification(path,result)['category'],'待确认')
            value=cards.item(path);value.update(category='平面织物',category_confirmed=True,requirements='去色卡',features=['流苏与细带'],features_confirmed=True)
            cards.save_item(path,value);result['category_uncertain']=False
            value=cards.save_classification(path,result)
            self.assertEqual(value['category'],'平面织物');self.assertEqual(value['requirements'],'去色卡')
            exported=cards.export_drafts([path],root/'export.json')
            self.assertEqual(exported,json.loads((root/'export.json').read_text()))
            self.assertTrue(exported['cards'][0]['category_confirmed'])
            self.assertEqual(exported['cards'][0]['image_requirements'],'去色卡')
            self.assertEqual(cards.snapshot([path])['one.png']['category'],'平面织物')
            Image.new('RGB',(20,20),'blue').save(path)
            self.assertEqual(cards.item(path)['category'],'待确认')
            self.assertFalse(cards.item(path)['category_confirmed'])
    def test_invalid_or_unresolved_category_cannot_be_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory);path=source/'one.png';Image.new('RGB',(10,10)).save(path)
            cards=TaskCards(source,source/'cards');value=cards.item(path)
            value.update(category='待确认',category_confirmed=True)
            with self.assertRaises(ValueError):cards.save_item(path,value)
            value.update(category='不存在',category_confirmed=False)
            with self.assertRaises(ValueError):cards.save_item(path,value)

if __name__=='__main__':unittest.main()
