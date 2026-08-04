# 视频日语字幕 → 简体中文 翻译工具（文字冒险游戏录屏）

把文字冒险类游戏（VN）录屏中的日语**台词**翻译为简体中文，只在画面底部加高条带内显示翻译（**append 模式**）：完全保留原视频画面，在底部加一条暖灰条带、把翻译后的台词居中放入，实现中日对照，适合翻译校对。

只处理台词：人名/说话人标签不再单独识别与显示；台词中出现的角色名与专有名词通过 `glossary.json` 词典按既定译名替换（不在词典中的名字交给模型翻译）。

## 核心流程

```
输入视频（命令行参数传入，如 1.mp4）
  ├─ 0. 检测台词区  每个视频处理前自动检测（也可单独运行 run.py regions <视频>）
  │        抽帧 OCR → 按位置聚类出「台词区」→ 写入 config.yaml
  │        → 生成 4 张校验截图 <视频名>_output/region_check_*.png（绿框=检测到的台词区，供人工查验）
  ├─ 1. OCR       按设定间隔抽帧 → 只在台词区内识别 → 字幕段聚合/精修
  ├─ 2. 翻译      台词中的词典词(glossary.json)先占位保护；其余文本并发调用翻译模型（已关闭思考模式）
  │               结果写入 translations.json（原文+译文+时间戳整合在一条记录里），可手工校对
  └─ 3. 渲染      原画面保留 + 底部加高条带绘制中文（台词居中）
       输出到 <视频名>_output/ 文件夹：
         output.mp4        最终视频（保留原音频）
         translations.json 该视频的翻译与时间戳
         segments.json     OCR 缓存
         region_check_*.png 台词区校验截图
         <视频名>.mp4      渲染完成后原视频被移动到该文件夹
```

## 目录结构

| 路径 | 说明 |
|---|---|
| `run.py` | 命令入口（regions / ocr / translate / render / all / init） |
| `config.yaml` | 全部配置（供应商、渲染参数；台词区自动检测写入） |
| `.env` | 密钥环境变量（DeepSeek key），**勿提交/外泄** |
| `requirements.txt` | Python 依赖 |
| `core/` | 核心代码 |
| ├─ `regions.py` | 台词区自动检测 + 校验截图生成 |
| ├─ `ocr.py` | OCR 引擎封装 + 台词区抽帧识别 + 字幕段聚合/精修 |
| ├─ `translate.py` | 翻译供应商抽象（关闭思考模式、并发）+ 词典替换 + 缓存 |
| ├─ `render.py` | append 条带渲染 + 输出文件夹 + 流式编码 |
| ├─ `video.py` | 视频信息、ffmpeg 管道编码 |
| └─ `config.py` | 配置加载/默认值/.env |
| `glossary.json` | **翻译词典**：`names`（人名）+ `proper_nouns`（专有名词），台词中出现时按译名直接替换 |
| `<视频名>_output/` | 该视频的全部文件（原视频、成品、翻译、OCR缓存、校验截图） |

> 处理完一个视频后，与其有关的所有文件都收进 `<视频名>_output/`，根目录保持整洁。

## 环境准备（一次性）

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py init      # 生成 config.yaml
```
在 `config.yaml` 填写 `translation.provider` 与 `api_key`（或 `.env` 的 `DEEPSEEK_API_KEY`）。
**视频路径一律通过命令行参数传入**（放在工作目录根下即可），不写进 config.yaml。

## 使用步骤

```bash
# 一键全流程（自动检测台词区 → OCR → 翻译 → 渲染，输出在 1_output/）
.venv/bin/python run.py all 1.mp4

# 或分步执行
.venv/bin/python run.py regions 1.mp4        # ① 仅检测台词区 + 生成校验截图
.venv/bin/python run.py ocr 1.mp4            # ② 检测台词区 + 文字识别
.venv/bin/python run.py translate 1.mp4      # ③ 翻译（可省略视频名，自动定位）
.venv/bin/python run.py render 1.mp4         # ④ 渲染（也可省略视频参数）
```

> 校验截图生成在 `<视频名>_output/region_check_*.png`，请先打开确认绿框已框住台词区，再继续后续步骤。若不准，可手动改 `config.yaml` 的 `ocr.dialogue_region` 后重跑 `ocr`。

**翻译词典（glossary.json）**：把日语人名/专有名词的中文译名填进去（例如 `"純夏": "纯夏"`），再跑 `translate` 即生效：
- 台词中出现词典词时：翻译前自动替换为占位符保护，保证 AI 不改写该译名，翻译后还原
- 不在词典中的名字交给模型翻译

**人工校对（如何修改翻译）**：翻译结果在 `<视频名>_output/translations.json`，每条记录整合了原文、译文与时间戳：
```json
{
  "_格式说明": "每条记录为「原文 -> {translation, first_seen, last_seen}」，只改 translation 的值，其余字段勿动",
  "まさか……あなたが私に銃を向けるなんて": {
    "translation": "没想到……你竟然会用枪指着我",
    "first_seen": 0.0,
    "last_seen": 3.6
  }
}
```
- 改完保存，运行 `run.py render 1.mp4` 重新生成视频即可生效

## 配置项速查（config.yaml）

| 字段 | 作用 |
|---|---|
| `ocr.dialogue_region` | 台词区（相对坐标 0~1），每个视频处理前自动检测写入；不准可手改 |
| `ocr.sample_step` | 每多少帧抽 1 帧识别（48 = 60fps 下约 800ms 一次） |
| `translation.provider` | `deepseek` / `openai` / `qwen` / `mock`（mock 免 key，用于流程验证） |
| `translation.model` | 模型名，如 `deepseek-v4-flash`（程序已自动关闭思考模式以提速） |
| `render.append_height` | 底部加高像素（默认 160） |
| `render.append_bg` | 条带底色：`auto`（匹配台词框暖灰，推荐）/ `black` / `#RRGGBB` |
| `render.width` | 输出宽度，0=保持原分辨率 |
| `render.font_scale` | 全局字号 = 字幕框高度中位数 × 此系数 |
| `render.test_frames` | 调试用：只渲染前 N 帧（0=全部） |

## 常见问题

- **翻译缺 key**：`config.yaml` 填 `translation.api_key` 或 `.env` 设 `DEEPSEEK_API_KEY`
- **翻译慢**：确认 `translation.model` 正确（如 `deepseek-v4-flash`）；程序已禁用思考模式并启用 6 路并发，数百条台词通常 1~3 分钟完成
- **台词区不准**：查看 `region_check_*.png`，手动改 `ocr.dialogue_region` 后重跑 `ocr`
- **人名/专有名词译名不一致**：在 `glossary.json` 里统一填写后重跑 `translate`
- **个别翻译失败**：重跑 `translate` 会只补齐失败的条目（带重试）
- **密钥勿外泄**：`.env`、缓存、`*_output/` 已列入 `.gitignore`
