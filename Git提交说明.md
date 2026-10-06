# 保存v1.9.1更新

在原项目覆盖core.py、editor.py、gestures.py；说明和tests也更新后执行：

```bash
git status
git add core.py editor.py gestures.py 使用说明.md 更新说明.md 验证记录.md Git提交说明.md tests/
git commit -m "修复捏合重入风险并默认使用兼容缩放"
```

若复制了可选启动_捏合测试.command，也将该文件加入暂存。保留.git，不重新初始化。
