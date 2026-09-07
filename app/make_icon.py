#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标 app/icon.ico(扁平摄像头风格,一次性运行)"""
import os
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# 深蓝圆角底
d.rounded_rectangle([10, 10, S - 10, S - 10], radius=52, fill=(23, 55, 94, 255),
                    outline=(70, 115, 165, 255), width=6)
# 白色机身
d.rounded_rectangle([34, 86, 222, 200], radius=26, fill=(245, 248, 252, 255))
# 镜头(外圈深、内芯浅蓝高光)
d.ellipse([118, 96, 206, 184], fill=(23, 55, 94, 255))
d.ellipse([136, 114, 188, 166], fill=(96, 165, 218, 255))
d.ellipse([148, 124, 166, 142], fill=(220, 238, 250, 255))
# 录制红点
d.ellipse([54, 104, 74, 124], fill=(214, 69, 69, 255))
# 底部支架线
d.rounded_rectangle([92, 214, 164, 228], radius=7, fill=(70, 115, 165, 255))

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
img.save(out, format="ICO", sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)])
img.save("/tmp/icon_preview.png")
print("icon written:", out, os.path.getsize(out), "bytes")
