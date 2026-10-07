# 保存v1.9.3更新

将更新文件复制到原项目，保留.git，不重新初始化。说明和tests也更新后，在VS Code原项目终端执行：

```bash
git status
git diff --stat
git add core.py editor.py 使用说明.md 更新说明.md 验证记录.md Git提交说明.md tests/
git commit -m "升级v1.9.3：审核末尾绕回未审核图片"
git status
```

以上清单针对从v1.9.2升级。不要提交照片、导出、模型、审核缓存或.venv。
