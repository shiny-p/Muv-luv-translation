# 识图能力

你的底层模型不具备原生识图能力。遇到图片时，不要尝试直接“读”图片内容（Read/查看图片无法取得内容），改用项目根目录的 vision 脚本：

```
./vision.sh "<图片绝对路径>" "请用中文详细描述这张图片的内容"
```

如果 `./vision.sh` 不可用（系统已安装 node），也可以直接：

```
node vision.js "<图片路径>" "请用中文详细描述这张图片的内容"
```

## 触发场景

- 用户分享图片路径（本地或网络 URL）
- 消息中出现 "Saved attachments:" 并列出图片
- 用户要求分析、描述、识别图片内容
- 需要从图片中提取文字（OCR 类需求）时，同样用该脚本并明确要求“请识别并输出图中所有文字”

## 规则

- 遇到图片必须先用 vision.js 取得文字描述，拿到结果后再回复，不可跳过。
- 配置在 .env（DASHSCOPE_API_KEY / VISION_MODEL / DASHSCOPE_BASE_URL），模型为阿里云百炼 qwen-vl-max。
- 不要把 .env 或其中内容提交到 git（.env 已在 .gitignore）。
