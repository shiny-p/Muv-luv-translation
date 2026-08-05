# 备份

本目录保存核心代码在历次优化前的版本，供回退参考。

| 文件 | 说明 |
|---|---|
| `regions.py.bak` | 台词区自动检测的原始版本（`BAND_GAP=35` 固定、min/max 边界、无分数/日文过滤） |
| `ocr.py.bak` | OCR 原始版本（无逐字显示模糊前缀合并） |

- 需要回退时：`cp backups/regions.py.bak core/regions.py`（同理 ocr.py），然后重跑 `regions`/`ocr` 验证。
- 当前版本的改进：
  - `regions.py`：检测阶段复用 `min_score`/`require_japanese` 过滤；`BAND_GAP` 按分辨率缩放（0.035×h）；相邻条带合并。
  - `ocr.py`：新增 `_typewriter_merge` 模糊前缀合并，解决游戏逐字显示字幕被采样成碎片/重复段的问题。
