# 恒源云实例开关机脚本操作指南

用 Node.js + Playwright 模拟浏览网页与点击，在恒源云控制台一键**关机/启动实例**，适合视频处理流水线跑完后的深夜无人值守场景。

## 环境要求

- 本地 macOS（实例关机后脚本必须存活，故不能在实例上运行）
- Node.js 运行时：优先使用 Codex 自带的 bundled node（脚本会自动查找 `~/.cache/codex-runtimes/*/dependencies/node`），也兼容系统 node
- 浏览器：系统已安装 Google Chrome（脚本用 `channel: 'chrome'`）
- 无需安装 playwright：脚本会自动加载 Codex bundled 的 playwright（1.62）

## 快速开始

```bash
# 1) 首次：登录一次（账号密码自动填入，只需人工输入验证码）
bash scripts/shutdown_gpushare.sh --login

# 2) 关闭实例（默认按 .env 的 GPUSHARE_INSTANCE_NAME 定位）
bash scripts/shutdown_gpushare.sh --shutdown

# 3) 启动实例（不指定名称时自动找「显卡空闲可启动」的实例）
bash scripts/shutdown_gpushare.sh --start

# 指定实例（按控制台显示的实例名称）
bash scripts/shutdown_gpushare.sh --start --instance-name 实例1
bash scripts/shutdown_gpushare.sh --shutdown --instance-name 实例2
```

## 配置（.env，已 gitignore 不提交）

| 变量 | 说明 |
|---|---|
| `GPUSHARE_USERNAME` | 恒源云账号（--login 自动填入） |
| `GPUSHARE_PASSWORD` | 恒源云密码（--login 自动填入；留空则手动输入） |
| `GPUSHARE_INSTANCE_NAME` | 控制台显示的实例名称（--shutdown 默认定位；--start 未指定名称时忽略此值，改为自动找可启动实例） |

## 参数

| 参数 | 说明 |
|---|---|
| `--login` | 首次登录并保存登录态（只需输一次验证码） |
| `--shutdown` | 关闭实例（走「实例管理」下拉 → 关机 → 「我已了解风险，立即关机」） |
| `--start` | 启动实例（下拉 → 启动 → 「确认启动」）；未指定名称时自动找「显卡空闲可启动」的实例 |
| `--headless` | 无头模式（默认有头，可观察操作过程） |
| `--instance-name <名称>` | 按控制台显示的实例名称定位 |
| `--console-url <URL>` | 指定实例列表地址（默认 `https://gpushare.com/center/hire`） |

## 行为细节

- **登录态**：`--login` 成功后，cookie 显式保存到 `~/.gpushare-auto/storage.json`（不依赖浏览器 profile 写盘），之后免登录
- **实例定位**：打开实例列表页后，按 `tr[data-row-key]` 行匹配名称/主机名；`--start` 无名称时遍历行找「显卡空闲可启动」（绿色字体提示）
- **幂等**：目标状态已满足时提示「无需操作」并正常退出，不会重复点击
- **截图**：关键步骤自动截图到 `~/.gpushare-auto/shots/`，定位失败时可据此排查
- **下拉菜单**：「实例管理」为 hover 展开的 Ant Dropdown，脚本用 `mouseenter` 事件触发，并用真实鼠标点击菜单项（仅 DOM click 无法触发弹窗）

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功（含幂等提示） |
| 1 | 参数/运行错误，或登录超时 |
| 2 | 未找到登录态 / 登录态失效（先跑 `--login`） |
| 3 | 实例定位或操作失败（看截图） |

## 常见问题

- **登录态失效**：重新运行 `bash scripts/shutdown_gpushare.sh --login`（需人工输验证码）
- **找不到实例**：确认实例名称（控制台「我的实例」列表可见）与 `--instance-name` / `.env` 一致；`--start` 找不到可启动实例时，说明没有「显卡空闲可启动」的实例（无空闲显卡或全部运行中）
- **操作失败**：查看 `~/.gpushare-auto/shots/` 最新截图定位原因
