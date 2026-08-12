# 视频日语字幕 → 简体中文 翻译工具（文字冒险游戏录屏）

把录屏中的日语**台词**翻译为简体中文，在画面底部叠加一条暖灰条带、居中显示中文译文（**append 模式**）：完全保留原视频画面与音频，实现中日对照，适合翻译校对。

主要功能：

- **只翻译台词**：人名/说话人标签不单独识别与显示；台词中的角色名、专有名词按 `glossary.json` 词典译名替换，不在词典中的名字交给模型翻译。
- **自动检测台词区**：按台词位置自动框定识别区域，并生成校验截图供人工确认。
- **支持逐字打字机显示**：自动合并前缀链，还原完整台词与准确时间。
- **AI 翻译**：DeepSeek / OpenAI / Qwen 可选；默认**剧本化翻译**（整段剧本 + `glossary.json` 作提示词，模型按上下文翻译），分块并发，关闭思考模式。
- **OCR 多进程并行**（`--jobs`）：多核机器上显著缩短识别耗时。

> **人名/说话人标签区刻意不识别、不翻译**：区域检测会自动剔除顶部的人名短标签带。若在校验截图或翻译结果中看到人名标签，说明台词区没检测干净，请先修正区域再继续。

## 环境准备（一次性）

> 在恒源云实例上部署：按 `恒源云部署指南.md` 操作（脚本 `scripts/init_instance.sh` 会自动装依赖、拉取代码、写 GPU 配置并验证）。本地 macOS 则按下面步骤。


```bash
python3.12 -m venv .venv        # 需要 Python 3.12（macOS 可用 /opt/homebrew/bin/python3.12）
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py init    # 生成 config.yaml
```

- 在 `config.yaml` 填写 `translation.provider` 与 `api_key`（或 `.env` 的 `DEEPSEEK_API_KEY`）。
- 视频路径一律通过命令行参数传入（放在工作目录根下即可），**不写进 config.yaml**。
- 下文命令以 `.venv/bin/python run.py` 为例；激活虚拟环境后可直接写 `python run.py`。

## 使用步骤

```bash
# 一键全流程（自动转 CFR → 检测台词区 → OCR → 翻译 → 渲染，输出在 1_output/）
.venv/bin/python run.py all 1.mp4

> ⚠️ **处理规则（必须遵守）**：除非用户明确命令使用 `all`，否则**禁止一键全流程**。必须依照「核心流程」逐步执行：`cfr` → `regions`→ `ocr`（完成后全文检查 `segments.json`）→ `translate`（完成后全量扫描 `translations.json`）→ `render`。**每个检查点通过后才可进入下一步**，避免翻译/识别出错后重新渲染。

# 或分步执行
.venv/bin/python run.py cfr 1.mp4            # ① 转恒定帧率(CFR)，CFR 放入 1_output/（默认删除源视频，见 cfr.keep_source）
.venv/bin/python run.py regions 1.mp4        # ② 检测台词区 + 生成校验截图 + 写入 region.json
.venv/bin/python run.py ocr 1.mp4            # ③ 按 region.json 做台词区文字识别
.venv/bin/python run.py ocr-range 1.mp4 --segment 12  # ③ 局部重识别：仅重做第 12 段（或用 --start/--end 按秒定位）
.venv/bin/python run.py ocr 1.mp4 --redetect-region   # ③ 忽略已有区域，重新检测
.venv/bin/python run.py ocr 1.mp4 --region 0.223,0.774,0.746,0.923  # ③ 手工指定并保存区域
.venv/bin/python run.py translate 1.mp4      # ④ 翻译（可省略视频名，自动定位）
.venv/bin/python run.py translate 1.mp4 --force  # ④ 全量扫描/人工复核发现问题时，强制重新翻译
.venv/bin/python run.py render 1.mp4         # ⑤ 渲染（也可省略视频参数）
```


## 核心流程

```
输入视频（命令行参数传入，如 1.mp4）
  ├─ 0. 转恒定帧率(CFR)  用 ffmpeg 把源视频转成严格恒定帧率（`run.py cfr <视频>`；`all` 自动执行）
  │        CFR 视频放入 <视频名>_output/，工作区根目录不残留视频；按 `cfr.keep_source` 决定源视频去留
  │        （默认 false=CFR 转换成功后删除源视频、保留 CFR，节省磁盘；源视频保留在本地可随时重传）
  │        目标帧率见 `cfr.fps`（0=自动取 `render.fps`，否则源视频帧率四舍五入）
  ├─ 1. 检测台词区  首次处理视频时自动检测（也可单独运行 `run.py regions <视频>`）
  │        抽帧 OCR → 按位置聚类出「台词区」→ 写入 <视频名>_output/region.json
  │        → 生成 4 张校验截图 <视频名>_output/region_check_*.png（绿框=检测到的台词区，**必须确认绿框只框住台词、不含人名标签/HUD 杂字**）
  ├─ 2. OCR       读取该视频的 region.json → 按设定间隔抽帧 → 只在台词区内识别 → 字幕段聚合/精修
  │        → **OCR 完成后，智能体必须完整检查 `segments.json` 的全部文本。发现乱码、无意义符号等明显不属于正常文本的内容时，优先进行局部 OCR 重识别后再翻译；不要求理解或校对具体台词内容。**
  ├─ 3. 翻译      按时间序把台词拼接成剧本，`glossary.json` 作为系统提示词，模型按整段上下文翻译（分块并发、关闭思考模式）
  │               结果写入 translations.json（原文+译文+时间戳整合在一条记录里），可手工校对
  │        → **翻译完成后，程序对全部译文做全量扫描（规则：翻译文本应大部分为中文）；凡译文仍含日文假名、为空或含乱码的条目，自动重新翻译（最多 2 轮），并列出剩余可疑条目供智能体复核；不要求理解或评估具体内容的翻译质量。**
  └─ 4. 渲染      原画面保留 + 底部加高条带绘制中文（台词居中）
       输出到 <视频名>_output/ 文件夹：
         <视频名>.mp4      源视频（第0步移入；cfr.keep_source=false 时转换成功后已删除）
         <视频名>_cfr.mp4  恒定帧率转换后的视频（后续 OCR/翻译/渲染基于它）
         output.mp4        最终视频（保留原音频）
         translations.json 该视频的翻译与时间戳
         segments.json     OCR 缓存
         region.json       该视频的台词区（可手工编辑）
         region_check_*.png 台词区校验截图
```
> 处理完一个视频后，与其有关的所有文件都收进 `<视频名>_output/`，根目录保持整洁。
## 配置项速查（config.yaml）

| 字段 | 作用 |
|---|---|
| `<视频名>_output/region.json` | 台词区（相对坐标 0~1）；首次自动检测写入，不准可手改，不影响其他视频 |
| `region.fixed` | 固定台词区 `[left,top,right,bottom]`；非空时**不再调用自动检测**、直接应用（仍生成校验截图），单视频仍可用 region.json / `--region` 覆盖 |
| `ocr.sample_step` | 每多少帧抽 1 帧识别（默认 48 ≈ 60fps 下 800ms 一次；**不要低于 24**，过密会捕捉逐字渲染中途的不稳定框） |
| `ocr.use_gpu` | 用 onnxruntime-gpu(CUDA) 跑 OCR（需 NVIDIA 显卡；安装 `pip install onnxruntime-gpu`） |
| `cfr.fps` | CFR 目标帧率，0=自动（先取 `render.fps`，否则源视频帧率四舍五入） |
| `video.encoder` | 视频编码（CFR/渲染共用）：`nvenc`（GPU，需系统 ffmpeg 含 h264_nvenc）/ `x264`（CPU；若需 CUDA 硬解请同时把 `video.ffmpeg` 设为系统 ffmpeg） |
| `video.ffmpeg` | 自定义 ffmpeg 可执行文件路径；留空自动（x264 用内置 ffmpeg；**nvenc 或 hwaccel=cuda 时建议显式设为系统 ffmpeg**，如 `/usr/bin/ffmpeg`） |
| `video.hwaccel` | 解码硬加速：`""`=CPU 解码；`cuda`=用 NVDEC/CUDA 硬解源帧（需 NVIDIA 显卡 + 系统 ffmpeg，渲染/CFR 显著提速） |
| `cfr.crf` / `cfr.preset` | CFR 转换质量/速度（crf 默认 18；preset 默认 fast，仓库 config.yaml 已改为 veryfast 提速） |
| `cfr.suffix` | CFR 文件名后缀（默认 `_cfr`，即 `<视频名>_cfr.mp4`） |
| `cfr.keep_source` | CFR 转换成功后是否保留源视频（默认 `false`=删除源视频、保留 CFR，省空间；`true`=都保留） |
| `translation.provider` | `deepseek` / `openai` / `qwen` / `mock`（mock 免 key，用于流程验证） |
| `translation.model` | 模型名，如 `deepseek-v4-flash`（程序已自动关闭思考模式以提速）；切千问 DashScope 可用 `deepseek-v4-flash-0731` |
| `render.append_height` | 底部加高像素（默认 160） |
| `render.append_bg` | 条带底色：`auto`（匹配台词框暖灰，推荐）/ `black` / `#RRGGBB` |
| `render.width` | 输出宽度，0=保持原分辨率（仓库 config.yaml 已设为 1920；性能不足可调低）；**输出宽高自动取偶数**（H.264 要求，如 1920×944） |
| `render.preset` / `render.crf` | 渲染编码速度/质量（仓库 config.yaml 已设为 veryfast / 18） |
| `render.font_scale` | 全局字号 = 字幕框高度中位数 × 此系数 |
| `render.test_frames` | 调试用：只渲染前 N 帧（0=全部） |

## 目录结构

| 路径 | 说明 |
|---|---|
| `run.py` | 命令入口（cfr / regions / ocr / translate / render / all / init） |
| `config.yaml` | 全局配置（OCR、翻译供应商、渲染参数；不保存视频专属台词区） |
| `.env` | 密钥环境变量（DeepSeek key），**勿提交/外泄** |
| `requirements.txt` | Python 依赖 |
| `core/` | 核心代码 |
| ├─ `cfr.py` | 第0步：ffmpeg 恒定帧率(CFR)转换；默认删源视频保留 CFR（`cfr.keep_source`） |
| ├─ `regions.py` | 台词区自动检测 + 校验截图生成 |
| ├─ `ocr.py` | OCR 引擎封装 + 台词区抽帧识别 + 字幕段聚合/精修 |
| ├─ `translate.py` | 翻译供应商抽象（关闭思考模式、并发）+ 词典替换 + 缓存 |
| ├─ `render.py` | append 条带渲染 + 输出文件夹 + 流式编码 |
| ├─ `video.py` | 视频信息、ffmpeg 管道编码 |
| └─ `config.py` | 配置加载/默认值/.env |
| `glossary.json` | **翻译词典**：`names`（人名）+ `proper_nouns`（专有名词），台词中出现时按译名直接替换 |
| `<视频名>_output/` | 该视频的全部文件（CFR 视频、成品、翻译、OCR缓存、台词区、校验截图；源视频按 `cfr.keep_source` 决定去留） |
| `backups/` | 程序完整备份（按时间戳归档全部源码与配置），需回退时把对应子目录内容复制回项目根即可 |
| `tools/region_selector/` | 手动框选台词区工具（最后手段）：浏览器打开 `selector.html` 拖框选台词区并输出归一化坐标；示例画面在 `frames/`（已 gitignore） |


## 逐字显示模式应对和采样建议

游戏默认常为**逐字显示**：同一句台词逐字打出，停留一段时间后整句消失。此时固定间隔采样会捕捉到同一句的多个“局部文本”，形成前缀链（如 `続いてご紹介` → `続いてご紹介いたしますの` → …… → 完整句）。

程序内置**模糊前缀合并**（`ocr.py` 的 `_typewriter_merge`）：
- 时间相邻、位置相同（同一行）、且“短文本 ≈ 长文本开头”（容忍打字瞬间的 OCR 错字，按编辑距离判断）的片段会自动合并为**一条完整句**；
- 合并后文本取链中最完整的一句，时间取整条链的并集（第一字出现 → 整句清空），字幕显示时长更准确。

采样间隔建议：
- 保持 `ocr.sample_step = 48`（60fps 下约 800ms 一次）。逐字模式完整句在“停留期”必然被采到，配合模糊合并即可还原，**无需提高采样密度**。
- **不要低于 24**：过密采样（≈200~400ms）会捕捉到字符渲染中途的不稳定框（残影），合并救不回来，反而产生更多碎片并显著增加耗时。
- 若个别视频打字极快且停留极短导致某句始终只采到局部文本，可临时把 `sample_step` 降到 32 左右（仍不建议低于 24）。

## 台词区域识别

### 固定台词区（默认，不调用自动检测）

仓库 `config.yaml` 的 `region.fixed` 已设为固定台词区 `[0.231, 0.773, 0.767, 0.959]`（经多个视频确认通用）。配置后：

- **不再调用自动检测函数**（`detect_dialogue_region` 保留，仅未配置固定区域时兜底）；
- 仍会生成 `region_check_*.png` 校验截图供人工确认；固定区域已确认稳定后，**处理时无需逐次展示校验图，直接继续后续步骤**（如需查看可要求展示，发现问题则人工喊停）；
- 单视频仍可用该视频的 `<视频名>_output/region.json` 或 `--region` 覆盖。

### 校验截图

`<视频名>_output/region_check_*.png` 是检测出的台词区校验截图（绿框），**首次处理或重新检测区域后必须打开检查**：绿框应只框住台词区——既不能漏掉台词，也不能框入顶部人名标签、右上角计时器等杂字。识别是否干净直接决定后续 OCR 与翻译质量。

**执行规则（处理方必须遵守）**：展示后**直接继续 OCR 等后续流程，无需等待人工确认**——除非人工主动指出区域不准并中止，否则按流程执行；若人工指出不准，按下面方法修正区域后重跑 `ocr --force`。

若不准，编辑该视频输出目录的 `region.json`，或用 `--region left,top,right,bottom` 保存手工区域后重跑 `ocr --force`；区域文件不会影响其他视频。

### 台词区的保存与手工调整

`<视频名>_output/region.json` 是该视频专属的台词区（`left`、`top`、`right`、`bottom` 均为 0～1 的相对坐标）：

- 首次 `ocr`、`ocr-range` 或 `all` 在文件缺失时会自动检测并保存；以后会复用该文件，不会改写 `config.yaml`，不同视频互不影响。
- 需要重新检测时添加 `--redetect-region`；需要人工微调时直接编辑该 JSON，或通过 `--region left,top,right,bottom` 保存。
- 手动修改区域后请使用 `ocr --force` 重跑完整 OCR。

### 由人类手动框选台词区（最后手段）

当自动检测不准、且智能体反复调整也无法纠错时（典型：说话人名嵌在对话框顶部的游戏，算法无法剔除框内人名行），可由人类手工指定台词区：

1. 用浏览器打开 `tools/region_selector/selector.html`（附 4 张示例画面，也可直接把任意截图拖进页面）；
2. 按住鼠标左键拖一个矩形，**只框住台词正文**（不要框顶部说话人名、不要漏台词），切换几个示例画面确认；
3. 点击「复制坐标」，得到归一化坐标 `left,top,right,bottom`（0~1）；
4. 写入该视频的 `<视频名>_output/region.json`，或直接执行：

   `.venv/bin/python run.py ocr <视频> --region left,top,right,bottom`（会自动保存 region.json）

5. 重跑 `.venv/bin/python run.py ocr <视频> --force` → `.venv/bin/python run.py translate <视频>` → `.venv/bin/python run.py render <视频>`。

> 示例画面帧存放在 `tools/region_selector/frames/`（已 gitignore，不入库）；工具本身支持拖入任意分辨率截图，坐标按原图尺寸归一化。

## OCR

### 加速选项（--jobs）

OCR 是整条流水线中最耗时的一步，现已支持多进程并行抽帧识别：

- `.venv/bin/python run.py ocr 1.mp4 --jobs 4`：用 4 个进程并行 OCR（每个进程约 2 个推理线程）
- `.venv/bin/python run.py all 1.mp4 --jobs 4`：整条流水线同样生效
- `.venv/bin/python run.py ocr-range 1.mp4 --segment 12 --jobs 4`：局部重识别同样生效
- 不传 `--jobs` 时自动按 CPU 核数选择：≤2 核不并行；否则取核数一半（上限 8）。多核机器上可自行调大（如 `--jobs 8`）
- 并行只影响耗时，不影响结果：每帧的识别逻辑与串行完全一致，抽样帧与合并规则不变

> 提示：在核数多的服务器（如 8C16G ECS）上，`--jobs` 的收益比笔记本更大，因为云 CPU 单核较弱、多进程并行能更好吃满多核。

### GPU 加速（可选）

需要 NVIDIA 显卡与驱动：

1. 安装 GPU 依赖：`pip install onnxruntime-gpu`（会替换 CPU 版 onnxruntime），并安装含 `h264_nvenc` 的系统 ffmpeg（如 Ubuntu `apt install ffmpeg`，用 `ffmpeg -encoders | grep nvenc` 验证）；
2. `config.yaml` 设置 `ocr.use_gpu: true`、`video.encoder: nvenc`、`video.hwaccel: cuda`（`video.ffmpeg` 留空会自动使用系统 ffmpeg）；
3. OCR 建议 `--jobs 1~2`（每个进程占用独立 CUDA 显存，过大可能爆显存）；渲染/CFR 用 NVENC 编码 + NVDEC 硬解，速度数倍于 CPU 编解码。

> GPU 只影响速度，不影响结果；没有 NVIDIA 显卡时保持默认 `use_gpu: false`、`encoder: x264` 即可。

### OCR 完成后的全文检查与局部重识别

智能体必须完整检查 `<视频名>_output/segments.json` 中每一条 `text`。这里的“明显错误”仅指乱码、无意义符号、无法构成正常文本的残片等，无需理解台词含义，也不要求判断一般错字、漏字或文本内容是否正确。发现此类问题时，不必重跑完整视频，优先使用 `ocr-range`：

- `--segment <id>` 使用 `segments.json` 每条记录的 `id`；也可用 `--start <秒> --end <秒>` 指定范围。
- 程序会在目标范围前后各额外扫描 1 秒，以保留逐字显示的上下文；仅替换目标时间范围内的字幕，不影响邻近台词。
- 局部 OCR 始终沿用 `ocr.sample_step`，默认 48 帧（60fps 下约 800ms），不增加采样频率。重识别后运行一次 `run.py translate <视频>`，新文本会自动补译，再执行 `render`。

> **局部重识别的注意事项（踩过的坑，务必先读）**：
> - `ocr-range` 是“整段替换”而不是逐字修补：只要旧段的存储时间范围与目标区间重叠，就会被整体丢弃，换成重扫（仍按 `sample_step` 粗采样）得到的结果。逐字显示下，段的存储时间是整条打字链的并集，往往比文字实际可见时间宽，因此重扫窗口内读到的可能是前后句，导致**合法台词被误删**（实测正确指定 `--segment` 也会一次丢 3 条）。
> - `--segment <id>` 的 id 在每次 `ocr-range` 后都会重排：连续多次重识别时**不要沿用上一次的旧 id**（实测按旧 id 执行会把另一个时间窗重扫一遍，一次丢 4 条）。每次执行前重新读取 `segments.json` 确认当前 id，或干脆用 `--start <秒> --end <秒>` 按秒定位。
> - 万一误删/误改：`run.py ocr <视频> --force` 重跑全量 OCR 即可恢复基线，再重新处理。
> - 更稳妥的修法：先用密集抽帧 OCR（每 0.3~0.5s 一帧）确认目标片段的真实文本，再直接手动清理 `segments.json`（常见问题亦允许手动清理）。OCR 对个别字符会稳定误读（如「えっと」→「¿Eと」、「いらっしゃいませー！」→「i-开¥1747c917.」），人工确认时以上下文 + 多帧结果为准。改完运行 `translate` 自动补译，再 `render`。

## 翻译

默认采用**剧本化翻译**（`translation.mode: script`）：把 segments 按时间序拼接成剧本，
并把 `glossary.json` 作为系统提示词交给模型，让模型**按整段上下文翻译**（而非逐行孤立翻译）。

- 优点：人名/专有名词按上下文正确处理（如 `める` 在 `私とめるは友達だ` 译成"梅露"，而 `決める` 仍是"决定"），无需脆弱的机械占位替换；语句更连贯、人名译法更统一；
- 实现：剧本按 `translation.script_chunk_lines`（默认 50 行/块）切块、4 路并发调用模型，返回按 `[行号] 译文` 解析回填；解析失败的行自动回落逐行翻译补齐；
- 预计耗时：每部视频约 1~3 分钟（取决于台词量，实测约 90 秒/200+ 条）；
- 若某次想用旧的逐行模式，把 `translation.mode` 改为 `line` 即可。

### 翻译完成后的全量扫描与自动重译

每次 `translate` 完成后，程序会对**全部译文**做全量扫描（而非仅随机抽几条）：

- **规则（必须遵守）**：翻译文本应大部分为中文。译文仍含日文假名（平假名/片假名/长音符）、为空、或含乱码/异常字符的条目，均判定为可疑。
- 对「残留日语 / 译文为空」的可疑条目，程序会用更严格的重译指令**自动重新翻译**（最多 2 轮）；重译后仍可疑的，会列在扫描报告中。
- 扫描报告列出剩余可疑条目（原因—原文—译文），智能体只需复核这些条目；**无需理解原文或判断译文的语义、语气、术语和通顺程度**（语义级校对属人工或更强模型范畴）。
- 若人工复核仍不通过，运行 `run.py translate <视频> --force` 强制重译全部非词典锁定的台词，完成后会再次自动扫描；`glossary.json` 中的既定译名仍会保留。

> 说明：日文假名是中文中不会出现的字符，因此「译文含假名」是“未翻译/残留日语”的可靠信号；纯汉字组成的日语句（如「本日晴天」）与中文无法区分，不在自动判定范围内。

### 翻译词典（glossary.json）

把日语人名/专有名词的中文译名填进去（例如 `"純夏": "纯夏"`），再跑 `translate` 即生效：

- 台词中出现词典词时：翻译前自动替换为占位符保护，保证 AI 不改写该译名，翻译后还原；
- 词典词**只在独立成词时替换**（左邻不是假名/汉字，即词首、标点后），避免污染长词内部的子串（如 `決める` 里的 `める` 不会被当成角色名 `梅露`）；短名与全名可同时收录（如 `める` 与 `桃園める`），长条目优先；
- 不在词典中的名字交给模型翻译。

### 人工校对（如何修改翻译）

翻译结果在 `<视频名>_output/translations.json`，每条记录整合了原文、译文与时间戳：

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

- 改完保存，运行 `run.py render 1.mp4` 重新生成视频即可生效。

## 并行处理与调度建议（实测经验）

- **渲染视频 A 的同时，尝试并行处理视频 B 的 CFR/OCR/翻译**：渲染主要占用 NVENC 编码 + CPU 画字幕；CFR 走 NVENC（独立编码会话，实测对渲染影响小）；OCR 走 GPU 推理（可错峰）；翻译走 API（不占实例算力）。实测这种调度把整批总耗时省了约 1/3。
- **OCR 多进程 `--jobs`**：并行抽帧识别，结果与串行完全一致；多核机器上收益明显。
- **CPU 核数足够时并行跑 2 个渲染**：渲染是 CPU 密集（OpenCV/PIL 逐帧画字幕），也是全流程最耗时的一步（1920×944@60fps 约 45~58 帧/秒，单视频 10~15 分钟）。消费级显卡 NVENC 并发会话约 3 路，**最多并行 2 路渲染**（2 渲染 + 1 CFR 会踩线）。

- **`gpushare-cli` 上传百度一次只跑一个**：两个上传并发会导致其本地数据库锁冲突直接 panic 崩溃（实测多洛缇雅上传因此失败一次，重传才成功）；上传进行中也**不要执行 `baidu ls`**（同样会 panic）。上传完成后用 `baidu ls` 核对文件确实在网盘（实测曾出现"报上传完成但实际未进网盘"，需重传）。

### 并行度实测数据（RTX 3090 + 8 vCPU 实例）

- OCR `--jobs 2` 约 4 分钟，`--jobs 6` 约 3.5 分钟——只快约 12%。**OCR 瓶颈不是进程数**，而是受 CPU 核数上限、逐帧 GPU 推理、磁盘读帧限制。
- GPU 计算利用率约 30% 是正常的：NVENC/NVDEC 是独立硬件引擎，不占通用 CUDA 核心；OCR 模型小、逐帧推理是延迟敏感型；渲染主要耗 CPU。

>并行处理受到实例配置影响极大，必须结合实例配置选择并行处理方式

### 实例配置建议

- 本管线 GPU 需求主要是**硬件编码（NVENC）+ 轻量 OCR 推理**，优先保证 NVENC 与 CPU 核数，显卡算力不必顶级（如 4060 Ti / 3070 级别足够）。**若镜像驱动对某型号显卡的 NVENC 支持不全**（实测 Blackwell 5060 Ti 在部分镜像上 NVENC 不可用），可回退 `video.encoder: x264` + `video.ffmpeg: /usr/bin/ffmpeg`，多核 CPU 实例下渲染仍可达 50+ 帧/秒。
- **CPU 核数 ≥8（越多越好）**：用于并行渲染与 OCR 多进程；单核性能影响单视频渲染速度。
- 显存 ≥8GB（OCR `--jobs 2~3` 够用）；内存 ≥16GB（渲染要读 3.5~5GB 的 CFR 文件）。
- 上传速度受**实例出口带宽**限制（实测约 4.5MB/s），会员权益只影响下载端；实例→网盘上传换配置也不会更快。上传仍首选百度（实测阿里云盘从实例上传仅约 0.5MB/s）。

## 成品交付（可选：整文件夹上传网盘/OSS）

处理完成后建议把**整个 `<视频名>_output/` 文件夹**打成 zip 上传百度网盘/阿里云盘/恒源云 OSS（不只是成品视频），
这样 translations.json / segments.json / region.json 也都在手边，后续要改 JSON 不用整条流水线重跑。

```bash
# 实例上：把整个输出文件夹打成 zip（store 模式，视频不压缩、秒级完成）
cd /hy-tmp && zip -0 -q -r <视频名>_output.zip <视频名>_output/

# 上传到百度网盘（需先在恒源云控制台授权百度网盘账号，见官方「公共网盘」文档）
gpushare-cli login -u <恒源云账号> -p <密码>
gpushare-cli baidu up /hy-tmp/<视频名>_output.zip /MuvLuv_成品/

# 或上传到恒源云 OSS（需先 oss login）
oss login
oss cp /hy-tmp/<视频名>_output.zip oss://<视频名>_output.zip
```

- **上传前先测速选优**：百度网盘/阿里云盘/OSS 的实际传输速度受账号权益、实例出口带宽与时段影响波动很大（实测 OSS 可到数十 MB/s、百度非会员约 10MB/s，且不同时段可能反转）。先用小文件（如 100MB）分别对 `gpushare-cli baidu up`、`gpushare-cli ali up` 与 `oss cp` 各测一次传输速度，选择最快的方式正式上传。
- 上传速度因账号权益而异：百度非会员上限约 10MB/s，阿里云盘一般不限速（推荐，需另行授权），OSS 通常更快；**一切以实测为准**。
- `gpushare-cli` 只支持上传单个文件，所以先打成 zip；目前**只能在实例上执行**


## 常见问题

- **翻译缺 key**：`config.yaml` 填 `translation.api_key` 或 `.env` 设 `DEEPSEEK_API_KEY`（DeepSeek） / `DASHSCOPE_API_KEY`（千问）
- **切换翻译供应商**：改 `config.yaml` 的 `translation.provider/base_url/api_key_env/model` 即可，多个 key 可同时保留在 `.env` 随时切换（如 deepseek 用 `deepseek-v4-flash`，千问 DashScope 用 `deepseek-v4-flash-0731`）
- **翻译慢**：确认 `translation.model` 正确（如 `deepseek-v4-flash`）；默认剧本化翻译（分块并发、关闭思考模式），数百条台词通常 1~3 分钟完成
- **台词区不准 / 识别不干净**：查看 `region_check_*.png`，确认绿框只框住台词；若框入了人名标签、计时器等杂字，手动收紧该视频 `region.json` 或使用 `--region` 后重跑 `ocr --force`；若自动检测始终不准（如人名嵌在对话框顶部的游戏），用 `tools/region_selector/selector.html` 手动框选（见「台词区域识别」）
- **人名/说话人标签被识别进去了**：本工具不识别、不翻译人名标签。出现说明台词区框得太松，收紧该视频区域后重跑 `ocr --force`
- **字幕逐字显示导致碎片/重复**：程序已自动用模糊前缀合并把打字前缀链合并为完整句；保持 `ocr.sample_step` ≥ 24 即可。若仍有局部文本残留，可临时把 `sample_step` 调到 32 左右或手动清理 `segments.json`
- **人名/专有名词译名不一致**：在 `glossary.json` 里统一填写后重跑 `translate`
- **个别翻译失败**：重跑 `translate` 会只补齐失败的条目（带重试）
- **ocr-range 误删邻句 / 段号过期**：`ocr-range` 是整段替换，逐字显示下段的存储时间并集可能与前后句重叠，重扫窗口又按 `sample_step` 粗采样，容易把合法台词一并删掉；且每次重识别后 `--segment` 的 id 会重排，沿用旧 id 会重识别错误时间窗。处理方式：误删后 `ocr --force` 恢复基线；修单句优先用 `--start/--end` 按秒定位，重扫后逐条核对，或用密集抽帧 OCR 确认真实文本后手动清理 `segments.json`
- **校验截图没覆盖到的 HUD 文字混入台词**：`region_check_*.png` 只抽查 4 帧，其他时段（如章节选择/画廊画面）落在台词区内的 HUD 计数（如「残り:1/1」）仍可能被识别。全文检查发现这类非台词段，直接从 `segments.json` 删除即可
- **音画不同步（VFR 源视频）**：本工具按恒定帧率（CFR）假设处理视频。若源视频是可变帧率（VFR，部分安卓录屏即使选了 60fps 仍是 VFR），渲染后画面会相对声音逐渐漂移、结尾音频还可能被截断。**管线第0步已内置 ffmpeg 转 CFR**（`run.py all` 自动执行，或单独 `run.py cfr <视频>`），无需手工转换；也可录屏时固定高帧率（如 XRecorder / AZ 选 60fps 并关闭「可变帧率」开关）从源头减少抖动。手工转换参考：

    ```bash
    ffmpeg -i input.mp4 -vf "fps=60,setpts=N/(60*TB)" -af "aresample=async=1" -c:v libx264 -crf 18 -c:a aac -movflags +faststart output_cfr.mp4
    ```

    可用 `ffmpeg -i output_cfr.mp4 -map 0:v:0 -vf showinfo -f null - 2>&1 | grep -o pts_time:[0-9.]*` 检查帧间隔是否恒为 1/60。
- **密钥勿外泄**：`.env`、缓存、`*_output/` 已列入 `.gitignore`
- **自动开关实例（深夜无人值守）**：处理完视频后运行 `bash scripts/shutdown_gpushare.sh --shutdown` 关闭实例、`--start` 启动实例（关机默认按 `.env` 的 `GPUSHARE_INSTANCE_NAME` 定位；`--start` 不指定名称时会**自动找到「显卡空闲可启动」的实例**启动，也可加 `--instance-name <名称>` 指定；目标状态已满足时幂等提示，不会重复操作）。首次需先运行 `bash scripts/shutdown_gpushare.sh --login`：账号/密码从 `.env` 的 `GPUSHARE_USERNAME/GPUSHARE_PASSWORD` 自动填入，只需人工输入验证码，登录态（cookie）显式保存到 `~/.gpushare-auto/storage.json`，之后免登录。操作走「实例管理」下拉（hover 展开）→ 关机/启动 → 弹窗确认按钮（关机「我已了解风险，立即关机」/ 启动「确认启动」），关键步骤自动截图到 `~/.gpushare-auto/shots/`。
