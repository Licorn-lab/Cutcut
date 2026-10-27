import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from task_cards import TaskCards
from pretrained_classifier import interpret, prompts, CATEGORIES, FEATURE_PROMPTS

class PretrainedTests(unittest.TestCase):
    def test_ai_cannot_overwrite_confirmed_labels_requirements_or_model(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'images';source.mkdir();path=source/'one.png'
            Image.new('RGB',(20,20)).save(path)
            cards=TaskCards(source,Path(directory)/'cards')
            value=cards.item(path);value.update(features=['浅色主体'],features_confirmed=True,requirements='保留吊带',model='isnet-general-use')
            cards.save_item(path,value)
            result={'sha256':value['sha256'],'suggested_features':['流苏与细带']}
            saved=cards.save_classification(path,result)
            self.assertEqual(saved['features'],['浅色主体'])
            self.assertTrue(saved['features_confirmed'])
            self.assertEqual(saved['requirements'],'保留吊带')
            self.assertEqual(saved['model'],'isnet-general-use')
            self.assertEqual(saved['classification'],result)
            saved['features_confirmed']=False;cards.save_item(path,saved)
            self.assertEqual(cards.save_classification(path,result)['features'],['流苏与细带'])
            with self.assertRaises(ValueError):cards.save_classification(path,{'sha256':'stale'})
    def test_equal_similarity_is_uncertain_and_never_percentage(self):
        result=interpret([[1.,0.]],[[1.,0.]]*len(prompts()))
        self.assertTrue(result['category_uncertain'])
        self.assertEqual(result['suggested_features'],[])
        self.assertTrue(all(v['suggested'] is None for v in result['feature_scores'].values()))
        self.assertNotIn('probability',result)

if __name__=='__main__':unittest.main()
