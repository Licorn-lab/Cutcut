"""Opt-in native pinch. Cocoa callbacks only queue data; Tk polls on its own loop."""
from collections import deque
import math
import os
import sys


def pinch_factor(amount):
    return math.exp(max(-math.log(1.06),min(math.log(1.06),amount)))


class PinchBridge:
    def __init__(self,window,canvases,callback):
        self.window=window;self.canvases=canvases;self.callback=callback
        self.monitor=None;self.job=None;self.closed=False;self.pending=deque(maxlen=128)
        self.status='缩放：⌘+滚动；原生捏合已关闭，双指滚动仍平移。'
        window.bind('<Destroy>',self.destroy,add='+')
        # Stable default avoids entering the native bridge on affected Mac/Tk builds.
        if sys.platform!='darwin' or os.environ.get('WHITEBG_NATIVE_PINCH')!='1':return
        try:
            from AppKit import NSEvent,NSEventMaskMagnify
            self.NSEvent=NSEvent;self.handler=self.handle
            self.monitor=NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskMagnify,self.handler)
            if self.monitor is None:raise RuntimeError('无法建立手势监听')
            self.job=window.after(40,self.flush)
            self.status='原生捏合测试模式：实机兼容性未确认；若退出请改用正常启动。'
        except Exception as error:
            if self.monitor is not None:self.NSEvent.removeMonitor_(self.monitor);self.monitor=None
            self.status='双指捏合未启用；暂用⌘+滚动。'
            print('Mac捏合组件未启用：'+str(error),flush=True)
    def handle(self,event):
        # Do not call winfo, after, drawing, or any Tk operation from Cocoa.
        if not self.closed:
            try:
                delta=float(event.magnification())
                if math.isfinite(delta):self.pending.append(math.log(max(.1,1+delta)))
            except Exception as error:print('捏合事件未读取：'+str(error),flush=True)
        return event
    def flush(self):
        self.job=None
        if self.closed:return
        amount=0.
        while self.pending:amount+=self.pending.popleft()
        try:
            if amount:
                px,py=self.window.winfo_pointerxy()
                canvas=self.window.winfo_containing(px,py)
                if canvas in self.canvases:
                    self.callback(pinch_factor(amount),canvas,px-canvas.winfo_rootx(),py-canvas.winfo_rooty())
        except Exception as error:print('Tk缩放未完成：'+str(error),flush=True)
        if not self.closed:self.job=self.window.after(40,self.flush)
    def destroy(self,event):
        if event.widget is not self.window:return
        self.closed=True;self.pending.clear()
        if self.job is not None:self.window.after_cancel(self.job);self.job=None
        if self.monitor is not None:self.NSEvent.removeMonitor_(self.monitor);self.monitor=None
