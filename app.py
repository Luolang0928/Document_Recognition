import os
import base64
import sqlite3
import requests
import json
from flask import Flask, render_template, request, jsonify, redirect, url_for, g
from flask_caching import Cache
from datetime import datetime
from werkzeug.utils import secure_filename

# 应用配置
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-for-smart-doc')
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB文件限制
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg'}  # 允许的图片格式

# 缓存配置
app.config['CACHE_TYPE'] = 'simple'
cache = Cache(app)

# 数据库配置
DATABASE = os.path.join(os.path.dirname(__file__), 'recognize_history.db')

# 移动云Qwen-VL模型配置
QWEN_API_URL = os.environ.get('QWEN_API_URL', 'http://zhenze-huhehaote.cmecloud.cn/v1/chat/completions')
QWEN_API_KEY = os.environ.get('QWEN_API_KEY', 'Y71W_IiWKmgWf2FFaHz2yPNwjJkrfG6P_hVy7al1Ylg')

# 确保上传目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# 数据库连接管理
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


# 初始化数据库
def init_db():
    db = get_db()
    with app.open_resource('schema.sql') as f:
        db.executescript(f.read().decode('utf8'))


@app.cli.command('init-db')
def init_db_command():
    """清除现有数据并创建新表"""
    init_db()
    print('数据库初始化完成.')


# 保存识别结果到数据库
def save_result(result):
    db = get_db()
    db.execute(
        '''INSERT INTO recognize_history 
        (name, model, spec, manufacturer, production_date, shipment_date, batch_number, remark)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            result.get('name', '-'),
            result.get('model', '-'),
            result.get('spec', '-'),
            result.get('manufacturer', '-'),
            result.get('production_date', ''),
            result.get('shipment_date', ''),
            result.get('batch_number', '-'),
            result.get('remark', '')
        )
    )
    db.commit()


# 获取所有识别历史
def get_history():
    db = get_db()
    return db.execute(
        'SELECT * FROM recognize_history ORDER BY create_time DESC'
    ).fetchall()


def allowed_file(filename):
    """检查文件是否为允许的格式"""
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


# 调用移动云Qwen-VL模型
def call_model_api(image_path):
    """调用移动云Qwen-VL模型进行单据识别"""
    try:
        # 读取图片并转换为base64
        with open(image_path, 'rb') as f:
            image_data = base64.b64encode(f.read()).decode('utf-8')

        # 构造请求数据
        payload = {
            "model": "qwen-vl",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"text": "识别这张单据中的产品名称、型号、规格、生产厂家、生产日期、出厂日期和批号。"},
                        {"image": image_data}
                    ]
                }
            ],
            "parameters": {
                "temperature": 0.2,
                "top_p": 0.8
            }
        }

        # 调用Qwen-VL API
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {QWEN_API_KEY}'
        }

        response = requests.post(
            QWEN_API_URL,
            headers=headers,
            json=payload,
            timeout=60  # 单据识别可能耗时较长
        )
        response.raise_for_status()
        result = response.json()

        # 解析模型返回结果
        # 注意：以下解析逻辑需要根据Qwen-VL实际返回格式调整
        parsed_result = parse_model_output(result)
        return parsed_result

    except Exception as e:
        app.logger.error(f"模型接口调用失败: {str(e)}")
        return None


# 解析模型输出
def parse_model_output(model_response):
    """解析Qwen-VL模型的输出，提取关键信息"""
    try:
        # 假设模型返回格式为{"content": "产品名称: 热轧钢板; 型号: Q235B; ..."}
        content = model_response.get('choices', [{}])[0].get('message', {}).get('content', '')

        # 简单的文本解析（实际应用中可能需要更复杂的处理）
        fields = {
            'product_name': '-',
            'model': '-',
            'specification': '-',
            'manufacturer': '-',
            'production_date': '',
            'shipment_date': '',
            'batch_number': '-',
            'confidence': '90'  # 示例值，实际应从模型输出中提取
        }

        # 提取关键信息（根据实际返回格式调整）
        for line in content.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip().lower()
                value = value.strip()

                if '产品名称' in key:
                    fields['product_name'] = value
                elif '型号' in key:
                    fields['model'] = value
                # 其他字段...

        return fields

    except Exception as e:
        app.logger.error(f"解析模型输出失败: {str(e)}")
        return None


# 路由：首页（单据识别）
@app.route('/')
def index():
    return render_template('index.html')


# 路由：处理图片上传
@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'status': 'error', 'message': '未选择文件'}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({'status': 'error', 'message': '未选择文件'}), 400

        if file and allowed_file(file.filename):
            # 安全处理文件名
            filename = secure_filename(f"temp_{os.urandom(8).hex()}.{file.filename.rsplit('.', 1)[1].lower()}")
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            return jsonify({
                'status': 'success',
                'message': '文件上传成功',
                'filename': filename
            })

        return jsonify({'status': 'error', 'message': '不支持的文件格式'}), 400

    except Exception as e:
        app.logger.error(f"上传文件失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '上传过程中发生错误'}), 500


# 路由：调用模型识别
@app.route('/recognize', methods=['POST'])
def recognize():
    try:
        data = request.json
        if not data or 'filename' not in data:
            return jsonify({'status': 'error', 'message': '参数错误'}), 400

        filename = data['filename']
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        if not os.path.exists(filepath):
            return jsonify({'status': 'error', 'message': '文件不存在'}), 404

        # 检查缓存（避免重复识别同一文件）
        cache_key = f"ocr_result_{filename}"
        cached_result = cache.get(cache_key)

        if cached_result:
            return jsonify({
                'status': 'success',
                'result': cached_result,
                'from_cache': True
            })

        # 调用大模型接口
        result = call_model_api(filepath)

        if not result:
            return jsonify({
                'status': 'error',
                'message': '识别失败，请重试'
            }), 500

        # 格式化结果（适配前端表单字段）
        formatted_result = {
            'nameResult': result.get('product_name', '-'),
            'modelResult': result.get('model', '-'),
            'specResult': result.get('specification', '-'),
            'manufacturerResult': result.get('manufacturer', '-'),
            'productionDateResult': result.get('production_date', ''),
            'shipmentDateResult': result.get('shipment_date', ''),
            'batchNumberResult': result.get('batch_number', '-'),
            'remarkResult': f"识别置信度：{result.get('confidence', 'N/A')}%"
        }

        # 缓存结果
        cache.set(cache_key, formatted_result, timeout=3600)  # 缓存1小时

        return jsonify({
            'status': 'success',
            'result': formatted_result,
            'from_cache': False
        })

    except Exception as e:
        app.logger.error(f"识别过程发生错误: {str(e)}")
        return jsonify({'status': 'error', 'message': '识别过程中发生错误'}), 500


# 路由：保存识别结果
@app.route('/save', methods=['POST'])
def save():
    try:
        data = request.json
        if not data:
            return jsonify({'status': 'error', 'message': '参数错误'}), 400

        # 转换前端字段名到数据库字段名
        db_data = {
            'name': data.get('nameResult', '-'),
            'model': data.get('modelResult', '-'),
            'spec': data.get('specResult', '-'),
            'manufacturer': data.get('manufacturerResult', '-'),
            'production_date': data.get('productionDateResult', ''),
            'shipment_date': data.get('shipmentDateResult', ''),
            'batch_number': data.get('batchNumberResult', '-'),
            'remark': data.get('remarkResult', '')
        }

        # 保存到数据库
        save_result(db_data)

        return jsonify({'status': 'success', 'message': '结果已保存'})

    except Exception as e:
        app.logger.error(f"保存结果失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '保存过程中发生错误'}), 500


# 路由：识别历史
@app.route('/history')
def history():
    try:
        history_data = get_history()
        return render_template('history.html', history=history_data)
    except Exception as e:
        app.logger.error(f"获取历史记录失败: {str(e)}")
        return render_template('error.html', message='获取历史记录失败'), 500


if __name__ == '__main__':
    # 确保数据库表存在
    init_db()
    app.run(debug=True)