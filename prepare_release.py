"""Create a clean shareable CutCut zip from an explicit allowlist."""
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = [
    'core.py', 'workflow.py', 'task_cards.py', 'image_geometry.py', 'editor.py', 'gestures.py', 'auto_model.py',
    'web_server.py', 'cloud_matting.py', 'instance_control.py', 'install_environment.py',
    'requirements.txt', '使用说明.md', 'prepare_release.py',
    'web/index.html', 'web/app.js', 'web/edit.js', 'web/geometry.js', 'web/cloud.js', 'web/style.css',
    'CutCut.app/Contents/Info.plist', 'CutCut.app/Contents/MacOS/launch',
    'CutCut.app/Contents/Resources/start.command', 'CutCut.app/Contents/Resources/CutCut.icns',
    '安装 CutCut.app/Contents/Info.plist', '安装 CutCut.app/Contents/MacOS/launch',
    '安装 CutCut.app/Contents/Resources/install.command', '安装 CutCut.app/Contents/Resources/CutCut.icns',
]


def build(destination):
    paths = [ROOT / name for name in FILES]
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError('发布文件缺失或为符号链接：' + str(path))
    with destination.open('xb') as target:
        try:
            with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path, name in zip(paths, FILES):
                    archive.write(path, 'CutCut/' + name)
        except BaseException:
            target.close(); destination.unlink(missing_ok=True); raise


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('用法：python3 prepare_release.py /保存位置/CutCut.zip')
    try: build(Path(sys.argv[1]).expanduser())
    except (ValueError, OSError) as exc: sys.exit('未生成发布包：' + str(exc))
    print('已生成发布包。请勿将自己的密钥提供给其他用户。')
