# 妙笔智改

妙笔智改是一款面向作文练习的 AI 写作学习应用，将写作辅助、作文批改、智能旁批与学习资源整合到同一个工作台。学生可以从提纲和初稿开始，结合评分、批注和修改建议完善文章，再通过素材与微课开展针对性练习。

项目提供完整的前端页面、后端接口和本地资源，适合开发者与学生阅读源码、运行体验，了解 AI 写作工具的实现方式。

## 功能特色

| 功能 | 实现内容 |
| --- | --- |
| 写作辅助 | 生成提纲、开头与结尾，支持细节扩写、续写和全文生成 |
| 作文批改 | 从内容、结构、语言、修辞、规范等维度给出评分与修改建议，并在页面展示评分雷达图 |
| 智能旁批 | 在原文中呈现亮点、修改建议、字词纠错和结构提醒，支持批注报告 PDF 导出 |
| 图片识别 | 通过百度 OCR 提取图片中的文字，辅助导入作文内容 |
| 学习资源 | 提供写作素材、本地微课视频，并结合写作记录与学习行为生成个性化推荐 |
| 个人空间 | 支持注册、登录、游客体验、资料与头像管理，以及写作历史和学习进度查看 |

部分写作接口采用流式返回，生成内容可逐步显示。未配置模型服务时，部分功能可使用内置基础规则完成体验；接入 DeepSeek 后可使用模型生成能力。

## 技术架构

浏览器页面通过 HTTP 接口与 Flask 后端交互；后端负责数据库操作、外部服务调用和报告生成。

| 层次 | 技术与职责 |
| --- | --- |
| 前端 | 原生 HTML、CSS、JavaScript；页面入口为 `index.html`，由 Flask 提供静态文件 |
| 后端 | Python、Flask、flask-cors；按账号、学习记录、AI 服务划分路由模块 |
| 数据存储 | SQLite；自动建立数据表和初始学习资源 |
| AI 与 OCR | Requests 调用 DeepSeek、百度 OCR；部分接口通过 SSE 返回生成结果 |
| 报告导出 | ReportLab 生成中文批注 PDF |

前端无需单独安装 Node.js 依赖或执行构建命令。Python 依赖见 [requirements.txt](requirements.txt)。

## 安装与运行

准备 Python 3、Git，并在终端中克隆项目：

```sh
git clone https://github.com/pasariga/ProjectZhiBi.git
cd ProjectZhiBi
```

**Windows PowerShell：**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

**macOS / Linux：**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，按照下节配置所需服务。模板中的 `your_...` 是占位符，必须替换为自己的凭证；不使用的 Key 项应留空，不能保留占位符。

完成配置后，在项目根目录启动：

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe app.py
```

```sh
# macOS / Linux
.venv/bin/python app.py
```

浏览器打开 <http://127.0.0.1:5000>，即可注册账号或使用游客体验。首次运行会自动创建 `essay.db`、数据表及初始素材和微课记录，无需手动导入数据库。健康检查地址为 <http://127.0.0.1:5000/health>。

以上命令按 `app.py` 中的默认地址启动本地服务，默认仅监听 `127.0.0.1:5000`。

## 配置说明

配置由 [config/settings.py](config/settings.py) 读取，支持项目根目录的 `.env` 文件和系统环境变量。常用配置如下：

| 配置项 | 用途与默认值 |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key；留空时可体验部分基础规则功能 |
| `DEEPSEEK_MODEL` | 模型名称，默认 `deepseek-chat` |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_URL` | 模型服务地址；默认使用模板中的 DeepSeek 接口地址 |
| `BAIDU_OCR_API_KEY` / `BAIDU_OCR_SECRET_KEY` | 百度 OCR 凭证；使用图片识别时需要填写 |
| `ENABLE_LOCAL_FALLBACK` | 是否在支持的接口中启用基础规则回退，默认 `true` |
| `ENABLE_ONLINE_RESOURCE_SEARCH` | 是否启用外部资源搜索，默认 `true`；还需配置搜索服务 |
| `SEARCH_PROVIDER` / `SEARCH_API_KEY` / `SEARCH_API_URL` | 搜索接口类型、凭证与地址；请求格式见 [utils/api_utils.py](utils/api_utils.py) |
| `SEARCH_RESULT_LIMIT` | 搜索结果数量，默认 `5` |
| `ZHIBI_DATABASE` | SQLite 文件路径，默认位于项目根目录的 `essay.db` |

只体验基础流程时，可以在 `.env` 中将以下项设置为空，并保留基础规则回退开关：

```dotenv
DEEPSEEK_API_KEY=
BAIDU_OCR_API_KEY=
BAIDU_OCR_SECRET_KEY=
SEARCH_API_KEY=
SEARCH_API_URL=
ENABLE_LOCAL_FALLBACK=true
```

此配置不提供在线模型生成和 OCR 服务。未配置可用的外部搜索服务时，资源推荐可使用内置精选资源。

真实 `.env`、本地数据库、用户上传文件与生成的报告不纳入版本控制；请勿将个人凭证提交到公开仓库。

## 目录说明

```text
ProjectZhiBi/
├── app.py                  Flask 应用入口与健康检查
├── index.html              应用主页面与前端交互
├── routes/                 账号、历史记录、AI、OCR 与推荐接口
├── utils/                  外部 API 调用、数据库与初始化逻辑
├── config/                 环境变量配置
├── data/                   学习资源数据
├── assets/                 样式、图标、图片和首页视频
├── covers/                 微课封面
├── videos/                 本地微课视频
├── scripts/                视频封面生成等辅助脚本
├── archive/                界面版本参考文件
├── index.ui1-backup.html    页面版本参考
├── requirements.txt        Python 依赖清单
└── .env.example            环境变量配置模板
```

阅读源码时，可以从 [app.py](app.py) 了解应用组装方式，再查看 [routes/ai_services.py](routes/ai_services.py) 中的写作与批改流程、[routes/prompt_templates.py](routes/prompt_templates.py) 中的提示词，以及 [utils/db_utils.py](utils/db_utils.py) 中的数据结构和资源初始化逻辑。
