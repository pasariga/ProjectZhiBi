"""
项目配置文件
注意：API Key 不再硬编码到源码中，请通过环境变量或 .env 文件配置。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv(BASE_DIR / '.env')

DATABASE = os.getenv('ZHIBI_DATABASE', str(BASE_DIR / 'essay.db'))

DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '').strip()
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
DEEPSEEK_API_URL = os.getenv('DEEPSEEK_API_URL', f'{DEEPSEEK_BASE_URL}/chat/completions').strip()
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-chat').strip()

BAIDU_OCR_API_KEY = os.getenv('BAIDU_OCR_API_KEY', '').strip()
BAIDU_OCR_SECRET_KEY = os.getenv('BAIDU_OCR_SECRET_KEY', '').strip()
OCR_TOKEN_URL = os.getenv('OCR_TOKEN_URL', 'https://aip.baidubce.com/oauth/2.0/token')
OCR_RECOGNIZE_URL = os.getenv('OCR_RECOGNIZE_URL', 'https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic')
OCR_REQUEST_INTERVAL = float(os.getenv('OCR_REQUEST_INTERVAL', '1.0'))

# 未配置 AI Key 时启用基础规则结果，保证核心流程可用。
ENABLE_LOCAL_FALLBACK = os.getenv('ENABLE_LOCAL_FALLBACK', 'true').lower() in {'1', 'true', 'yes', 'on'}


# 个性化资源推荐：默认开启，但未配置搜索 API 时会使用合规精选资源库。
ENABLE_ONLINE_RESOURCE_SEARCH = os.getenv('ENABLE_ONLINE_RESOURCE_SEARCH', 'true').lower() in {'1', 'true', 'yes', 'on'}
SEARCH_PROVIDER = os.getenv('SEARCH_PROVIDER', 'custom').strip().lower()
SEARCH_API_KEY = os.getenv('SEARCH_API_KEY', '').strip()
SEARCH_API_URL = os.getenv('SEARCH_API_URL', '').strip()
SEARCH_RESULT_LIMIT = int(os.getenv('SEARCH_RESULT_LIMIT', '5'))
