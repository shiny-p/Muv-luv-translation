# 阿里云 ECS 部署 Muv-luv-translation 说明

## 项目本质
这是一个 **命令行批处理工具**（不是网站/服务）：输入录屏视频 → OCR 识别日文台词 →
AI 翻译成中文 → ffmpeg 渲染加字幕条输出。所以 ECS 上只需要：
- Python 3.12 + 依赖（OpenCV/RapidOCR/onnxruntime/openai/imageio-ffmpeg）
- 中文字体（Linux 默认没有，必须装并指定 `render.font`）
- DeepSeek API Key
- 上传视频 → 后台跑 `run.py all` → 下载成品

## 部署方式一：脚本一键部署（推荐）
1. 把 `setup_ecs.sh` 上传到 ECS：
   `scp deploy_ecs/setup_ecs.sh root@<ECS公网IP>:/root/`
2. 登录 ECS 执行（记得带上你的 DeepSeek key）：
   `DEEPSEEK_API_KEY=sk-xxxx bash setup_ecs.sh`
3. 之后按脚本结尾提示操作即可。

## 部署方式二：手工步骤（Ubuntu 22.04/24.04 示例）
```bash
# 1) 系统依赖
apt update && apt install -y python3.12 python3.12-venv python3-pip \
  git ffmpeg libgl1 libglib2.0-0 fonts-noto-cjk

# 2) 项目
git clone --depth 1 https://github.com/shiny-p/Muv-luv-translation.git /root/muv
cd /root/muv

# 3) 依赖
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 4) OCR 模型（ModelScope 国内直连）
.venv/bin/rapidocr download_models

# 5) 配置：.env 写入 DEEPSEEK_API_KEY；config.yaml 设置 render.font
vi .env            # DEEPSEEK_API_KEY=sk-xxxx
sed -i 's|^  font:.*|  font: "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"|' config.yaml
```

## 处理视频
```bash
# 本机上传视频到 ECS（3Mbps 带宽下 1GB 视频约 45 分钟，耐心等）
scp /path/to/1.mp4 root@<ECS公网IP>:/root/muv/

# ECS 上后台运行（断连也不中断）
cd /root/muv && nohup .venv/bin/python run.py all 1.mp4 > run.log 2>&1 &

# 查看进度
tail -f /root/muv/run.log

# 完成后下载成品（1_output/ 里是全部产物：成品视频+翻译+OCR缓存）
scp -r root@<ECS公网IP>:/root/muv/1_output/ ./
```

## 免费试用 ECS 的性能提示
- 典型免费实例是 2 vCPU / 2GB 内存，OCR(onnxruntime CPU) + x264 编码都比较吃 CPU，
  处理 1 小时 1080p60 视频预计需要数小时，属正常，建议 nohup 挂后台。
- 若想提速：config.yaml 里把 `render.width` 设为 1280（降分辨率），
  `cfr.preset` / `render.preset` 改为 `veryfast`（画质略降但快很多）。
- 若 2GB 内存不够（OOM），把 `ocr.sample_step` 保持 48 即可（不要低于 24），
  并关闭其他大内存程序。

## 注意事项
- `.env` 含密钥，切勿提交 git 或泄露。
- OCR 模型来自 ModelScope（国内可直连），首次运行前用 `rapidocr download_models` 预下载。
- 渲染前必须确认 `render.font` 指向存在的字体文件，否则 `run.py render` 会报“未找到中文字体”。
- 成品在 `<视频名>_output/`：`output.mp4` 是最终视频，`translations.json` 可手工校对翻译。
