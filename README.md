# 视频日语字幕 → 简体中文 翻译工具（文字冒险游戏录屏）

把文字冒险类游戏（VN）录屏中的日语文字翻译为简体中文。文字只有两部分：**人名**（说话人标签）和**台词**，两者位置固定。

采用 **append 模式**：完全保留原视频画面，在底部增加一条加高条带，把翻译后的人名和台词放进条带里，实现中日对照，适合翻译校对。

## 核心流程

```
输入视频（命令行参数传入，如 0_1_1.mp4）
  ├─ 0. 定位区域   python run.py regions <视频>  （只需首次执行一次）
  │        自动检测出「人名区」和「台词区」两个矩形 → 写入 config.yaml（固定参数，以后所有视频共用）
  ├─ 1. OCR       按设定间隔抽帧 → 在人名区/台词区分别识别 → 字幕段打上 name/dialogue 标签
  ├─ 2. 翻译      人名与专有名词查 glossary.json 词典；台词走模型翻译（台词中的词典词会被占位保护）
  │               结果写入 translations.json（分 names/dialogues + 时间戳），可手工校对
  └─ 3. 渲染      原画面保留 + 底部加高条带绘制中文（人名左上对齐、台词居中）
       输出到 <视频名>_output/ 文件夹：
         output.mp4        最终视频（保留原音频）
         translations.json 该视频的翻译与时间戳
         <视频名>.mp4      渲染完成后原视频被移动到该文件夹
```

## 目录结构

| 路径 | 说明 |
|---|---|
| `run.py` | 命令入口（regions / ocr / translate / render / all / init） |
| `config.yaml` | 全部配置（区域、供应商、渲染模式等） |
| `.env` | 密钥环境变量（DeepSeek key），**勿提交/外泄** |
| `requirements.txt` | Python 依赖 |
| `core/` | 核心代码 |
| ├─ `regions.py` | 自动识别人名区/台词区（分带聚类，写入 config） |
| ├─ `ocr.py` | OCR 引擎封装 + 分区域抽帧识别 + 字幕段聚合/精修 |
| ├─ `translate.py` | 翻译供应商抽象 + 词典替换 + 缓存（含时间戳） |
| ├─ `render.py` | append 条带渲染 + 输出文件夹 + 流式编码 |
| ├─ `video.py` | 视频信息、ffmpeg 管道编码 |
| └─ `config.py` | 配置加载/默认值/.env |
| `glossary.json` | **翻译词典**：`names`（人名）+ `proper_nouns`（专有名词），填上日语→中文译名即可，翻译时直接查用 |
| `<视频名>_output/` | 该视频的全部文件：原视频 + output.mp4 + translations.json + segments.json |

> 处理完一个视频后，与其有关的所有文件都收进 `<视频名>_output/`，根目录保持整洁。处理下一个视频（如 `0_1_2.mp4`）时流程相同，产出进入 `0_1_2_output/`。

## 环境准备（一次性）

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py init      # 生成 config.yaml
```
在 `config.yaml` 填写 `translation.provider` 与 `api_key`（或 `.env` 的 `DEEPSEEK_API_KEY`）。
**视频路径一律通过命令行参数传入**（视频是编号名，如 `0_1_1.mp4`、`0_1_2.mp4`，放在工作目录根下即可），不写进 config.yaml。

## 使用步骤

```bash
# 0. 首次：自动定位人名区/台词区（位置固定则只需执行一次）
.venv/bin/python run.py regions 0_1_1.mp4

# 1+2+3. 一键全流程（输出在 0_1_1_output/，原视频随后移入该文件夹）
.venv/bin/python run.py all 0_1_1.mp4

# 或分步执行（便于核对中间结果）
.venv/bin/python run.py ocr 0_1_1.mp4      # ① 识别
.venv/bin/python run.py translate 0_1_1.mp4  # ② 翻译（可省略视频名，自动定位）
.venv/bin/python run.py render 0_1_1.mp4   # ③ 渲染（也可省略视频参数，自动用 OCR 结果里的视频）
```

> 提示：`translate` / `render` 不带视频参数时会自动从最近处理过的 `*_output/` 文件夹读取结果；原视频渲染完成后被移入输出文件夹，之后再渲染程序也会自动找到它。

**翻译词典（glossary.json）**：把日语人名/专有名词的中文译名填进去（例如 `"威厳のある女性": "威严的女性"`），再跑 `translate` 即生效：
- 人名（name 类文本）：整条命中直接采用词典译名
- 台词中的词典词：翻译前自动替换为占位符保护，保证 AI 不改写该译名，翻译后还原
- 词典里没填的词仍由模型翻译，人名会在终端提示你补填

**人工校对（如何修改翻译）**：翻译结果在 `<视频名>_output/translations.json`，结构清晰，直接改译文即可：
```json
{
  "names": { "威厳のある女性": "威严的女性" },
  "dialogues": {
    "まさか……あなたが私に銃を向けるなんて": "没想到……你竟然会用枪指着我"
  },
  "timestamps": { "names": {...}, "dialogues": { "まさか……": [0.0, 3.6] } }
}
```
- `names` / `dialogues`：**原文 → 译文** 的简单映射，改引号里的中文即可
- `timestamps`：每条原文首次/最后出现时间（秒），仅作参考，不用改
- 改完保存，运行 `run.py render 0_1_1.mp4` 重新生成视频即可生效

## 配置项速查（config.yaml）

| 字段 | 作用 |
|---|---|
| `ocr.name_region` / `ocr.dialogue_region` | 人名区/台词区（相对坐标 0~1），由 `regions` 自动检测；位置固定的视频共用即可，不准可手改 |
| `ocr.sample_step` | 每多少帧抽 1 帧识别（48 = 60fps 下约 800ms 一次） |
| `translation.provider` | `deepseek` / `openai` / `qwen` / `mock`（mock 免 key，用于流程验证） |
| `render.append_height` | 底部加高像素（默认 160） |
| `render.append_bg` | 条带底色：`auto`（匹配台词框暖灰，推荐）/ `black` / `#RRGGBB` |
| `render.show_name` | 是否显示翻译后的人名（默认 true） |
| `render.width` | 输出宽度，0=保持原分辨率 |
| `render.font_scale` | 全局字号 = 字幕框高度中位数 × 此系数 |
| `render.test_frames` | 调试用：只渲染前 N 帧（0=全部） |

## 常见问题

- **翻译缺 key**：`config.yaml` 填 `translation.api_key` 或 `.env` 设 `DEEPSEEK_API_KEY`
- **区域不准**：手动改 `ocr.name_region/dialogue_region` 后重跑 `ocr`；不同分辨率的视频区域会自动按比例换算
- **人名/专有名词译名不一致**：在 `glossary.json` 里统一填写后重跑 `translate`
- **个别翻译失败**：重跑 `translate` 会只补齐失败的条目（带重试）
- **密钥勿外泄**：`.env`、缓存、`*_output/` 已列入 `.gitignore`
