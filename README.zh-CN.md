[English](README.md) | [简体中文](README.zh-CN.md)

# 妙笔智改 · ZhiBi

项目正式全称为**妙笔智改**，简称**智笔**，英文标识为 **ZhiBi**，GitHub 仓库名暂时保留 `ProjectZhiBi`。界面和源码中既有的“智笔”是正常简称，不是命名错误。

妙笔智改是一个**历史中文写作学习项目**，把写作辅助、作文批改、智能旁批和学习资源整合到同一工作台。原在线服务器已经停止；本仓库用于保留源码与资源、阅读实现和本地探索，不代表仍在维护的在线服务。

[文衡](https://github.com/pasariga/multi_model_essay)是后续独立发展的应用。智笔采用 Flask／SQLite 和写作辅助流程；文衡采用 Vue／FastAPI／PostgreSQL 和多提供方独立评测。后者的评审编排、研究结果或部署状态不能算作智笔的成果。[OpenEssayScore](https://github.com/pasariga/OpenEssayScore) 则是独立研究工具包，不是这两个应用的改名。

## 已有功能

| 能力 | 实现内容 |
| --- | --- |
| 写作辅助 | 提纲、开头／结尾、细节扩写、续写和全文生成路由，部分结果使用 SSE 流式返回。 |
| 作文批改 | 结构化评分／建议和前端雷达图；它们是应用输出，不是经过验证的教学准确率结论。 |
| 智能旁批 | 亮点、建议、字词／结构批注，以及 ReportLab PDF 导出。 |
| 图片导入 | 调用百度 OCR 提取图片文字。 |
| 学习资源 | 素材、本地微课视频、写作历史和基于学习行为的推荐代码。 |
| 个人空间 | 注册／登录、游客体验、资料／头像管理和学习历史。 |

`routes/ai_services.py` 包含输出清洗、长度／上下文检查和明确的本地回退实现。外部调用与规则回退需要区分：本地建议不等于真实模型调用成功，也不提供在线 OCR。未配置在线搜索时，资源层可使用本地精选素材。本说明不声称量化的学习效果或性能提升。

## 架构与源码入口

- [`app.py`](app.py)：组装 Flask、初始化 SQLite 并注册路由。
- [`index.html`](index.html)：HTML／CSS／JavaScript 前端，由 Flask 提供，无需 Node 构建步骤。
- [`routes/`](routes/)：区分账号、应用记录和 AI 功能；[`routes/prompt_templates.py`](routes/prompt_templates.py) 负责提示词构造。
- [`utils/`](utils/)：外部 API 与数据库辅助；[`config/settings.py`](config/settings.py) 读取环境配置。
- `assets/`、`covers/`、`videos/`：已有展示与课程资料。`archive/` 和 `index.ui1-backup.html` 是界面参考，不是独立生产版本。

历史记录包含原始项目和后续旧源码保存提交。本次文档工作不代表重新开发或重连旧服务器。

## 本地探索

准备 Python 3，按 [requirements.txt](requirements.txt) 安装依赖：

```powershell
git clone https://github.com/pasariga/ProjectZhiBi.git
cd ProjectZhiBi
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe app.py
```

Linux／macOS 对应使用 `python3`、`.venv/bin/python` 和 `cp`。开发入口绑定 `http://127.0.0.1:5000`，`/health` 用于应用健康检查；数据表和初始资源由应用初始化。

将模板占位符改为自己的配置，不使用的 Key 项留空。`ENABLE_LOCAL_FALLBACK=true` 可在没有模型 Key 时体验基础规则，但不能启用 OCR 或真实模型生成。DeepSeek、百度 OCR 和可选搜索分别需要有效服务配置，外部服务会接收相应提交内容。不要提交真实 `.env`、数据库、上传内容或个人报告。

## 状态与权利

旧部署已经退役。本轮仅审阅文档和源码，没有重新运行本地启动、在线提供方或报告导出，也没有发布新版本或承诺在线可用性。

当前没有仓库级许可证文件，本文不新增授权，也不能据此认定第三方学习／媒体内容可以再分发。既有历史和署名保持不变，资源的后续公开分发范围仍需要确认。
