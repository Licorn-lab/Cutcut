# 保存v1.9.5更新

在原项目覆盖更新，保留.git。VS Code项目终端执行：

```bash
git status
git diff --stat
git add core.py 使用说明.md 更新说明.md 验证记录.md Git提交说明.md tests/
git commit -m "升级v1.9.5：兼容MPO主照片读取"
git status
```

以上针对v1.9.4升级，不提交照片、导出、模型、审核缓存或.venv。
