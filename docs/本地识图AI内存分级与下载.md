# 本地识图 AI：按内存分级与下载

工程助手用本地视觉模型做图纸复核，**图纸不出电脑、断网也能用**。模型权重不放进本仓库（单个模型文件约 5.5 GB，超过 GitHub 普通仓库 100 MB 单文件限制），由安装脚本从 Ollama 官方源下载到 `local_ai/models/`。

## 一、先选档位

| 物理内存 | 推荐模型 | 下载体积 | 说明 |
|---:|---|---:|---|
| 8–15 GB | `qwen2.5vl:3b` | 3.2 GB | 能跑，但复杂三视图、剖视图、局部放大图准确率有限，必须人工复核 |
| 16–47 GB | `qwen2.5vl:7b` | 6.0 GB | **当前实机重点验证档**，NX / SolidWorks 同时运行较稳妥 |
| 48–95 GB | `qwen2.5vl:32b` | 21 GB | 复杂图纸档；32 GB 机器不建议默认开，叠加 CAD 容易吃紧 |
| 96 GB 及以上 | `qwen2.5vl:72b` | 49 GB | 高精度大模型，模型本身即占 49 GB 磁盘 |

更细的常见内存档：

| 内存 | 选择 | 理由 |
|---:|---|---|
| 8 GB | `qwen2.5vl:3b` | 再大加载不稳 |
| 16 GB | `qwen2.5vl:7b` | 本机 15.7 GB 实测可用 |
| 32 GB | `qwen2.5vl:7b` | 32b 加载后与 CAD 抢内存，风险高 |
| 48 GB | `qwen2.5vl:32b` | 可上复杂图纸 |
| 64 GB | `qwen2.5vl:32b` | 余量更足，仍不建议无必要时上 72b |
| 96 GB+ | `qwen2.5vl:72b` | 高精度档 |

> 注意：只有 `qwen2.5vl:7b` 做过完整 CAD 图纸端到端验收。3b / 32b / 72b 只按模型官方能力分级，**尚未做同等级别验收**，不能宣称与 7b 完全等效。任何档位识别出的图纸仍要过软件自带的完整性与矛盾校验；缺尺寸、视图不全、数据打架时软件直接报错，**不会让模型清洗参数、补数据或修复矛盾**。

## 二、官方下载地址

### 运行器

- Ollama Windows 安装包：<https://ollama.com/download/windows>

### 模型（Ollama 官方库）

| 模型 | 官方页面 | 拉取命令 |
|---|---|---|
| `qwen2.5vl:3b` | <https://ollama.com/library/qwen2.5vl:3b> | `ollama pull qwen2.5vl:3b` |
| `qwen2.5vl:7b` | <https://ollama.com/library/qwen2.5vl:7b> | `ollama pull qwen2.5vl:7b` |
| `qwen2.5vl:32b` | <https://ollama.com/library/qwen2.5vl:32b> | `ollama pull qwen2.5vl:32b` |
| `qwen2.5vl:72b` | <https://ollama.com/library/qwen2.5vl:72b> | `ollama pull qwen2.5vl:72b` |

量化版（精度更高、内存占用也更高，按需手动选择）：

| 模型 | 体积 | 拉取命令 |
|---|---:|---|
| `qwen2.5vl:3b-q8_0` | 4.6 GB | `ollama pull qwen2.5vl:3b-q8_0` |
| `qwen2.5vl:7b-q8_0` | 9.4 GB | `ollama pull qwen2.5vl:7b-q8_0` |
| `qwen2.5vl:32b-q8_0` | 36 GB | `ollama pull qwen2.5vl:32b-q8_0` |

## 三、安装（推荐：自动选型）

在仓库根目录执行：

```powershell
python scripts\install_local_ai.py
```

脚本会：

1. 读取本机物理内存；
2. 打印推荐档位；
3. 启动软件自带的便携 Ollama（端口 11511，不与系统 Ollama 抢端口）；
4. 执行 `ollama pull` 下载推荐模型到 `local_ai/models/`。

常用参数：

```powershell
python scripts\install_local_ai.py --check                  # 只检查，不下载
python scripts\install_local_ai.py --list                   # 查看全部档位
python scripts\install_local_ai.py --model qwen2.5vl:7b     # 手动指定型号
```

下载可断点续传。中途失败重新执行同一命令即可。

## 四、安装后怎么生效

软件启动时读取 `%APPDATA%\EngineeringAssistant\settings.json`：

```json
{
  "local_ai_enabled": true,
  "local_ai_model": "qwen2.5vl:7b",
  "local_ai_url": "http://127.0.0.1:11511"
}
```

- 没有手动配置过 `local_ai_model` 时，软件按内存自动选推荐档；
- 用户手动写进 `settings.json` 的模型优先，软件不会覆盖；
- 旧版 `qwen3-vl:2b` 会被自动替换（2b 结构化读图不合格，已停用）。

## 五、磁盘与内存提示

- 模型下载占磁盘：3b 约 3.2 GB，7b 约 6 GB，32b 约 21 GB，72b 约 49 GB；
- 运行时还要额外内存，建议给模型留出「模型体积 + 4 GB」以上的空闲内存；
- 同一时间只开一个 CAD 软件（NX 或 SolidWorks），不要多开；
- 内存不足时软件会拒绝启动识图或降级为纯规则识图，不会硬撑到崩溃。

## 六、自检

```powershell
python -c "from app import ai_tiers; print(ai_tiers.describe())"
python scripts\install_local_ai.py --check
```

第一条打印本机内存、推荐模型和全部档位；第二条确认模型是否真的已安装。
