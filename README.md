# 妙笔智改（旧项目归档）

一个面向作文练习的 AI 写作学习原型。源码中的界面和启动名称为「智笔 AI 写作学习助手」。本仓库独立保存旧项目，不包含「文衡智评」的代码或配置。

## 原有架构

- 前端：`index.html` 中的原生 HTML、CSS 和 JavaScript，样式与图片在 `assets/`，由 Flask 同源提供静态文件，没有独立的 Node.js 前端构建流程。
- 后端：Python、Flask、flask-cors；入口为 `app.py`，接口位于 `routes/`。
- 数据：SQLite，建表和初始学习资源见 `utils/db_utils.py`。
- 外部服务：DeepSeek 文本生成、百度 OCR，以及可选的学习资源搜索接口；配置统一从环境变量或 `.env` 读取。
- 导出：ReportLab 生成批注 PDF；部分接口使用 SSE 流式返回。

## 主要功能

- 账号注册、登录、游客体验和个人资料管理。
- 作文提纲、开头、细节扩写、全文生成、续写和结尾辅助。
- 作文批改、结构优化、语言润色、智能批注和 PDF 导出。
- 图片文字识别、写作历史、学习进度与个性化资源推荐。
- 本地微课视频、封面和写作素材展示。

未配置有效的 DeepSeek Key 时，部分写作功能可使用原有基础规则回退；OCR 需要自行配置百度凭证。

## 本地运行

在 Windows PowerShell 中进入仓库根目录：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env：填写需要使用的服务凭证，不使用的 Key 项留空。
.\.venv\Scripts\python.exe app.py
```

浏览器打开 <http://127.0.0.1:5000>。首次运行会自动创建 `essay.db`、数据表和初始学习资源，可以在页面注册账号或使用游客体验。数据库位置可通过 `ZHIBI_DATABASE` 指定。

`requirements.txt` 保留原有依赖范围。本次归档未安装依赖、启动服务或验证运行环境。

## 目录

```text
app.py                  Flask 入口
index.html              当前旧版页面
index.ui1-backup.html    原目录中的历史页面备份
routes/                 登录、历史、AI、OCR、推荐与导出接口
utils/                  外部服务调用、数据库及初始化
config/                 环境变量配置读取
data/                   原有学习资源 JSON
assets/                 样式、图标、图片和首页视频
covers/                 微课封面
videos/                 随项目附带的微课视频
scripts/                视频封面生成辅助脚本
archive/                嵌套旧副本中有差异的界面文件
.env.example            无真实凭证的配置模板
```

## 归档说明

保留原有源码和资源目录，未重构业务或升级依赖。仅移除了 `config/settings.py` 中两处硬编码的 OCR 凭证默认值，仍通过原有环境变量读取。

真实 `.env`、SQLite 数据库、用户上传头像、生成的 PDF 报告、运行日志、缓存及本机工具元数据没有提交，原文件仍保留在本地。数据库结构和初始公共资源可由源码重新建立。历史副本的差异说明见 [archive/README.md](archive/README.md)。

这是历史项目源码归档，未进行部署或生产可用性验证。
