# 在原Git项目中提交v1.7

先关闭工具，将新版文件覆盖原项目相应文件；说明和tests也可复制。保留原来的.git、.gitignore、.venv，不要再次git init。

在VS Code打开原项目，在终端执行：

```bash
git status
git diff --stat
git add core.py app.py cli.py workflow.py editor.py 使用说明.md 更新说明.md 验证记录.md Git提交说明.md tests/test_workflow.py
git commit -m "升级v1.7：套索编辑、平移缩放及可选BiRefNet"
git status
```

提交保存本机代码版本。原图、输出、模型与审核进度不提交；Git不自动备份这些数据。
