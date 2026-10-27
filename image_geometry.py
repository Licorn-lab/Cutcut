"""Non-destructive output geometry shared by browser previews and JPEG export."""
import math
import numpy as np
from PIL import Image

DEFAULT = {'turns': 0, 'flip': False, 'angle': 0, 'horizontal': 0, 'vertical': 0}


def validate_geometry(value):
    if value is None:return dict(DEFAULT)
    if not isinstance(value, dict) or set(value) - set(DEFAULT):raise ValueError('图片校正参数无效')
    result = dict(DEFAULT, **value)
    if type(result['turns']) is not int or not 0 <= result['turns'] <= 3:raise ValueError('旋转次数无效')
    if type(result['flip']) is not bool:raise ValueError('翻转参数无效')
    for key, limit in [('angle', 45), ('horizontal', 100), ('vertical', 100)]:
        number = result[key]
        if type(number) not in (int, float) or not math.isfinite(number) or abs(number) > limit:
            raise ValueError('图片校正范围无效')
        result[key] = round(number, 2)
    return result


def geometry_size(size, value):
    g = validate_geometry(value)
    w, h = size
    if g['turns'] % 2:w, h = h, w
    if g['angle']:
        angle = math.radians(g['angle']);c, s = abs(math.cos(angle)), abs(math.sin(angle))
        w, h = math.ceil(w*c+h*s), math.ceil(w*s+h*c)
    return int(w), int(h)


def apply_geometry(image, value):
    g = validate_geometry(value)
    if g == DEFAULT:return image
    image = image.convert('RGBA')
    if g['flip']:image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if g['turns']:
        image = image.transpose({1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180,
                                 3: Image.Transpose.ROTATE_90}[g['turns']])
    if g['angle']:
        w, h = image.size;ow, oh = geometry_size((w, h), dict(g, turns=0))
        angle = math.radians(g['angle']);c, s = math.cos(angle), math.sin(angle)
        # Positive angles turn clockwise, with a larger canvas to retain every corner.
        image = image.transform((ow, oh), Image.Transform.AFFINE,
                                (c, s, w/2-c*ow/2-s*oh/2, -s, c, h/2+s*ow/2-c*oh/2),
                                Image.Resampling.BICUBIC, fillcolor=(0, 0, 0, 0))
    if g['horizontal'] or g['vertical']:
        w, h = image.size
        normalize = np.array([[2/w, 0, -1], [0, 2/h, -1], [0, 0, 1.]])
        project = np.array([[1., 0, 0], [0, 1., 0], [g['horizontal']*.003, g['vertical']*.003, 1.]])
        corners = project @ np.array([[-1, 1, 1, -1], [-1, -1, 1, 1], [1, 1, 1, 1]])
        xy = corners[:2] / corners[2]
        lo, hi = xy.min(axis=1), xy.max(axis=1)
        fit = np.array([[w/(hi[0]-lo[0]), 0, -lo[0]*w/(hi[0]-lo[0])],
                        [0, h/(hi[1]-lo[1]), -lo[1]*h/(hi[1]-lo[1])], [0, 0, 1.]])
        inverse = np.linalg.inv(fit @ project @ normalize)
        inverse /= inverse[2, 2]
        image = image.transform((w, h), Image.Transform.PERSPECTIVE, tuple(inverse.ravel()[:8]),
                                Image.Resampling.BICUBIC, fillcolor=(0, 0, 0, 0))
    return image


def on_white(image):
    result = Image.new('RGBA', image.size, 'white')
    result.alpha_composite(image.convert('RGBA'))
    return result.convert('RGB')
