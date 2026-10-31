import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import cloud_matting as cloud

CONFIG={'bucket':'example-1234567890','region':'ap-guangzhou','secret_id':'A'*20,'secret_key':'B'*20}

class CloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.journal=patch.object(cloud,'JOURNAL',self.root/'cleanup.json');self.journal.start()
        self.image=self.root/'image.png';Image.new('RGB',(80,60),'red').save(self.image)
    def tearDown(self):self.journal.stop();self.tmp.cleanup()
    def test_credentials_never_in_process_arguments(self):
        from subprocess import CompletedProcess
        with patch.object(cloud.subprocess,'run',return_value=CompletedProcess([],0,'','')) as run:
            cloud.save_config(CONFIG)
        args=run.call_args.args[0]
        self.assertNotIn(CONFIG['secret_key'],str(args));self.assertNotIn(CONFIG['secret_id'],str(args))
        self.assertIn('add-generic-password',run.call_args.kwargs['input'])
    def test_cloud_success_and_cleanup(self):
        result=io.BytesIO();Image.new('RGBA',(80,60),(255,0,0,100)).save(result,'PNG');calls=[]
        def request(config,method,key,query=None,body=None):
            calls.append((method,key,query));return result.getvalue() if method=='GET' else b''
        with patch.object(cloud,'signed_request',side_effect=request):mask,remaining=cloud.run(self.image,CONFIG)
        self.assertEqual(mask.getpixel((1,1)),100);self.assertEqual(remaining,0)
        self.assertEqual([c[0] for c in calls],['PUT','GET','DELETE']);self.assertEqual(cloud.pending(),[])
    def test_failed_delete_is_recorded_and_retried(self):
        def request(config,method,key,query=None,body=None):
            if method=='GET':raise ValueError('failure')
            if method=='DELETE':raise ValueError('offline')
        with patch.object(cloud,'signed_request',side_effect=request):
            with self.assertRaisesRegex(ValueError,'待核实或清理'):cloud.run(self.image,CONFIG)
        self.assertEqual(len(cloud.pending()),1)
        with patch.object(cloud,'signed_request',return_value=b''):self.assertEqual(cloud.cleanup(CONFIG),0)
    def test_invalid_cloud_image_preserves_cleanup(self):
        result=io.BytesIO();Image.new('RGB',(80,60)).save(result,'PNG')
        with patch.object(cloud,'signed_request',side_effect=lambda c,m,k,query=None,body=None:result.getvalue() if m=='GET' else b''):
            with self.assertRaisesRegex(ValueError,'透明通道'):cloud.run(self.image,CONFIG)
        self.assertEqual(cloud.pending(),[])
    def test_query_signature_keeps_case(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return b'OK'
        with patch.object(cloud,'urlopen',return_value=Response()) as call:
            cloud.signed_request(CONFIG,'GET','cutcut-temporary/test.png',{'ci-process':'AIPicMatting'})
        self.assertIn('ci-process=AIPicMatting',call.call_args.args[0].full_url)

    def test_http_error_identifies_step_without_secrets(self):
        from urllib.error import HTTPError
        for method,stage in [('PUT','上传图片'),('GET','云端抠图'),('DELETE','清理临时文件')]:
            with self.subTest(method=method), patch.object(cloud,'urlopen',side_effect=HTTPError('https://example.test',403,'forbidden',{},None)):
                with self.assertRaises(ValueError) as result:
                    cloud.signed_request(CONFIG,method,'cutcut-temporary/test.png')
                self.assertIn(stage,str(result.exception));self.assertIn('403',str(result.exception))
                self.assertNotIn(CONFIG['secret_id'],str(result.exception));self.assertNotIn(CONFIG['secret_key'],str(result.exception))

if __name__=='__main__':unittest.main()
