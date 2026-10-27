# 画家麦麦的日常 (Painter Maimai's Daily Life)

<p align="center">
  <strong>集智能绘画、生成式自拍、内置日程与多渠道发布于一体</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/版本-v4.0.0-blue" alt="Version">
  <img src="https://img.shields.io/badge/MaiBot-1.3.x+-green" alt="MaiBot">
  <img src="https://img.shields.io/badge/License-AGPL--3.0-orange" alt="License">
</p>

---

## 关于本仓库

本仓库由 **nguspring** 维护，是在上游画图插件基础上的**改版**，不是从零原创。发展脉络如下：

1. 最初基于 **1021143806 (Ptrel)** 的 [custom_pic_plugin](https://github.com/1021143806/custom_pic_plugin) 修改，发布为 `selfie_painter`（v3.4.x ~ v3.5.x）。
2. 原作者后来将 custom_pic_plugin 升级重构为 [mais-art-journal](https://github.com/1021143806/mais-art-journal)。
3. 本仓库曾基于 mais-art-journal 合并重构，发布为 `selfie_painter_v2`（v3.6.x）。
4. **v4.0.0** 以 mais-art-journal 的 4.7.0 工作树为底座，重置品牌、插件 ID 与版本号，正式更名为 **画家麦麦的日常**。

| 项目 | 链接 |
|------|------|
| 上游原版 | [mais-art-journal](https://github.com/1021143806/mais-art-journal)（原作者 1021143806 / Ptrel） |
| 本仓库 | https://github.com/nguspring/painter_maimai_daily |
| 旧版 selfie_painter | 旧 main 保存在标签 `legacy-selfie-painter-v3.6.10` |
| 当前版本 | v4.0.0 |

画图底座、API 客户端、部分自拍与命令逻辑来自上游；内置日程增强、日程注入、多渠道自动自拍、自研 QQ 空间发布、额度与二改适配由 nguspring 维护。转载或二次开发请保留上游署名与 AGPL-3.0 许可证。

---

## 从旧版升级到 v4.0.0

v4.0.0 更换了插件 ID、目录名和 Python 包名，**不能直接覆盖旧目录升级**。

```shell
cd MaiBot/plugins
# 1. 先备份旧 config.toml
# 2. 删除旧插件目录（selfie_painter 或 mais_art_journal）
# 3. 重新克隆
git clone https://github.com/nguspring/painter_maimai_daily.git
```

重启 MaiBot 后会自动生成新的 `config.toml`，按下方指南填写 API 密钥和模型即可。

注意事项：

- 新插件 ID：`nguspring.painter_maimai_daily`，旧 `1021143806.mais_art_journal` 与旧 `selfie_painter` 的配置不会自动沿用。
- 配置版本重置为 `4.0.0`；旧 3.x 结构会走迁移逻辑，旧 4.x 绘卷配置请勿直接拷贝。
- 首次加载生成 `config.toml` 后，建议再重启一次 MaiBot，确保模型配置被完整读取。

---

## 插件定位

**画家麦麦的日常** = 画图 + 自拍 + 内置日程 + 日程注入 + 多渠道发布

| 功能 | 描述 |
|------|------|
| 画图 | 文生图 / 图生图自动识别，兼容 10 种 API 格式 |
| 自拍 | standard / mirror / photo 三种构图，支持参考图图生图 |
| 日程 | 内置 SQLite 日程库，LLM 多轮生成、质量评估、模板兜底 |
| 注入 | 在规划器 / 回复器请求前注入当前日程，支持意图识别与冷却 |
| 多渠道 | 同一张自拍可发往 QQ 空间、多个群和私聊名单 |
| 空间 | 自研 QQ 空间图文发布，不依赖 Maizone |
| 额度 | 群 / 私聊独立额度与投递结果核对 |
| 对外 API | 其他插件可调用本插件生图能力 |

---

## 主要特性

### 智能图片生成

- 自动判断文生图或图生图；消息里带图时走图生图。
- 支持 auto / standard / mirror / photo 四种自拍构图。
- 提示词优化器按后端自动选择自然语言或 SD 标签。
- 结果缓存、失败重试、按模型配置自动撤回。
- 角色参考图、风格别名、模型别名与聊天流级配置。

### 多 API 格式

| format | 平台 / 用途 |
|--------|-------------|
| `openai` | OpenAI、硅基流动、NewAPI、智谱 GLM、魔搭同步接口等 |
| `openai-chat` | 通过 `/chat/completions` 生图的模型 |
| `doubao` | 豆包 / 火山方舟（Ark SDK） |
| `gemini` | Google Gemini 原生接口 |
| `modelscope` | 魔搭异步任务，自动轮询 |
| `novelai` | NovelAI 原生 V3 / V4 / V4.5 / V5 |
| `shatangyun` | 砂糖云 NovelAI |
| `mengyuai` | 梦羽 AI |
| `comfyui` | 本地 / 远程 ComfyUI 工作流 |
| `dashscope` | 阿里百炼：通义千问、万相、Z-Image、可灵等 |

> 魔搭部分图片模型只支持异步调用，请把模型 `format` 设为 `modelscope`，不要用 `openai`。

### 内置日程

- 每日自动生成，支持 `builtin` 与 `maitrace` 两种 provider。
- 模板兜底 + LLM 多轮生成 + 质量评分，不达标自动重试。
- 读取麦麦人设、兴趣、生活规律，并参考最近几天日程保持连续性。
- 数据落 SQLite，支持历史、保留天数与跨天清理。

### 日程注入

- 通过 `maisaka.planner.before_request` 与 `maisaka.replyer.before_request` 注入。
- 意图分类：闲聊、日程相关、无关话题分别处理。
- 支持消息数阈值、最小间隔、上下文轮数、会话白名单。
- 同一请求标记去重，避免重复注入。

### 多渠道自动自拍

- 一次生成，同时发往 QQ 空间、多个群、私聊名单。
- 各渠道独立冷却与结算；空间失败不影响群和私聊。
- 配文支持 `shared`（共用）与 `per_channel`（按渠道分别生成）。
- 支持安静时段、概率、活动过滤与参考图。

### 自研 QQ 空间发布

- 取 Cookie 支持适配器、NapCat HTTP、本地缓存三种方式。
- 自动计算 `g_tk`，上传图片后发布图文说说。
- 结果分级为 `sent` / `rejected` / `uncertain`，`uncertain` 不自动重发。
- 鉴权失败会自动刷新一次 Cookie。

---

## /dr 命令系统

### 图片生成

| 命令 | 说明 |
|------|------|
| `/dr <描述>` | 自然语言生成图片，自动判断文生图 / 图生图 |
| `/dr <风格名>` | 对最近图片应用预设风格 |
| `/dr 用model2画一只猫` | 指定模型生成，也支持 `用模型2`、`用模型二`、`model2` |

### 风格与帮助

| 命令 | 说明 |
|------|------|
| `/dr styles` | 列出所有风格 |
| `/dr style <名>` | 查看风格详情 |
| `/dr help` | 帮助信息 |

### 配置管理（管理员）

| 命令 | 说明 |
|------|------|
| `/dr list` / `/dr models` | 列出所有模型 |
| `/dr config` | 查看当前聊天流配置 |
| `/dr set <模型ID>` | 设置当前聊天流的 /dr 模型 |
| `/dr default <模型ID>` | 设置智能生图默认模型 |
| `/dr model on\|off <模型ID>` | 开关指定模型 |
| `/dr recall on\|off <模型ID>` | 开关指定模型自动撤回 |
| `/dr on` / `/dr off` | 开关插件 |
| `/dr selfie on\|off` | 开关自拍日程增强 |
| `/dr selfie auto\|standard\|mirror\|photo` | 切换自拍风格 |
| `/dr schedule` | 查看当前活动与今日日程 |
| `/dr schedule regen` | 用 LLM 重新生成今日日程 |
| `/dr reset` | 清除当前聊天流的运行时覆盖 |

### 额度

| 命令 | 说明 |
|------|------|
| `/dr quota` / `/dr 额度` | 查看群共享额度或私聊额度 |
| `/dr quota adjust +5 [原因]` | 管理员增减本周期额度 |
| `/dr quota pending [页码]` | 查看投递结果待核对记录 |
| `/dr quota resolve <记录ID> sent [原因]` | 确认图片已送达 |

> 模型、开关、自拍风格等聊天流配置会持久化，重启后保留。

---

## 新手快速配置

### 1. 安装

```shell
cd MaiBot/plugins
git clone https://github.com/nguspring/painter_maimai_daily.git
```

确认 MaiBot 版本满足 manifest 要求（MaiBot 1.3.11+，SDK 2.15+）。

### 2. 准备 API

至少准备一个可用的生图接口，例如魔搭、硅基流动、OpenAI 兼容中转、豆包、Gemini 或本地 ComfyUI。

### 3. 填写配置

首次启动后会生成 `config.toml`，重点填写：

- `[models.items]`：`base_url`、`api_key`、`format`、`model`
- `[selfie]`：参考图、自拍模型、渠道开关
- `[schedule]`：provider、时区、生成时间
- `[qzone]`：Cookie 获取方式与 HTTP 地址
- `[basic]`：命令前缀、管理员、默认模型

### 4. 测试

在 QQ 里发送：

```text
/dr 画一只在窗边晒太阳的猫
/dr schedule
```

能正常出图和显示日程，说明基础链路已通。

---

## 配置说明

### `[plugin]`

插件名称、配置版本、总开关。升级后建议保留 `enabled = true`。

### `[basic]`

命令前缀、默认模型、重试次数、调试日志、缓存、自动撤回、提示词优化器、生成超时等。

### `[selfie]`

自拍总开关、参考图、固定风格、日程开关、模型、配文模式、安静时段、渠道开关与概率。

### `[group_image]`

群聊主动图片：群名单、间隔、冷却、概率、最低消息数、上下文窗口、安静时段。

### `[schedule]`

内置日程：provider、时区、LLM 任务名、多轮次数、质量阈值、条目数量、自动生成时间、历史与保留天数、人设与自定义提示词。

### `[inject]`

日程注入：规划器 / 回复器开关、意图分类、状态分析、闲聊注入概率、上下文轮数、TTL、最小间隔与消息数、会话白名单。

### `[qzone]`

空间发布：开关、Cookie 获取顺序（`adapter` / `napcat` / `local`）、HTTP 地址与 Token、Cookie 有效期、发布超时。

### `[quota]`

额度：周期、时区、群 / 私聊上限、规则列表。

### `[proxy]`

可选 HTTP 代理，供生图与图片下载使用。

### `[models.items]` 与 `[styles.items]`

模型与风格列表。模型字段较多，按后端的 `format` 填写对应参数即可；不确定的字段保持默认。

---

## 平台速查

| 平台 | format | 关键配置 |
|------|--------|----------|
| 魔搭 | `modelscope` | `base_url = https://api-inference.modelscope.cn/v1`，必须异步 |
| 硅基流动 | `openai` | `base_url = https://api.siliconflow.cn/v1` |
| OpenAI 兼容中转 | `openai` | 填中转 `/v1` 地址与 key |
| 豆包火山方舟 | `doubao` | 填 Ark key 与 endpoint/model |
| Gemini | `gemini` | 填 Gemini key 与模型名 |
| NovelAI 原生 | `novelai` | 填官方或反代地址、模型与采样参数 |
| 砂糖云 | `shatangyun` | 按服务商文档填 token |
| 本地 ComfyUI | `comfyui` | 填地址与工作流 JSON 路径 |

---

## 依赖

- `maibot-plugin-sdk >= 2.15, < 3`
- `requests`
- `Pillow`
- `pydantic`
- `tomlkit`
- `tzdata`
- `httpx`

---

## 使用示例

```text
/dr 画一只在雨里打伞的白猫，水彩风
/dr 用model2画一张赛博朋克城市夜景
/dr selfie mirror
/dr schedule
/dr quota
```

自动自拍开启后，会按日程活动生成图片，并按配置发布到空间、群或私聊。

---

## 常见问题

**Q：插件加载了但读不到模型？**  
A：先确认 `config.toml` 已生成并填写完整，再重启一次 MaiBot。

**Q：魔搭返回“不支持同步调用”？**  
A：把该模型 `format` 改为 `modelscope`，走异步任务轮询。

**Q：空间发布提示“请先登录空间”？**  
A：Cookie 必须按空间域名获取；适配器方式应请求 `qzone.qq.com`，并确保请求头带完整 Cookie。

**Q：空间报“您输入的链接不是有效链接”？**  
A：检查 `richval` 是否为网页端格式，且不要额外提交 `pic_bo`。本仓库已修复该问题。

**Q：自动自拍没有发空间？**  
A：检查 `[selfie].qzone_enabled`、`[qzone].enabled`、Cookie 获取方式和安静时段。

**Q：旧版配置能直接复制吗？**  
A：不能。v4.0.0 更换了插件 ID 与配置版本，请重新生成后手动迁移密钥和模型。

---

## 致谢

感谢以下开发者：

- **1021143806 (Ptrel)**：创建 custom_pic_plugin，并重构为 mais-art-journal，提供画图底座与多模型架构。
- **Rabbit-Jia-Er**：多模型调用与命令功能。
- **saberlights Kiuon**：自拍功能与自然语言命令。
- **A-Dawn**：代码排查思路、A_MIND 插件灵感与 Gemini 图片反代热修复。
- **XXXxx7258**：Mai_Only_You 插件灵感与搜图代码。
- **xuqian13**：内置日程系统参考 autonomous_planning_plugin 的设计思路。
- **小乐**：首席体验官。

本仓库的改版维护、内置日程增强、日程注入、多渠道自拍、自研空间发布与额度系统由 **nguspring** 完成。

---

## 开发历程

1. 基于 MaiBot 早期豆包生图示例。
2. MaiBot 0.8 插件系统重构。
3. Rabbit-Jia-Er 加入，多模型与命令。
4. saberlights Kiuon 加入，自拍与自然语言命令。
5. 原作者将 custom_pic_plugin 升级为 mais-art-journal。
6. nguspring 基于原版开发 selfie_painter（v3.4.x ~ v3.5.x），重做日程系统。
7. v3.6.x 基于 mais-art-journal 合并重构，持续修复自拍与提示词。
8. v4.0.0 重置品牌与版本，合并内置日程、注入、多渠道与自研空间发布。

---

## 版权与反馈

- 作者：nguspring
- 许可证：AGPL-3.0
- 项目主页：https://github.com/nguspring/painter_maimai_daily
- Issue：https://github.com/nguspring/painter_maimai_daily/issues
- 联系 QQ：3896597914

更新日志见 [changelog.md](changelog.md)。
