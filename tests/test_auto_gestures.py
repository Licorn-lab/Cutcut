import sys
import tempfile
import unittest
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Options
from auto_model import AutoSession,evaluate_mask
from gestures import PinchBridge,pinch_factor

class AutoTests(unittest.TestCase):
    def setUp(self):
        self.image=Image.new('RGBA',(100,80),'white');ImageDraw.Draw(self.image).rectangle((25,15,75,65),fill='red')
        self.good=self.image.copy();mask=Image.new('L',self.image.size,0);ImageDraw.Draw(mask).rectangle((25,15,75,65),fill=255);self.good.putalpha(mask)
        self.empty=self.image.copy();self.empty.putalpha(0)
    def test_quality_prefers_complete_cutout_over_empty_and_full_background(self):
        self.assertGreater(evaluate_mask(self.image,self.good)['score'],evaluate_mask(self.image,self.empty)['score'])
        self.assertGreater(evaluate_mask(self.image,self.good)['score'],evaluate_mask(self.image,self.image)['score'])
    def test_clear_default_result_never_loads_other_models(self):
        seen=[]
        result=AutoSession(lambda m:seen.append(m) or object(),lambda *args:self.good).predict(self.image,Options())
        self.assertEqual(seen,['isnet-general-use']);self.assertFalse(result.info['routing']['fallback_triggered'])
    def test_missing_subject_and_residual_background_trigger_comparison(self):
        partial=self.good.copy();mask=partial.getchannel('A');ImageDraw.Draw(mask).rectangle((25,15,50,65),fill=0);partial.putalpha(mask)
        self.assertTrue(evaluate_mask(self.image,partial)['suspicious'])
        residue=self.good.copy();mask=residue.getchannel('A');ImageDraw.Draw(mask).rectangle((0,0,20,79),fill=255);residue.putalpha(mask)
        self.assertTrue(evaluate_mask(self.image,residue)['suspicious'])
    def test_model_choice_and_sessions_are_reused(self):
        created=[];called=[]
        def remove(image,session,options):called.append(options.model);return self.empty if options.model=='isnet-general-use' else self.good
        auto=AutoSession(lambda m:created.append(m) or object(),remove)
        first=auto.predict(self.image,Options());second=auto.predict(self.image,Options())
        self.assertEqual(first.info['routing']['selected'],'birefnet-general');self.assertEqual(len(created),2)
        self.assertEqual(second.size,self.image.size);self.assertNotIn('framed-dual',called)
    def test_third_model_only_when_initial_candidates_bad(self):
        seen=[]
        def remove(image,session,options):seen.append(options.model);return self.good if options.model=='u2net' else self.image
        result=AutoSession(lambda m:object(),remove).predict(self.image,Options())
        self.assertEqual(seen,['isnet-general-use','birefnet-general','u2net'])
        self.assertEqual(result.info['routing']['selected'],'u2net')
    def test_both_empty_fail_and_one_model_failure_is_explicit(self):
        with self.assertRaises(RuntimeError):AutoSession(lambda m:object(),lambda *args:self.empty).predict(self.image,Options())
        def factory(m):
            if m=='birefnet-general':raise RuntimeError('下载失败')
            return object()
        partial=self.good.copy();mask=partial.getchannel('A');ImageDraw.Draw(mask).rectangle((25,15,50,65),fill=0);partial.putalpha(mask)
        result=AutoSession(factory,lambda *args:partial).predict(self.image,Options())
        self.assertEqual(result.info['routing']['selected'],'isnet-general-use');self.assertTrue(result.info['auto_warnings'])
    def test_routing_persisted_without_approval(self):
        import os
        from workflow import run_batch,BatchStore
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'WHITEBG_DATA_HOME':str(Path(d)/'cache')}):
            source=Path(d)/'source';source.mkdir();self.image.save(source/'a.png');output=Path(d)/'output'
            auto=AutoSession(lambda m:object(),lambda *args:self.good)
            run_batch(source,output,Options(),session_factory=lambda m:auto,remover=lambda im,s,o:s.predict(im,o))
            row=BatchStore(output).get('a.png')
            self.assertIn(row['routing']['selected'],['isnet-general-use','birefnet-general']);self.assertEqual(row['status'],'pending')
            self.assertFalse(list(output.rglob('*.jpg')))

class FakeWindow:
    def __init__(self,canvas):self.canvas=canvas;self.jobs={};self.number=0;self.pointer=(100,80)
    def bind(self,*args,**kwargs):pass
    def winfo_pointerxy(self):return self.pointer
    def winfo_containing(self,*args):return self.canvas
    def after(self,delay,fn):self.number+=1;self.jobs[self.number]=fn;return self.number
    def after_cancel(self,job):self.jobs.pop(job,None)

class GestureTests(unittest.TestCase):
    def make_bridge(self):
        self.canvas=SimpleNamespace(winfo_rootx=lambda:10,winfo_rooty=lambda:20)
        self.window=FakeWindow(self.canvas);self.calls=[];self.removed=[]
        self.events=SimpleNamespace(addLocalMonitorForEventsMatchingMask_handler_=lambda mask,fn:'monitor',removeMonitor_=self.removed.append)
        appkit=SimpleNamespace(NSEvent=self.events,NSEventMaskMagnify=1)
        with patch('gestures.sys.platform','darwin'),patch.dict(sys.modules,{'AppKit':appkit}):
            self.bridge=PinchBridge(self.window,[self.canvas],lambda *args:self.calls.append(args))
        return self.bridge
    def test_pinch_direction_limit_and_coalescing(self):
        bridge=self.make_bridge();event=SimpleNamespace(magnification=lambda:.1)
        self.assertIsNone(bridge.handle(event));bridge.handle(event)
        self.assertEqual(len(self.window.jobs),1);bridge.flush()
        self.assertAlmostEqual(self.calls[0][0],1.06);self.assertEqual(self.calls[0][2:],(90,60))
        bridge.handle(SimpleNamespace(magnification=lambda:-.02));bridge.flush();self.assertLess(self.calls[-1][0],1)
        self.assertAlmostEqual(pinch_factor(100)*pinch_factor(-100),1)
    def test_outside_canvas_and_invalid_event_not_consumed(self):
        bridge=self.make_bridge();event=SimpleNamespace(magnification=lambda:.1)
        self.window.canvas=object();self.assertIs(bridge.handle(event),event)
        self.window.canvas=self.canvas;bad=SimpleNamespace(magnification=lambda:math.nan)
        self.assertIs(bridge.handle(bad),bad);self.assertFalse(self.window.jobs)
    def test_destroy_unregisters_monitor_and_cancels_timer(self):
        bridge=self.make_bridge();bridge.handle(SimpleNamespace(magnification=lambda:.1))
        bridge.destroy(SimpleNamespace(widget=object()));self.assertFalse(bridge.closed)
        bridge.destroy(SimpleNamespace(widget=self.window));self.assertTrue(bridge.closed)
        self.assertEqual(self.removed,['monitor']);self.assertFalse(self.window.jobs)
    def test_missing_component_keeps_window_usable(self):
        with patch('gestures.sys.platform','darwin'),patch.dict(sys.modules,{'AppKit':None}):
            canvas=object();bridge=PinchBridge(FakeWindow(canvas),[canvas],lambda *args:None)
            self.assertIn('未启用',bridge.status);self.assertIsNone(bridge.monitor)

if __name__=='__main__':unittest.main()
