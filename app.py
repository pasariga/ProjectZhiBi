"""
智笔 AI 写作学习助手主程序入口
"""
import os
from flask import Flask, send_from_directory
from flask_cors import CORS
from utils.db_utils import init_db
from config.settings import BAIDU_OCR_API_KEY, BAIDU_OCR_SECRET_KEY, DEEPSEEK_API_KEY, ENABLE_LOCAL_FALLBACK, ENABLE_ONLINE_RESOURCE_SEARCH, SEARCH_API_KEY
from routes.auth import configure_auth_routes
from routes.api import configure_api_routes
from routes.ai_services import configure_ai_routes


def create_app():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(__name__, static_folder=base_dir, static_url_path='')
    CORS(app)
    init_db()
    configure_auth_routes(app)
    configure_api_routes(app)
    configure_ai_routes(app)


    @app.route('/', methods=['GET'])
    def index():
        return send_from_directory(base_dir, 'index.html')

    @app.route('/health', methods=['GET'])
    def health_check():
        return {'status': 'success', 'message': '服务正常运行中'}

    return app


app = create_app()

if __name__ == '__main__':
    print('🚀 智笔 AI 写作学习助手启动中...')
    print('✅ 数据库初始化完成')
    print('🔐 AI 服务:', '已配置' if DEEPSEEK_API_KEY else '未配置')
    print('🔍 OCR 服务:', '已配置' if (BAIDU_OCR_API_KEY and BAIDU_OCR_SECRET_KEY) else '未配置')
    print('🌐 学习资源:', '已启用在线资源接口' if (ENABLE_ONLINE_RESOURCE_SEARCH and SEARCH_API_KEY) else '使用精选资源库')
    print('🌐 访问地址: http://127.0.0.1:5000')
    print('=' * 50)
    app.run(host='127.0.0.1', port=5000)
