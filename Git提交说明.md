# 保存v1.9更新

在原项目覆盖core.py、auto_model.py、workflow.py，说明和tests可一起复制。VS Code原项目终端执行：

```bash
git status
git diff --stat
git add core.py auto_model.py workflow.py 使用说明.md 更新说明.md 验证记录.md Git提交说明.md tests/
git commit -m "升级v1.9：可疑时换模型与原名加w导出"
git status
```

若之前v1.8文件也尚未提交，先将这些代码更新一起暂存。保留.git；不要提交原图、输出、运行环境、模型或审核缓存。
