# 视频日语字幕 → 简体中文 翻译工具（文字冒险游戏录屏）

把文字冒险类游戏（VN）录屏中的日语**台词**翻译为简体中文，只在画面底部加高条带内显示翻译（**append 模式**）：完全保留原视频画面，在底部加一条暖灰条带、把翻译后的台词居中放入，实现中日对照，适合翻译校对。

只处理台词：人名/说话人标签不再单独识别与显示；台词中出现的角色名与专有名词通过 `glossary.json` 词典按既定译名替换（不在词典中的名字交给模型翻译）。

> **人名/说话人标签区刻意不识别、不翻译**：区域检测会自动剔除顶部的人名短标签带，OCR 与翻译只针对台词区。若在校验截图或翻译结果中看到人名标签，说明台词区没检测干净，请先修正区域再继续。

## 核心流程

```
输入视频（命令行参数传入，如 1.mp4）
  ├─ 0. 检测台词区  首次处理视频时自动检测（也可单独运行 run.py regions <视频>）
  │        抽帧 OCR → 按位置聚类出「台词区」→ 写入 <视频名>_output/region.json
  │        → 生成 4 张校验截图 <视频名>_output/region_check_*.png（绿框=检测到的台词区，**必须人工确认绿框只框住台词、不含人名标签/HUD 杂字**）
  ├─ 1. OCR       读取该视频的 region.json → 按设定间隔抽帧 → 只在台词区内识别 → 字幕段聚合/精修
  │        → **OCR 完成后，智能体必须完整检查 `segments.json` 的全部文本。发现乱码、无意义符号等明显不属于正常文本的内容时，优先进行局部 OCR 重识别后再翻译；不要求理解或校对具体台词内容。**
  ├─ 2. 翻译      台词中的词典词(glossary.json)先占位保护；其余文本并发调用翻译模型（已关闭思考模式）
  │               结果写入 translations.json（原文+译文+时间戳整合在一条记录里），可手工校对
  │        → **翻译完成后，智能体必须随机抽检原文—译文对；发现乱码、无意义符号等明显不属于正常文本的内容时，必须重新发起翻译并再次抽检；不要求理解或评估具体内容的翻译质量。**
  └─ 3. 渲染      原画面保留 + 底部加高条带绘制中文（台词居中）
       输出到 <视频名>_output/ 文件夹：
         output.mp4        最终视频（保留原音频）
         translations.json 该视频的翻译与时间戳
         segments.json     OCR 缓存
         region.json       该视频的台词区（可手工编辑）
         region_check_*.png 台词区校验截图
         <视频名>.mp4      原视频副本（渲染完成后一并复制进该文件夹，原文件保留）
```

## 逐字显示（打字机）字幕的支持

游戏默认常为**逐字显示**：同一句台词逐字打出，停留一段时间后整句消失。此时固定间隔采样会捕捉到同一句的多个“局部文本”，形成前缀链（如 `続いてご紹介` → `続いてご紹介いたしますの` → …… → 完整句）。

程序内置**模糊前缀合并**（`ocr.py` 的 `_typewriter_merge`）：
- 时间相邻、位置相同（同一行）、且“短文本 ≈ 长文本开头”（容忍打字瞬间的 OCR 错字，按编辑距离判断）的片段会自动合并为**一条完整句**；
- 合并后文本取链中最完整的一句，时间取整条链的并集（第一字出现 → 整句清空），字幕显示时长更准确。

采样间隔建议：
- 保持 `ocr.sample_step = 48`（60fps 下约 800ms 一次）。逐字模式完整句在“停留期”必然被采到，配合模糊合并即可还原，无需加密。
- **不要低于 24**：过密采样（≈200~400ms）会捕捉到字符渲染中途的不稳定框（残影），合并救不回来，反而产生更多碎片并显著增加耗时。
- 若个别视频打字极快且停留极短导致某句始终只采到局部文本，可临时把 `sample_step` 降到 32 左右（仍不建议低于 24）。

## 目录结构

| 路径 | 说明 |
|---|---|
| `run.py` | 命令入口（regions / ocr / translate / render / all / init） |
| `config.yaml` | 全局配置（OCR、翻译供应商、渲染参数；不保存视频专属台词区） |
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
| `<视频名>_output/` | 该视频的全部文件（原视频副本、成品、翻译、OCR缓存、台词区、校验截图） |
| `backups/` | 程序完整备份（按时间戳归档全部源码与配置），需回退时把对应子目录内容复制回项目根即可 |

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
# 一键全流程（首次自动检测台词区 → OCR → 翻译 → 渲染，输出在 1_output/）
.venv/bin/python run.py all 1.mp4

# 或分步执行
.venv/bin/python run.py regions 1.mp4        # ① 仅检测台词区 + 生成校验截图 + 写入 region.json
.venv/bin/python run.py ocr 1.mp4            # ② 使用该视频的 region.json 做文字识别
.venv/bin/python run.py ocr-range 1.mp4 --segment 12  # 发现第 12 段错误时，仅重识别该段
.venv/bin/python run.py ocr-range 1.mp4 --start 123.4 --end 127.8  # 或按秒指定范围
.venv/bin/python run.py ocr 1.mp4 --redetect-region  # 忽略已有区域并重新检测
.venv/bin/python run.py ocr 1.mp4 --region 0.223,0.774,0.746,0.923  # 手工指定并保存区域
.venv/bin/python run.py translate 1.mp4      # ③ 翻译（可省略视频名，自动定位）
.venv/bin/python run.py translate 1.mp4 --force  # 抽检发现明显误译时，强制重新发起翻译
.venv/bin/python run.py render 1.mp4         # ④ 渲染（也可省略视频参数）
```

> 校验截图生成在 `<视频名>_output/region_check_*.png`，**首次处理或重新检测区域后必须打开检查**：绿框应只框住台词区——既不能漏掉台词，也不能框入顶部人名标签、右上角计时器等杂字。识别是否干净直接决定后续 OCR 与翻译质量。若不准，编辑该视频输出目录的 `region.json`，或用 `--region left,top,right,bottom` 保存手工区域后重跑 `ocr --force`；区域文件不会影响其他视频。

**台词区的保存与手工调整**：`<视频名>_output/region.json` 是该视频专属的台词区（`left`、`top`、`right`、`bottom` 均为 0～1 的相对坐标）。首次 `ocr`、`ocr-range` 或 `all` 在文件缺失时会自动检测并保存；以后会复用该文件，不会改写 `config.yaml`，不同视频互不影响。需要重新检测时添加 `--redetect-region`；需要人工微调时直接编辑该 JSON，或通过 `--region left,top,right,bottom` 保存。手动修改区域后请使用 `ocr --force` 重跑完整 OCR。

**OCR 完成后的全文检查与局部重识别**：智能体必须完整检查 `<视频名>_output/segments.json` 中每一条 `text`。这里的“明显错误”仅指乱码、无意义符号、无法构成正常文本的残片等，无需理解台词含义，也不要求判断一般错字、漏字或文本内容是否正确。发现此类问题时，不必重跑完整视频，优先使用 `ocr-range`：
- `--segment <id>` 使用 `segments.json` 每条记录的 `id`；也可用 `--start <秒> --end <秒>` 指定范围。
- 程序会在目标范围前后各额外扫描 1 秒，以保留逐字显示的上下文；仅替换目标时间范围内的字幕，不影响邻近台词。
- 局部 OCR 始终沿用 `ocr.sample_step`，默认 48 帧（60fps 下约 800ms），不增加采样频率。重识别后运行一次 `run.py translate <视频>`，新文本会自动补译，再执行 `render`。

> **局部重识别的注意事项（处理 03.mp4 时踩过的坑，务必先读）**：
> - `ocr-range` 是“整段替换”而不是逐字修补：只要旧段的存储时间范围与目标区间重叠，就会被整体丢弃，换成重扫（仍按 `sample_step` 粗采样）得到的结果。逐字显示下，段的存储时间是整条打字链的并集，往往比文字实际可见时间宽，因此重扫窗口内读到的可能是前后句，导致**合法台词被误删**（实测正确指定 `--segment` 也会一次丢 3 条）。
> - `--segment <id>` 的 id 在每次 `ocr-range` 后都会重排：连续多次重识别时**不要沿用上一次的旧 id**（实测按旧 id 执行会把另一个时间窗重扫一遍，一次丢 4 条）。每次执行前重新读取 `segments.json` 确认当前 id，或干脆用 `--start <秒> --end <秒>` 按秒定位。
> - 万一误删/误改：`run.py ocr <视频> --force` 重跑全量 OCR 即可恢复基线，再重新处理。
> - 更稳妥的修法：先用密集抽帧 OCR（每 0.3~0.5s 一帧）确认目标片段的真实文本，再直接手动清理 `segments.json`（常见问题亦允许手动清理）。OCR 对个别字符会稳定误读（如「えっと」→「¿Eと」、「いらっしゃいませー！」→「i-开¥1747c917.」），人工确认时以上下文 + 多帧结果为准。改完运行 `translate` 自动补译，再 `render`。

**翻译完成后的随机抽检与重译**：每次 `translate` 完成时，程序会随机输出最多 8 条“日文原文—中文译文”供智能体检查。此处的“明显错误”也仅指乱码、无意义符号、无法构成正常文本的内容；无需理解原文或判断译文的语义、语气、术语和通顺程度。发现此类问题时，必须运行 `run.py translate <视频> --force` 重新发起翻译，完成后再次抽检。`--force` 会重译全部非词典锁定的台词；`glossary.json` 中的既定译名仍会保留。

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
| `<视频名>_output/region.json` | 台词区（相对坐标 0~1）；首次自动检测写入，不准可手改，不影响其他视频 |
| `ocr.sample_step` | 每多少帧抽 1 帧识别（默认 48 ≈ 60fps 下 800ms 一次；**不要低于 24**，过密会捕捉逐字渲染中途的不稳定框） |
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
- **台词区不准 / 识别不干净**：查看 `region_check_*.png`，确认绿框只框住台词；若框入了人名标签、计时器等杂字，手动收紧该视频 `region.json` 或使用 `--region` 后重跑 `ocr --force`
- **人名/说话人标签被识别进去了**：本工具不识别、不翻译人名标签。出现说明台词区框得太松，收紧该视频区域后重跑 `ocr --force`
- **字幕逐字显示导致碎片/重复**：程序已自动用模糊前缀合并把打字前缀链合并为完整句；保持 `ocr.sample_step` ≥ 24 即可。若仍有局部文本残留，可临时把 `sample_step` 调到 32 左右或手动清理 `segments.json`
- **人名/专有名词译名不一致**：在 `glossary.json` 里统一填写后重跑 `translate`
- **个别翻译失败**：重跑 `translate` 会只补齐失败的条目（带重试）
- **ocr-range 误删邻句 / 段号过期**：`ocr-range` 是整段替换，逐字显示下段的存储时间并集可能与前后句重叠，重扫窗口又按 `sample_step` 粗采样，容易把合法台词一并删掉；且每次重识别后 `--segment` 的 id 会重排，沿用旧 id 会重识别错误时间窗。处理方式：误删后 `ocr --force` 恢复基线；修单句优先用 `--start/--end` 按秒定位，重扫后逐条核对，或用密集抽帧 OCR 确认真实文本后手动清理 `segments.json`
- **校验截图没覆盖到的 HUD 文字混入台词**：`region_check_*.png` 只抽查 4 帧，其他时段（如章节选择/画廊画面）落在台词区内的 HUD 计数（如「残り:1/1」）仍可能被识别。全文检查发现这类非台词段，直接从 `segments.json` 删除即可
- **音画不同步（VFR 源视频）**：本工具按恒定帧率（CFR）假设处理视频。若源视频是可变帧率（VFR，部分安卓录屏即使选了 60fps 仍是 VFR），渲染后画面会相对声音逐渐漂移、结尾音频还可能被截断。建议录屏时固定高帧率（如 XRecorder / AZ 选 60fps 并关闭「可变帧率」开关）；最稳妥的做法是进管线前先转成严格 CFR：
  ```bash
  ffmpeg -i input.mp4 -vf "fps=60,setpts=N/(60*TB)" -af "aresample=async=1" -c:v libx264 -crf 18 -c:a aac -movflags +faststart output_cfr.mp4
  ```
  转完后可用 `ffmpeg -i output_cfr.mp4 -map 0:v:0 -vf showinfo -f null - 2>&1 | grep -o pts_time:[0-9.]*` 检查帧间隔是否恒为 1/60。
- **密钥勿外泄**：`.env`、缓存、`*_output/` 已列入 `.gitignore`
