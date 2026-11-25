# 智能TTS（MaiBot 插件）

通过回复扩展（`@ReplyExtension`），让麦麦在调用 `reply` 时自己决定要不要给回复附加语音。

- Planner 不填 `plugin_options` 里的对应键：正常纯文字回复。
- 填了：按指定厂商和音色合成语音，追加或替换文字。
- 合成失败、超时、厂商没配置：静默回退成纯文字，不中断对话。

> **状态说明**：本插件按各厂商官方文档和 MaiBot SDK v2.23.0 / 主程序 v1.4.8 的源码编写，但**尚未在真实的 MaiBot 环境中实际运行验证过**，也没有用真实 API Key 调用过任何一家厂商。首次使用请先在一个聊天里试好，再放开使用。遇到问题欢迎提 Issue。

## 环境要求

- MaiBot 1.3.5 ～ 1.4.x（需要支持回复扩展的主程序）
- `maibot-plugin-sdk` >= 2.23.0
- Python 依赖 `httpx`、`edge-tts`，MaiBot 加载插件时会自动安装

## 安装

把仓库放到 MaiBot 的 `plugins/` 目录下，保证路径是 `plugins/smart-tts/plugin.py`：

```bash
cd <MaiBot 目录>/plugins
git clone https://github.com/WinTerminal/smart-tts.git
```

然后在 WebUI 的插件页确认「智能TTS」已加载并启用。

## 配置

在 WebUI 插件页打开「智能TTS」配置，或在 `plugins/smart-tts/config.toml` 里改（保存后自动热重载）。

最少只需要填两样：

| 设置 | 说明 |
|---|---|
| `plugin.default_provider` | Planner 没指定厂商时用哪家 |
| `providers.<厂商>.api_key` | 你要用的那家的 Key |

每家厂商都可以改：`api_key`、`base_url`（留空用官方地址）、`default_voice`、`model`，以及个别厂商的特有字段。没填 Key 的厂商被选中时会自动回退成纯文字。

### 支持的厂商

| 厂商 | `provider` | 除 `api_key` 外必填 | 默认音色 |
|---|---|---|---|
| OpenAI | `openai` | | `coral` |
| DeepSeek | `deepseek` | | `stella` |
| Gemini | `gemini` | | `Kore` |
| Azure | `azure` | `region` | `zh-CN-XiaoxiaoNeural` |
| MiniMax | `minimax` | | `male-qn-qingse` |
| 阿里云百炼 CosyVoice | `qwen` | `base_url` | `longanyang` |
| 阿里云百炼 Qwen-Audio-TTS | `qwen_audio` | `base_url` | `longanhuan_v3.1` |
| Groq | `groq` | | `troy` |
| xAI | `xai` | | `eve` |
| ElevenLabs | `elevenlabs` | | `JBFqnCBsd6RMkjVDRZzb` |
| 小米 MiMo | `mimo` | | `mimo_default` |
| 阶跃星辰 StepFun | `stepfun` | | `cixingnansheng` |
| Fish Audio | `fish_audio` | `reference_id` | — |
| 硅基流动 | `siliconflow` | | `FunAudioLLM/CosyVoice2-0.5B:claire` |
| 火山引擎（豆包） | `volcengine` | | `zh_female_vv_uranus_bigtts` |
| 智谱 | `zhipu` | | `tongtong` |
| 腾讯云 | `tencent` | `secret_id` | `101001` |
| Edge TTS | `edge` | 不需要 Key | `zh-CN-XiaoxiaoNeural` |

### 几家厂商的注意点

**接入地址填到哪一级**：插件会在你填的地址后面自己拼路径，所以填到下面这层为止。

| 厂商 | `base_url` 填到 |
|---|---|
| OpenAI、Groq、MiniMax、xAI、MiMo、StepFun、ElevenLabs、硅基流动、智谱 | 到 `/v1`（智谱到 `/api/paas/v4`） |
| DeepSeek | `https://api.deepseek.com` |
| Gemini | 到 `/v1beta` |
| Qwen 系列 | 到 `/api/v1` |
| Fish Audio | `https://api.fish.audio` |
| 火山引擎 | 完整的 `https://openspeech.bytedance.com/api/v3/tts/unidirectional` |
| Azure、腾讯云、Edge TTS | 不用填 |

- **火山引擎**：`model` 填的是资源 ID（`seed-tts-2.0` 或 `seed-tts-1.0`），必须和音色所属的模型版本一致。旧版控制台需要再填 `app_id`，此时 `api_key` 填 Access Token。
- **硅基流动**：系统预置音色要写成“模型名:音色名”。
- **腾讯云**：`api_key` 填 SecretKey，`secret_id` 填 SecretId，`default_voice` 填音色编号（整数）；单次最多 150 个汉字。
- **Groq**：只有英文和沙特阿拉伯语模型，单次最多 200 字符，不适合中文。
- **Edge TTS**：免 Key，但调用的是微软 Edge 浏览器“大声朗读”的非官方接口，可能被限流，不建议作为唯一方案。
- **中转站**：默认模型和音色在中转站上不一定有，需要按它支持的改。DeepSeek、Gemini、MiMo 的语音接口不是 OpenAI 兼容格式，一般的 OpenAI 中转站转发不了。

### 语速

`plugin.speed`（0.5～2.0）只对支持的厂商生效：OpenAI、MiniMax、xAI、StepFun、硅基流动、火山引擎、智谱、Edge TTS。其他厂商会忽略。

## 使用

不需要额外操作。启用后，Planner 在调用 `reply` 时会看到 `smart-tts-plugin.smart-tts.voice` 这个可选项，由它自行决定是否使用：

| Planner 填写 | 效果 |
|---|---|
| 不填 | 纯文字回复 |
| `{"provider": "deepseek"}` | 用 DeepSeek 合成，语音追加在文字后面 |
| `{"provider": "openai", "voice": "nova"}` | 指定厂商和音色 |
| `{"keep_text": false}` | 文字被语音替换，图片、表情、@ 等附件保留 |

## 已知限制

- 音频格式、Azure 的 SSML 语言（固定 `zh-CN`）和输出码率、MiniMax 的采样率等参数写死在代码里，不能通过配置修改。
- Groq、StepFun、腾讯云有单次文本长度上限，超长会回退成纯文本，不会拆分合成。
- MaiBot 自带的 `attach_voice` 参数（用模型库里配置的 TTS）与本插件是两条独立的路径，Planner 面对两个语音选项时的选择表现未验证。

## 许可证

[MIT](LICENSE)
