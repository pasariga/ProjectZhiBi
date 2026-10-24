[English](README.md) | [简体中文](README.zh-CN.md)

# 妙笔智改 · ZhiBi

**妙笔智改** is the full project name; **智笔** is its Chinese short name and **ZhiBi** its English identifier. The GitHub repository remains `ProjectZhiBi`. Existing 智笔 branding in the interface and source is intentional.

ZhiBi is a **historical Chinese writing-learning application** that brings writing assistance, essay correction, inline feedback and learning resources into one workspace. Its former hosted server has been stopped. This repository preserves source and resources for study and local exploration, not a currently maintained online service.

[WenHeng / 文衡](https://github.com/pasariga/multi_model_essay) developed later as a separate application. ZhiBi uses Flask/SQLite and writing-assistance flows; WenHeng uses Vue/FastAPI/PostgreSQL and independent multi-provider assessment. The later project's orchestration, research results or deployment status must not be attributed to ZhiBi. [OpenEssayScore](https://github.com/pasariga/OpenEssayScore) is a separate research toolkit, not a replacement name for either application.

## Implemented workflow

| Capability | Implementation |
| --- | --- |
| Writing assistance | Outline, opening/ending, detail expansion, continuation and full-text generation routes; selected responses stream through SSE. |
| Essay correction | Structured score/advice responses and a frontend radar presentation. These are application outputs, not validated educational accuracy claims. |
| Inline feedback | Highlights, suggestions, language/structure comments and ReportLab PDF export. |
| Image intake | Baidu OCR integration for extracting text from images. |
| Learning resources | Materials, local micro-course videos, writing history and behavior-based recommendation code. |
| Personal workspace | Registration/login, guest experience, profile/avatar handling and learning-history views. |

`routes/ai_services.py` includes output cleanup, length/context checks and explicit local fallback implementations. Configurable external calls and local rules are distinct: a rule-based fallback is not a successful model call and does not provide online OCR. The resource layer can use curated local materials when online search is not configured. No quantified learning or performance improvement is asserted.

## Architecture and source map

- [`app.py`](app.py) assembles Flask, initializes SQLite and registers route modules.
- [`index.html`](index.html) contains the HTML/CSS/JavaScript frontend, served by Flask without a Node build step.
- [`routes/`](routes/) separates authentication, application records and AI functions; [`routes/prompt_templates.py`](routes/prompt_templates.py) holds prompt construction.
- [`utils/`](utils/) contains API and database helpers; [`config/settings.py`](config/settings.py) reads environment configuration.
- `assets/`, `covers/` and `videos/` hold existing presentation and course materials. `archive/` and `index.ui1-backup.html` preserve interface references, not separate production versions.

The development history includes the original project and a later legacy-source preservation commit. This documentation work does not restart development or reconnect the old server.

## Local exploration

Use Python 3 and install the dependencies listed in [requirements.txt](requirements.txt):

```powershell
git clone https://github.com/pasariga/ProjectZhiBi.git
cd ProjectZhiBi
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe app.py
```

On Linux/macOS, use `python3`, `.venv/bin/python` and `cp`. The development entry point binds to `http://127.0.0.1:5000`, with `/health` for application health. Database tables and seed resources are initialized by the application.

Replace example placeholders with your own configuration, or leave unused provider keys empty. `ENABLE_LOCAL_FALLBACK=true` enables basic rule-based exploration without a model key; it does not enable OCR or real model generation. DeepSeek, Baidu OCR and optional search require their own valid service configuration. External providers receive the relevant submitted content. Do not commit real `.env`, databases, uploads or generated personal reports.

## Status and rights

The old deployment is retired. Local startup, live providers and report output were not rerun as part of this documentation-only review. No new release or service availability is claimed.

No repository-level license file is present at this revision. This README does not add a license grant or establish rights to redistribute third-party learning/media content. Existing source history and attribution remain unchanged; further public distribution of resource assets requires rights clarification.
