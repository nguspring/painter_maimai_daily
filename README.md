# 画家麦麦的日常 (Painter Maimai's Daily Life) - 智能多模型图片生成插件

基于 MaiBot 的智能多模型图片生成插件，支持文生图和图生图自动识别。兼容原生 NovelAI、OpenAI、OpenAI-Chat、豆包（火山方舟）、Gemini、魔搭、硅基流动、砂糖云、梦羽、智谱 GLM、阿里百炼 DashScope（通义千问/万相/Z-Image/可灵）、ComfyUI 等多种 API 格式。提供内置日程与日程注入、空间/群/私聊多渠道自动自拍、群聊主动图片、聊天设置持久化、群/私聊额度管理、对外生图 API 等功能，可选对接 MaiTrace。

## ✨ 主要特性

### 🎯 智能图片生成

- **文生图**: 根据对话内容自动生成图片
- **图生图**: 检测到消息中有图片时自动使用图生图模式
- **自拍模式**: 支持 auto（按活动）/ standard（前置自拍）/ mirror（对镜自拍）/ photo（第三人称照片），共用角色形象与可选参考图
- **提示词优化**: 普通图与自拍均按后端选择自然语言或标签，保留主体数量、布局、画面文字、画风及图生图编辑要求；自拍保持角色身份并按活动换装
- **结果缓存**: 相同参数复用之前的结果
- **自动撤回**: 可按模型配置延时撤回，支持 SnowLuma／NapCat；现有 QQ 官方适配器未开放撤回 API，自动跳过

### 🎨/dr 命令系统

#### 图片生成

| 命令 | 说明 |
|------|------|
| `/dr <风格名>` | 对最近的图片应用预设风格（图生图） |
| `/dr <描述>` | 自然语言生成图片（自动判断文/图生图） |
| `/dr 用model2画一只猫` | 指定模型生成，也支持 `用模型2`、`用模型二`、`model2`、`模型2` |

#### 风格管理

| 命令 | 说明 |
|------|------|
| `/dr styles` | 列出所有可用风格 |
| `/dr style <名>` | 查看风格详情 |
| `/dr schedule` | 查看当前活动和今天全部日程 |
| `/dr help` | 帮助信息 |

#### 配置管理（需管理员权限）

| 命令 | 说明 |
|------|------|
| `/dr list` | 列出所有模型 |
| `/dr config` | 显示当前聊天流配置 |
| `/dr set <模型ID>` | 设置 /dr 命令使用的模型 |
| `/dr default <模型ID>` | 设置智能生图 Tool 默认模型（当前聊天流，优先于场景自动选择） |
| `/dr model on\|off <模型ID>` | 开关指定模型 |
| `/dr recall on\|off <模型ID>` | 开关指定模型的撤回 |
| `/dr on` / `/dr off` | 开关插件（当前聊天流） |
| `/dr selfie on\|off` | 开关自拍日程增强（当前聊天流） |
| `/dr selfie auto\|standard\|mirror\|photo` | 切换自拍风格（当前聊天流） |
| `/dr schedule regen` | 用 LLM 重新生成今日日程 |
| `/dr reset` | 重置当前聊天流的所有运行时配置 |

> 模型切换、插件/模型/撤回开关、自拍日程开关和自拍风格按宿主真实聊天流保存，重启后继续生效。没有设置覆盖的聊天流跟随全局配置；`/dr reset` 只清除当前聊天流的设置覆盖，不重置生图额度。

#### 聊天生图额度

| 命令 | 说明 |
|------|------|
| `/dr quota` 或 `/dr 额度` | 查看当前群共享额度或私聊个人额度 |
| `/dr quota adjust +5 [原因]` | 管理员增加本周期额度；负数为减少 |
| `/dr quota pending [页码]` | 管理员查看投递结果待核对的记录 |
| `/dr quota resolve <记录ID> sent [原因]` | 管理员确认这张图已经送达 |
| `/dr quota resolve <记录ID> refund [原因]` | 管理员确认未送达并退回原周期额度 |

额度默认关闭。启用后，用户发起的智能 Tool、风格命令和自然语言命令共用一套额度；同一群内所有用户共享，私聊按用户单独计算。每张成功发送的图片（含缓存图片）计一次，管理员也照常计数。

### 内置日程与日程注入

默认使用插件自带的 SQLite 日程库，无需安装 MaiTrace；也可把 `schedule.provider` 改回 `maitrace` 复用外部日程。

- 每天先用模板兜底，再由 LLM 结合人设、兴趣、生活习惯和历史日程生成；质量不达标时按配置多轮重试
- 日程按 `schedule.timezone` 统一计算，支持跨午夜活动；`/dr schedule` 查看当天日程，管理员可用 `/dr schedule regen` 重新生成
- 规划器和回复器通过命名 Hook 读取内存快照注入当前活动，不查库、不调 LLM；带固定标记去重，可用意图分类、时间冷却、消息数阈值和会话白名单控制频率
- 关闭 `schedule.enabled` 后所有主动任务视为无日程，不会静默回退到 MaiTrace

### 多渠道自动自拍与群聊主动图片

根据当前日程、人设和群话题，决定分享图文自拍到 QQ 空间、多个允许群和私聊名单，或跳过本轮。

- 同一轮只生成一张图；每个渠道独立冷却、独立预占、独立结算，空间失败不会阻断群和私聊
- 自动自拍默认每 20 分钟判断一次，发布冷却 2 小时，00:00–07:00 静默；群话题图片另有概率和冷却设置
- 配文可按 `selfie.caption_mode` 共用一条或按渠道分别生成；关闭配文时使用固定文案
- 判断不发不占发布冷却；临发送重新查日程，活动、地点、服装或生活场景变化时丢弃结果，连续失败会延长重试间隔
- 空间发布由插件自行完成：通过适配器 / NapCat HTTP / 本地缓存获取 Cookie，只发图文说说，不依赖 MaiTrace

---

## 支持的 API 格式

| format | 平台 | 说明 |
|--------|------|------|
| `openai` | OpenAI / 硅基流动 / Grok / NewAPI 等 | 通用 `/images/generations` 接口 |
| `openai-chat` | 支持生图的 Chat 模型 | 通过 `/chat/completions` 生图，多策略提取图片 |
| `doubao` | 豆包（火山引擎） | 使用 Ark SDK，支持 seed/guidance_scale/watermark |
| `gemini` | Google Gemini | 原生 `generateContent` 接口，支持 Gemini 2.5/3 系列 |
| `modelscope` | 魔搭社区 | 异步任务模式，自动轮询结果 |
| `dashscope` | 阿里百炼 DashScope | 支持通义千问/万相/Z-Image/可灵等模型 |
| `shatangyun` | 砂糖云 (NovelAI) | GET 请求，URL 参数传递 |
| `novelai` | 原生 NovelAI | 官方 `/ai/generate-image`，V3/V4/V4.5/V5，单图文生图和单张参考图的图生图 |
| `mengyuai` | 梦羽 AI | 支持图生图（默认走 Qwen Image Edit，可通过 `img2img_model_index` 切换） |
| `comfyui` | 本地 ComfyUI | 加载工作流 JSON，替换占位符，轮询结果（支持代理配置） |

---

## 🚀 快速开始

1. 使用终端或 Git Bash 进入 MaiBot 的插件目录：

   ```shell
   cd MaiBot/plugins
   ```

2. 克隆本仓库，指定目录名为 `painter_maimai_daily`：

   ```shell
   git clone https://github.com/nguspring/selfie_painter.git painter_maimai_daily
   ```

3. 在 MaiBot 使用的 Python 环境中安装插件依赖：

   ```shell
   uv pip install -r painter_maimai_daily/requirements.txt
   ```

4. 重启 MaiBot，插件会在 `MaiBot/plugins/painter_maimai_daily/` 生成 `config.toml`。
5. 按下方说明配置生图模型和辅助 LLM，重启 MaiBot 后用 `/dr 画一只猫` 验证。API Key 从对应服务商申请（如何申请 key 请自行前往对应平台官网查看 api 文档）

## 配置说明

首次启动自动生成 `config.toml`，可在 WebUI 修改配置并查看各字段说明。下面只列常用设置；升级时原配置会备份到 `old/`。各 TOML 示例对应文件中的不同段，修改已有段即可；新增模型才追加 `[[models.items]]`。

### 基础设置

```toml
[plugin]
enabled = true                    # 启用插件
config_version = "4.0.0"          # 插件配置版本，自动维护，请勿改

[basic]
command_prefix = "/dr"            # 命令前缀，可改 /dv、/draw 等，必须以 / 开头
default_txt2img_model = "model1"  # 默认文生图模型 ID（纯文生图时使用）
default_img2img_model = ""        # 默认图生图模型 ID；配置了第二个模型后可填 model2，留空沿用文生图模型
enable_unified_generation = true  # 启用智能生图 Tool（draw_picture）
enable_pic_command = true         # 启用 /dr 图片生成命令
pic_command_model = "model1"      # /dr 命令默认模型（可通过 /dr set 动态切换）
admin_users = ["12345"]           # 管理员实际用户 ID 列表（字符串；官方适配器按绑定情况填写 OpenID 或 QQ 号）
max_retries = 2                   # API 失败重试次数
enable_debug_info = false         # 在聊天中显示生成进度等调试信息
enable_verbose_debug = false      # OpenAI/OpenAI-Chat/DashScope 的详细请求/响应日志
cache_enabled = true              # 启用结果缓存
cache_max_size = 10               # 文生图和图生图各自的内存缓存条数上限，重启清空
auto_recall_enabled = false       # 自动撤回总开关（还需在模型配置里设 auto_recall_delay > 0）
prompt_optimizer_enabled = true   # 普通图/自拍场景优化；关闭后发布判断、尺寸选择、配文仍可用 LLM
llm_task_name = "utils"           # 辅助 LLM 任务名：utils / replyer / planner / memory / learner
llm_timeout_seconds = 45          # 辅助 LLM 每次调用超时（秒），包含主动判断
generation_timeout_seconds = 170 # 普通后端完整生成总超时；YesNovelAI 建议 420；ComfyUI 另按等待时间预留
proactive_min_energy = 30         # 所有主动图的最低精力，MaiTrace 生活状态使用 0-100
```

### 生图模型配置

在 WebUI 的「模型管理 → 模型列表」增删模型，或在插件 `config.toml` 中编辑 `[[models.items]]`。每项独立配置服务地址、密钥和型号。下面以硅基流动的文生图配置为例，`model` 须使用账号当前可用的生图型号：

```toml
[[models.items]]
id = "model1"                    # 插件内的唯一 ID，默认模型和命令引用它
name = "硅基流动文生图"            # 显示名称
format = "openai"                # 按实际接口协议选择，见上方 API 格式表
base_url = "https://api.siliconflow.cn/v1"
api_key = "Bearer YOUR_API_KEY_HERE"
model = "Kwai-Kolors/Kolors"      # 服务端型号，与本地 id 不同
fixed_size_enabled = true        # 固定尺寸，避免自动选择服务不支持的尺寸
default_size = "1024x1024"
seed = -1                        # 随机种子
guidance_scale = 2.5
num_inference_steps = 20
custom_prompt_add = ""           # 留空表示不追加固定画风
negative_prompt_add = "lowres, bad anatomy"
prompt_syntax = "tags"           # OpenAI 兼容扩散模型可显式使用标签语法
support_img2img = false          # 此例用于文生图，图生图需服务实际支持
auto_recall_delay = 0            # 0 不撤回；启用还需 basic.auto_recall_enabled
```

新增模型时复制整项，把 `id` 改为 `model2`、`model3` 等，再填写该模型的连接参数。`basic`、`selfie` 和 `group_image` 中的模型选择字段都填这里的 `id`；修改 `name` 或 `model` 不会切换 API 协议。

| 参数 | 填写说明 |
| --- | --- |
| `base_url` | `openai` / `openai-chat` 通常填带 `/v1` 的 API 基址；Gemini 填服务根地址，客户端会追加 `/v1beta/models/...:generateContent`；DashScope 填根地址，如 `https://dashscope.aliyuncs.com` |
| `api_key` | `openai` / `openai-chat` 原样发送 Authorization，应填 `Bearer token`；其他云后端支持裸 token 或 `Bearer token`，ComfyUI 可留空 |
| `fixed_size_enabled` / `default_size` | 开启固定尺寸时使用 `default_size`；关闭时优先本次请求尺寸，再由辅助 LLM 选择，失败使用默认值。尺寸须符合具体服务和型号要求 |
| `guidance_scale` / `num_inference_steps` | 扩散模型的引导强度 / 步数；OpenAI 兼容接口可填 `-1` 省略。其他后端是否使用或允许省略见 WebUI 字段说明；NovelAI 步数 1–50、引导强度 0.1–20 |
| `watermark` | 豆包、DashScope 及 OpenAI 格式连接火山方舟时使用的水印开关；其他客户端不发送，不能去除服务强制水印 |
| `custom_prompt_add` / `negative_prompt_add` | 所有请求共用的正面增强词 / 负面词；更换后端时同时检查，避免沿用默认模型的画风。正面词直接拼接，建议带逗号或空格；负面词按后端能力处理 |
| `prompt_syntax` | `auto`：魔搭、砂糖云、梦羽、NovelAI、ComfyUI 用标签，OpenAI、OpenAI-Chat、Gemini、豆包、DashScope 用自然语言；`natural` / `tags` 可显式指定 |
| `support_img2img` | 仅声明参考图能力，不能给服务增加图生图支持；设为 `false` 时清除源图并使用同一模型文生图 |
| `selfie_reference_strength` | 有参考图的自拍重绘强度，默认 0.6；用于 NovelAI、ComfyUI 和 `prompt_syntax = "tags"` 的 OpenAI 扩散网关，本次显式 `strength` 优先 |

部分后端还有专用字段：

| 后端 | 专用配置 |
| --- | --- |
| `shatangyun` | `artist`、`cfg`（CFG Rescale）、`sampler`、`noise_schedule`、`nocache`；当前客户端仅文生图，应设 `support_img2img = false`。`nocache` 不控制插件自身缓存 |
| `mengyuai` | `model` 填文生图数字索引字符串，如 `"0"`；图生图用 `img2img_model_index`（默认 19）。`image_upload_url` 用于上传参考图取得公网 URL，留空尝试 data URI，服务可能不支持 |
| `dashscope` | `endpoint_path` 留空使用多模态默认端点；万相 2.5 图生图填 `/api/v1/services/aigc/image2image/image-synthesis`，可灵填 `/api/v1/services/aigc/image-generation/generation`。这两类端点自动启用异步，其他端点按需设 `dashscope_async`；`prompt_extend` 控制服务端扩写 |

#### 原生 NovelAI

在模型列表新增一项。NAI 专属参数与通用参数写在同一项中，均可在 WebUI 的模型列表直接配置；旧版 `[models.items.novelai]` 会在升级时迁移并保留已有值。

```toml
[[models.items]]
id = "model2"
name = "NovelAI V4.5"
format = "novelai"
base_url = "https://image.novelai.net"
api_key = "YOUR_API_KEY_HERE"
model = "nai-diffusion-4-5-full"
fixed_size_enabled = true
default_size = "832x1216"
num_inference_steps = 28
guidance_scale = 5.0
custom_prompt_add = ""
negative_prompt_add = "lowres, bad anatomy, bad hands"
support_img2img = true

novelai_api_mode = "official"     # official / json；YesNovelAI 用 json
novelai_mode = "anime"            # anime / furry；background 仅 V4.5/V5
novelai_quality_tags = true
novelai_smea = false              # SMEA 仅支持 V3 文生图
novelai_smea_dynamic = false      # 开启时还需 novelai_smea = true
novelai_img2img_strength = 0.6     # 普通图生图强度；自拍用 selfie_reference_strength
novelai_img2img_noise = 0.1
novelai_request_timeout_seconds = 120
```

宽高须为 64–2048 之间的 64 倍数。支持的型号有 `nai-diffusion-3`、`nai-diffusion-furry-3`、`nai-diffusion-4-full`、`nai-diffusion-4-curated-preview`、`nai-diffusion-4-5-full`、`nai-diffusion-4-5-curated`、`nai-diffusion-5-full`、`nai-diffusion-5-curated`。V3 的 `novelai_mode` 须与 Anime/Furry 型号对应；V4+ 通过数据集标签切换模式。不支持的型号或参数组合会报错。

接入 [YesNovelAI](https://nai.rinko.ai/docs) 时，设置 `format = "novelai"`、`novelai_api_mode = "json"`、`base_url = "https://nai.rinko.ai"`、`api_key = "ynai-你的令牌"`，模型可用 `nai-diffusion-4-5-full`。JSON 模式也接受 `https://nai.rinko.ai/v1` 或完整的 `/v1/nai/generate-image` 地址，请求仍是原生 NAI 格式。总像素不得超过 3,145,728，CFG 最大 10。

建议将该模型的 `novelai_request_timeout_seconds` 设为 `310`，同时在 `[basic]` 将 `generation_timeout_seconds` 设为 `420`，为提示词优化和图片处理留出时间。对外生图 API 支持最长 660 秒；自动自拍和群主动图片仍受各自周期及调用方的总时限限制。JSON 请求使用幂等键；响应超时、断线或 HTTP 错误不会自动重发。

#### ComfyUI

将工作流导出为 **API 格式 JSON** 放入插件 `workflow/`，再配置一项模型。工作流使用的模型文件和节点需已安装在 ComfyUI 实例中：

```toml
[[models.items]]
id = "model3"
name = "ComfyUI 本地"
format = "comfyui"
base_url = "http://127.0.0.1:8188"
api_key = ""
model = "sdxl_txt2img_api.json"   # 相对 workflow/ 的文件名，也可填绝对路径
fixed_size_enabled = true
default_size = "1024x1024"
guidance_scale = 7.0
num_inference_steps = 30
custom_prompt_add = ""
negative_prompt_add = "lowres, bad anatomy"
support_img2img = false          # 图生图请换用带 ${image} 的工作流并设为 true

[comfyui]
client_id = "maibot-hilda"        # 多机器人连同一实例时各用不同标识
wait_seconds = 600               # 最长出图轮询时间（秒）
```

工作流支持以下占位符，在 JSON 字段值中用带引号的字符串填写，例如 `"steps": "${steps}"`：

| 占位符 | 内容 |
| --- | --- |
| `${prompt}` / `${negative_prompt}` | 正面 / 负面提示词 |
| `${width}` / `${height}` | 图片宽高 |
| `${seed}` / `${steps}` / `${cfg}` | 种子、步数、引导强度 |
| `${image}` / `${denoise}` | 图生图源图、降噪强度 |

图生图工作流必须包含 `${image}`；其余占位符按需使用。未替换的字段保留工作流原值。`comfyui.wait_seconds` 不受 `proxy.timeout` 控制，但仍受调用入口总时限限制：智能 Tool 为 660 秒、公开 API 为 180 秒，主动任务另有周期超时。

### 辅助 LLM 配置

生图服务与辅助 LLM 分别配置：

| 用途 | 配置位置 | 引用方式 |
| --- | --- | --- |
| 生成图片 | 插件 `config.toml` 的 `[[models.items]]` | `basic.default_txt2img_model` 等字段填写本地 `id` |
| 优化提示词、选择尺寸、自拍场景、主动发布判断和配文 | MaiBot 主程序 `config/model_config.toml` 的模型与任务配置 | 插件 `basic.llm_task_name` 填任务名，默认 `utils` |

若主程序的 `utils` 已能正常调用文本模型，保持 `llm_task_name = "utils"` 即可。需要更换辅助模型时，在主程序已有的 `[model_task_config.utils]` 段中调整 `model_list`，其中每项对应主程序 `[[models]]` 的 `name`。该任务也供主程序其他功能使用：

```toml
# MaiBot/config/model_config.toml 中已有的任务段
[model_task_config.utils]
model_list = ["你的文本模型配置名"]
```

`llm_task_name` 填 `utils`、`replyer` 等主程序已配置的文本任务名；`selfie.llm_selfie_model` 则填生图模型 ID。辅助 LLM 建议选择能稳定遵循指令、输出 JSON 的文本模型。它使用主程序的服务商、密钥和代理配置，插件 `[proxy]` 控制绘卷自己的图片请求。

关闭 `basic.prompt_optimizer_enabled` 会停用提示词与自拍场景的 LLM 优化，主动发布判断、自动尺寸选择和可选配文仍可能调用辅助 LLM。开启优化器时，优化失败会终止本次生成；`basic.llm_timeout_seconds` 控制单次辅助调用超时。

### 各入口如何选择模型

| 入口 | 选择顺序 |
| --- | --- |
| 智能生图 Tool（`draw_picture`） | 显式 `model_id` → 当前聊天 `/dr default` 覆盖 → 自拍时的 `selfie.llm_selfie_model` → 有参考图时的默认图生图模型 → 默认文生图模型 |
| `/dr` 风格 / 自然语言命令 | 描述中显式指定的模型（自然语言命令）→ 当前聊天 `/dr set` 覆盖 → `basic.pic_command_model` |
| 定时自动自拍 | `selfie.selfie_model` → `selfie.llm_selfie_model` → 按是否有共用参考图选择默认图生图 / 文生图模型 |
| 群话题主动图片 | `group_image.model_id` → 角色照片沿用 `selfie.llm_selfie_model`，再按参考图选择默认模型；普通图片用默认文生图模型 |
| 公开 `generate_image` API | 显式 `model_id` → 自拍时的 `selfie.llm_selfie_model` → 有参考图时的默认图生图模型 → 默认文生图模型 |

默认图生图 / 文生图模型分别是 `basic.default_img2img_model` / `basic.default_txt2img_model`；图生图留空沿用文生图。留空才会顺延，填写了不存在的 ID 会报错。`/dr` 即使检测到图片，也仍使用命令模型；希望改图用另一个模型时，请显式指定或用 `/dr set` 切换。

### 额度配置

```toml
[quota]
enabled = false                  # 开启后限制人类发起的聊天生图
period = "daily"                 # daily / weekly（周一）/ monthly / once
timezone = "Asia/Shanghai"
group_limit = 20                 # 每群每周期共享 20 张
private_limit = 5                # 每个私聊用户每周期 5 张

[[quota.rules]]                  # 可选：指定群或私聊用户覆盖默认值
platform = "qq"
target_type = "group"            # group / user（仅私聊）
target_id = "123456"
limit = 50                      # 0 表示禁止生图
period = "weekly"               # 省略时使用全局周期
```

管理员调整只影响当前周期；周期重置后仍使用配置中的基础额度。`once` 不会自动重置，也不受时区变化影响。改变其他周期或时区会使用对应的新周期账本，尚未结算的记录保留在原周期。`/dr quota pending` 可查看历史周期的待核对记录。

自动自拍、群聊主动图片和 MaiTrace 配图不扣聊天额度。发送失败且确认未投递时退还；发送结果未知时保留占用，由管理员用 `/dr quota pending` 和 `/dr quota resolve` 核对。

### 自拍与主动图片

| 配置 | 用途 |
| --- | --- |
| `selfie.enabled` | 所有自拍入口的总开关 |
| `selfie.prompt_prefix` | 共用角色形象，填写发色、瞳色等固定外观 |
| `selfie.reference_image_path` | 共用参考图，相对插件目录或绝对路径；留空使用文生图 |
| `selfie.default_style` | `auto` / `standard` / `mirror` / `photo` |
| `selfie.llm_selfie_model` | 普通自拍模型；留空按默认模型选择 |
| `selfie.schedule_enabled` | 聊天自拍是否读取日程 |
| `selfie.auto_enabled` | 定时自动自拍开关，默认关闭 |
| `selfie.selfie_model` | 自动自拍模型；留空沿用普通自拍模型 |
| `selfie.qzone_enabled` / `selfie.group_enabled` | 自动自拍允许发到空间 / 群 |
| `selfie.private_enabled` / `selfie.private_user_ids` | 自动自拍允许发私聊，并填写接收者实际用户 ID |
| `selfie.caption_mode` | `shared` 多目标共用一条配文；`per_channel` 按渠道分别生成 |
| `selfie.check_interval_minutes` / `selfie.interval_minutes` | 自动自拍判断间隔 / 发布冷却，默认 20 / 120 分钟 |
| `group_image.enabled` | 群话题主动生图开关，默认关闭 |
| `group_image.platform` / `group_image.group_ids` | 主动发群使用的平台与允许群 ID 名单 |
| `group_image.model_id` | 群话题图片使用的模型 |
| `group_image.probability` / `group_image.cooldown_minutes` | 群话题生图概率 / 每群发布冷却 |

自动自拍、聊天自拍和外部 API 自拍共用形象与参考图；服装按活动调整。主动发图需要有效日程（默认读内置日程，`provider=maitrace` 时读 MaiTrace），睡眠、低精力或静默时段会跳过。

自动自拍发群与群话题图片共用群名单和群冷却；只发定时自拍时无需开启 `group_image.enabled`。空间发布由插件自研链路完成，无需 MaiTrace 的空间接口。关闭 `selfie.caption_enabled` 后，`selfie.caption_text` 必须填写固定配文。

### 内置日程配置

```toml
[schedule]
enabled = true                    # 关闭后主动任务视为无日程，也不会回退 MaiTrace
provider = "builtin"              # builtin / maitrace
timezone = "Asia/Shanghai"        # 生成与判断统一使用的 IANA 时区
llm_task_name = "planner"         # 主程序已配置的文本任务名
generation_timeout_seconds = 180  # 单日生成总超时
max_tokens = 8192
multi_round = true
max_rounds = 2
quality_threshold = 0.8
min_activities = 8
max_activities = 15
auto_generate_time = "00:30"      # 每日自动生成时刻（schedule.timezone）
template_mode = "auto"            # auto 按工作日/周末；rest_day 为保留值
history_days = 3
retention_days = 30
snapshot_refresh_seconds = 60     # 内存快照刷新间隔
schedule_identity = ""            # 身份补充
schedule_interest = ""            # 兴趣爱好
schedule_lifestyle = ""           # 生活习惯
custom_prompt = ""                # 追加到生成提示词的要求
```

日程库固定写入 SDK 数据目录的 `schedule.db`；删除该文件会回到模板日程。

### 日程注入配置

```toml
[inject]
enabled = true
inject_into_planner = true        # 规划器追加日程背景
inject_into_replyer = true        # 回复器追加 extra_prompt
enable_intent_classification = true
enable_state_analysis = true
enable_inject_optimization = true
casual_chat_inject_probability = 0.5
context_max_turns = 3
context_ttl_seconds = 600
max_future_activities = 3
min_seconds = 300                 # 两次注入最小间隔
min_messages = 5                  # 冷却期内至少新增消息数
allowed_streams = []              # 留空不限；填写后只注入这些 session_id
```

### QQ 空间发布配置

```toml
[qzone]
enabled = true
cookie_methods = ["adapter", "napcat", "local"]  # 按顺序尝试
http_host = "127.0.0.1"
http_port = "9999"
http_token = ""
adapter_priority = []             # 优先适配器 plugin_id 或后缀
cookie_ttl_hours = 6
publish_timeout_seconds = 30
```

Cookie 有效时写入数据目录 `qzone_cookies-<uin>.json`；获取失败只会跳过空间渠道，群和私聊仍会发送。`uncertain`（网络中断、缺少动态 ID）不会自动重发，避免重复说说。

### QQ 适配器

支持 QQ 官方、SnowLuma／NapCat，通过宿主统一收发。管理员、额度目标和允许群名单均填实际聊天 ID：官方适配器未绑定时用 OpenID，绑定后用 QQ 号／群号。自动撤回支持 SnowLuma／NapCat，官方消息会跳过。空间 Cookie 会优先通过适配器的 `account.get_cookies` 获取。

## 与 MaiTrace 配合

[MaiTrace](https://github.com/Rabbit-Jia-Er/MaiTrace)（麦时迹）现在是可选的日程来源：把 `schedule.provider` 设为 `maitrace` 后，主动发图会读取它的公开日程 API 和可选生活状态；默认的 `builtin` 模式完全独立运行。空间发布已由绘卷自研链路负责，不再调用 MaiTrace 的直发接口。

从旧版升级时，原有 `selfie.auto_enabled`、群名单、冷却和额度设置继续生效；新增的 `schedule` / `inject` / `qzone` 段会在首次加载时自动补齐默认值。

其他插件可通过公开 API 获取图片，自行决定如何发送：

```python
result = await ctx.call_capability(
    "api.call", timeout_ms=185000,
    api_name="nguspring.painter_maimai_daily.generate_image", version="1",
    args={"prompt": "公园里的猫", "model_id": "model1"},
)
```

返回 `success`、`image_base64`、`model_id`、`size`、`is_img2img`、`error`、`selfie_style` 和 `outfit`；图片为纯 base64。自拍传入 `selfie_mode=true`，可选传构图、活动和服装；模型列表接口为 `nguspring.painter_maimai_daily.list_image_models`。

## 💡 使用示例

### 自然语言生图（可以指定model1，model2 等，支持中文）

```
用户：麦麦，画一张美少女
麦麦：[生成图片]

用户：[发送图片]
用户：[回复 麦麦： [图片] ]，说：麦麦，把这张图的背景换成海滩
麦麦：[图生图：基于输入图片生成]
```

### 自拍模式

```
用户：麦麦，来张自拍！
麦麦：[生成符合当前活动和服装要求的角色照片]
```

### 命令式风格转换（仅图生图）

```
用户：[发送图片]
用户：[回复 麦麦： [图片] ]，说：/dr cartoon
麦麦：[应用卡通风格]
```

### 命令式自然语言生成（文/图生图）

```
用户：/dr 画一只可爱的猫
麦麦：[文生图：生成新图片]

用户：[发送图片]
用户：[回复 麦麦： [图片] ]，说：/dr 把这张图的背景换成海滩
麦麦：[图生图：基于输入图片生成]
```

## 🔧 依赖说明

- 插件需 Python 3.12+，同时遵循所用 MaiBot 版本的 Python 要求
- 依赖 MaiBot 项目插件系统与 `maibot-plugin-sdk>=2.15,<3`
- 内置日程、注入和 QQ 空间发布依赖 `httpx`、`tzdata`、`tomlkit` 等，启动时会按 manifest 自动检查
- MaiBot 项目地址：https://github.com/Mai-with-u/MaiBot
- 如果使用豆包（火山方舟）API，需要安装豆包 SDK

   ```shell
   cd MaiBot
   uv pip install "volcengine-python-sdk[ark]"
   ```

## ⚠️ 注意事项

- 请妥善保管 API 密钥，不要在公开场合泄露，各平台 API 可能有调用频率限制，请注意控制使用频率，生成的图片内容受模型和提示词影响，请遵守相关平台的使用规范（注：梦羽AI和砂糖云支持NSFW，请自行判断）

## 常见问题

- **API 密钥未配置/错误**：请检查 `config.toml` 中对应模型的 api_key 配置。
- **模型不存在 / 用错模型**：默认模型字段填写 `[[models.items]]` 的 `id`；`/dr set` 和 `/dr default` 分别控制命令与智能 Tool，已有聊天覆盖可用 `/dr reset` 清除。
- **辅助 LLM 调用失败**：检查 `basic.llm_task_name` 对应的主程序模型任务是否可用，以及 `model_list`、服务商密钥与超时设置。
- **图片尺寸无效**：按所用服务和型号选择像素尺寸或比例 / 分辨率；NovelAI 宽高须为 64–2048 的 64 倍数，其他后端也有各自限制。
- **依赖缺失**：请确保 MaiBot 插件系统相关依赖已安装。

API 返回错误时，先查看日志中的服务端 `message`：

| HTTP 状态 | 排查方向 |
| --- | --- |
| 400 | 检查接口协议、服务端型号、尺寸及该模型支持的参数 |
| 401 | 检查 API Key 和对应后端要求的密钥格式 |
| 403 | 检查模型访问权限、账号认证和服务端报错 |
| 429 | 已触发服务商限流，按返回信息检查请求数、Token 或图片额度 |
| 503 / 504 | 服务繁忙或超时，可稍后重试 |

## 贡献和反馈

- **制作者水平有限，任何漏洞、疑问或建议,欢迎提交 [Issue](https://github.com/nguspring/selfie_painter/issues) 或 Pull Request！**
- **作者QQ：1021143806,3082618311或群1025591815（部分请求可能被过滤导致回复不及时，请见谅）**
- **其余问题请联系作者修复或解决**

---

## 🤝 插件开发历程

- 该插件基于 MaiBot 最早期官方豆包生图示例插件修改而来，最早我是为了兼容 GPT 生图进行修改，添加对 GPT 生图模型直接返回 base64 格式图片的兼容判断，因为 GPT 生图太贵了，所以后续想兼容魔搭社区的免费生图，新增一层报文兼容。（我不是计算机专业，大部分代码来自 DeepSeek R1 研究了很久，不得不说确实很好玩。）
- 目前支持三种报文返回，即三个平台的图片返回报文 url，image，base64，如果其他平台返回的报文符合以上三种格式也可以正常使用，可以自行尝试。
- MaiBot 0.8 版本更新，根据新插件系统进行重构。
- Rabbit-Jia-Er 加入，添加可以调用多个模型和命令功能。
- saberlights Kiuon 加入，添加自拍功能和自然语言命令功能。
- v4.0.0 迁移到 MaiBot SDK 2.x，并重构为 Pipeline + 装饰器子命令架构：
  - 4 条入口（智能 Action / `/dr` 风格 / `/dr` 自然语言 / 自动自拍 / 独立接口）复用同一组可组合 Step（解析模型 → 校验配置 → 协商尺寸 → 优化提示词 → 构造自拍提示词 → 合并负面 → 图生图回退 → 缓存查询 → 调用 API → 解析响应 → 解析图片 → 发送 → 写缓存 → 调度撤回）。
  - 子命令通过 `@subcommand` 装饰器注册到查找表，新增子命令只需在 `core/commands/handlers/` 下加一个函数。
  - 自拍提示词、动作池、风格约束、LLM 手部动作生成全部独立到 `core/prompts/selfie_prompt_builder.py`。
  - API 客户端依赖 `ClientContext` 而非 Action 实例，外部 `generate_image_standalone` 不再需要桩对象。

## 🔗 版权信息

- 作者：Ptrel, Rabbit-Jia-Er, saberlights Kiuon
- 许可证：AGPL-3.0-or-later
- 项目主页：https://github.com/nguspring/selfie_painter
