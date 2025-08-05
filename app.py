import os
import json
import requests
import time
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, jsonify
from flask_caching import Cache
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import boto3  # 移动云EOS依赖（兼容S3协议）
from botocore.exceptions import ClientError

# 初始化应用
app = Flask(__name__)

# 核心配置
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-for-doc-recognition')
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB限制
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg'}

# 移动云EOS配置（生产环境建议用环境变量）
MOBILECLOUD_EOS_ACCESS_KEY = os.environ.get('EOS_ACCESS_KEY', "HOG91Q1TB5E9I8ZZ0V6G")
MOBILECLOUD_EOS_SECRET_KEY = os.environ.get('EOS_SECRET_KEY', "4nSdV7PUF2RhHw29mdmXHtJMD7P8DUDlFbEQTt1u")
MOBILECLOUD_EOS_ENDPOINT = os.environ.get('EOS_ENDPOINT', "https://eos.chengdu-zs-1.cmecloud.cn")
MOBILECLOUD_EOS_BUCKET = os.environ.get('EOS_BUCKET', "cloudchain")

# 模型API配置
QWEN_API_URL = os.environ.get('QWEN_API_URL', 'http://zhenze-huhehaote.cmecloud.cn/v1/chat/completions')
QWEN_API_KEY = os.environ.get('QWEN_API_KEY', 'Y71W_IiWKmgWf2FFaHz2yPNwjJkrfG6P_hVy7al1Ylg')
QWEN_MODEL = "Qwen2.5-VL-72B-Instruct"

# 初始化移动云EOS客户端
s3_client = boto3.client(
    's3',
    aws_access_key_id=MOBILECLOUD_EOS_ACCESS_KEY,
    aws_secret_access_key=MOBILECLOUD_EOS_SECRET_KEY,
    endpoint_url=MOBILECLOUD_EOS_ENDPOINT,
    region_name='chengdu-zs-1'  # 与Endpoint地域匹配
)

# 数据库与缓存配置
app.config['CACHE_TYPE'] = 'simple'
cache = Cache(app)
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(os.path.dirname(__file__), 'recognize_history.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'connect_args': {'check_same_thread': False}  # 解决SQLite多线程问题
}
db = SQLAlchemy(app)

# 初始化目录
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
CORS(app)


# 数据库模型
class RecognizeHistory(db.Model):
    __tablename__ = 'recognize_history'
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.Text, default='-')
    model = db.Column(db.Text, default='-')
    spec = db.Column(db.Text, default='-')
    manufacturer = db.Column(db.Text, default='-')
    production_date = db.Column(db.Text, default='')
    shipment_date = db.Column(db.Text, default='')
    batch_number = db.Column(db.Text, default='-')
    remark = db.Column(db.Text, default='')
    create_time = db.Column(db.DateTime, default=datetime.utcnow)


# 辅助函数：检查文件格式
def allowed_file(filename):
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


# EOS上传函数（带完整校验）
def upload_to_mobilecloud_eos(image_path):
    """上传图片到移动云EOS并返回公开访问URL"""
    try:
        # 1. 文件有效性校验
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"文件不存在: {image_path}")
        if not os.access(image_path, os.R_OK):
            raise PermissionError(f"无权限读取文件: {image_path}")
        file_size = os.path.getsize(image_path)
        if file_size == 0:
            raise ValueError(f"文件为空: {image_path}")
        app.logger.info(f"准备上传文件: {image_path}，大小: {file_size / 1024:.2f}KB")

        # 2. 生成唯一文件名
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        original_name = secure_filename(os.path.basename(image_path))
        filename = f"docs/{timestamp}_{original_name}"
        ext = original_name.rsplit('.', 1)[1].lower() if '.' in original_name else 'png'

        # 3. 执行上传
        s3_client.upload_file(
            Filename=image_path,
            Bucket=MOBILECLOUD_EOS_BUCKET,
            Key=filename,
            ExtraArgs={
                'ACL': 'public-read',
                'ContentType': f'image/{ext}'
            }
        )

        # 4. 构造并验证URL
        endpoint_host = MOBILECLOUD_EOS_ENDPOINT.replace('https://', '')
        image_url = f"{MOBILECLOUD_EOS_ENDPOINT}/{MOBILECLOUD_EOS_BUCKET}/{filename}"

        # 验证URL可用性
        try:
            response = requests.head(image_url, timeout=10)
            response.raise_for_status()  # 触发HTTP错误
            app.logger.info(f"EOS上传成功，URL: {image_url}")
            return image_url
        except requests.exceptions.RequestException as e:
            app.logger.error(f"URL验证失败: {str(e)}，URL: {image_url}")
            raise ConnectionError(f"URL不可访问: {str(e)}")

    except ClientError as e:
        error_code = e.response['Error']['Code'] if hasattr(e, 'response') else 'Unknown'
        error_msg = e.response['Error']['Message'] if hasattr(e, 'response') else str(e)
        app.logger.error(f"EOS上传错误 [代码: {error_code}]: {error_msg}")
        return None
    except Exception as e:
        app.logger.error(f"上传流程失败: {str(e)}")
        return None


# 模型调用函数（增强日志和解析）
def call_model_api(image_path):
    """上传图片到EOS并调用大模型识别信息，返回结果和错误信息"""
    try:
        # 1. 上传图片到EOS
        image_url = upload_to_mobilecloud_eos(image_path)
        if not image_url:
            return None, "图片上传至EOS失败，请检查EOS配置或日志"

        # 2. 清理本地临时文件
        try:
            if os.path.exists(image_path):
                os.remove(image_path)
                app.logger.info(f"已清理本地临时文件: {image_path}")
        except Exception as e:
            app.logger.warning(f"临时文件清理失败: {str(e)}")  # 非致命错误

        # 3. 构造模型请求（强化提示词格式要求）
        payload = {
            "model": QWEN_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url}
                        },
                        {
                            "type": "text",
                            "text": """请严格按照以下要求识别图片中的单据信息：
1. 输出格式：仅返回JSON数组，无任何多余文本（如解释、说明、换行）
2. 数组元素：每个元素是一个单据的字典，必须包含字段：
   - product_name（产品名称，必填，无法识别用"-"）
   - model（型号，必填，无法识别用"-"）
   - specification（规格，必填，无法识别用"-"）
   - manufacturer（生产厂家，必填，无法识别用"-"）
   - production_date（生产日期，必填，格式YYYY-MM-DD，无法识别用"-"）
   - shipment_date（出厂日期，无法识别，格式YYYY-MM-DD，在整个图片范围内查找“发货日期”或“签发日期”。这可能不在合格证标签上，而是在主文档的表格或页脚。）
   - batch_number（批号，必填，无法识别用"-"）
3. 示例：[{"product_name":"XXX","model":"Y123","specification":"10cm","manufacturer":"XX厂","production_date":"2023-01-01","shipment_date":"2023-01-10","batch_number":"BN001"}]"""
                        }
                    ]
                }
            ],
            "stream": False,
            "temperature": 0.1,
            "max_tokens": 2000
        }

        # 4. 调用模型API
        headers = {
            "Authorization": f"Bearer {QWEN_API_KEY}",
            "Content-Type": "application/json"
        }

        app.logger.info(f"调用模型: {QWEN_MODEL}，图片URL: {image_url}")
        try:
            response = requests.post(
                QWEN_API_URL,
                headers=headers,
                json=payload,
                timeout=60  # 延长超时时间
            )
            response.raise_for_status()  # 触发HTTP错误
            app.logger.info(f"模型调用成功，状态码: {response.status_code}")
        except requests.exceptions.HTTPError as e:
            error_detail = e.response.text if e.response else str(e)
            app.logger.error(f"模型API HTTP错误: {error_detail}")
            return None, f"模型接口错误 (状态码: {e.response.status_code if e.response else '未知'})"
        except requests.exceptions.RequestException as e:
            app.logger.error(f"模型请求失败: {str(e)}")
            return None, f"模型连接失败: {str(e)}"

        # 5. 解析模型响应（打印原始数据）
        try:
            response_json = response.json()
            full_response = response_json['choices'][0]['message']['content'].strip()

            # 关键修复：移除代码块标记（```json和```）
            if full_response.startswith('```json'):
                full_response = full_response[7:]  # 移除开头的```json
            if full_response.endswith('```'):
                full_response = full_response[:-3]  # 移除结尾的```
            full_response = full_response.strip()  # 清除可能的空格和换行

            # 打印清理后的响应
            print("\n===== 大模型原始返回数据（清理后） =====")
            print(full_response)
            print("=====================================\n")
            app.logger.info(f"模型清理后响应: {full_response}")
        except (KeyError, json.JSONDecodeError) as e:
            app.logger.error(f"模型响应格式错误: {str(e)}, 原始响应: {response.text}")
            return None, f"模型返回格式异常: {str(e)}"

        # 6. 严格校验JSON格式和内容
        try:
            parsed_results = json.loads(full_response)
            # 校验是否为数组
            if not isinstance(parsed_results, list):
                raise ValueError("模型返回不是数组格式")
            # 校验数组元素有效性
            required_fields = ['product_name', 'model', 'specification', 'manufacturer', 'batch_number']
            valid_results = []
            for item in parsed_results:
                if not isinstance(item, dict):
                    app.logger.warning(f"过滤无效元素（非字典）: {item}")
                    continue
                # 检查必填字段
                missing_fields = [f for f in required_fields if f not in item]
                if missing_fields:
                    app.logger.warning(f"过滤缺失字段的元素: 缺少{missing_fields}，元素: {item}")
                    continue
                # 填充空字段为"-"
                for field in required_fields:
                    if item[field] in (None, ''):
                        item[field] = '-'
                valid_results.append(item)
            app.logger.info(f"模型响应解析成功，有效记录数: {len(valid_results)}/{len(parsed_results)}")
            return valid_results, None if valid_results else "模型返回数据为空或无效"
        except json.JSONDecodeError as e:
            app.logger.error(f"JSON解析失败: {str(e)}, 原始响应: {full_response}")
            # 降级文本解析
            parsed_result = parse_model_output(full_response)
            if parsed_result:
                return [parsed_result], f"模型返回非JSON格式，已尝试文本解析"
            else:
                return None, f"解析失败: 无法识别模型返回格式"
        except ValueError as e:
            app.logger.error(f"模型响应内容错误: {str(e)}, 原始响应: {full_response}")
            return None, f"模型返回内容不符合要求: {str(e)}"

    except Exception as e:
        app.logger.error(f"模型调用流程异常: {str(e)}")
        return None, f"识别过程异常: {str(e)}"


# 文本响应降级解析函数
def parse_model_output(model_response):
    """当模型返回非JSON文本时的降级解析逻辑"""
    try:
        result = {
            'product_name': '-',
            'model': '-',
            'specification': '-',
            'manufacturer': '-',
            'production_date': '',
            'shipment_date': '',
            'batch_number': '-'
        }

        # 扩展关键词映射（支持更多表述）
        keyword_mapping = {
            'product_name': ['产品名称', '商品名称', '品名', '产品型号名称'],
            'model': ['型号', '产品型号', '机型', '规格型号'],
            'specification': ['规格', '产品规格', '技术规格', '尺寸规格'],
            'manufacturer': ['生产厂家', '制造商', '生产企业', '出品方', '生产公司'],
            'production_date': ['生产日期', '制造日期', '生产时间', '出厂日期(生产)'],
            'shipment_date': ['出厂日期', '发货日期', '出库日期', '交货日期'],
            'batch_number': ['批号', '批次号', '生产批号', '批次编码']
        }

        # 按行解析文本
        for line in model_response.split('\n'):
            line = line.strip()
            if not line or ':' not in line:
                continue
            key_part, value = line.split(':', 1)
            key_part = key_part.lower().strip()
            value = value.strip().replace('"', '').replace("'", "")  # 去除引号干扰

            # 匹配关键词并赋值
            for field, keywords in keyword_mapping.items():
                if any(keyword in key_part for keyword in keywords):
                    result[field] = value if value else '-'
                    break  # 匹配到第一个关键词后停止

        app.logger.info(f"文本降级解析结果: {result}")
        return result
    except Exception as e:
        app.logger.error(f"文本响应解析失败: {str(e)}")
        return None


# 路由：识别接口
@app.route('/recognize', methods=['POST'])
def recognize():
    try:
        data = request.json
        if not data or 'filename' not in data:
            return jsonify({'status': 'error', 'message': '缺少参数: filename'}), 400

        filename = data['filename']
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        # 路径安全验证（防止路径遍历）
        if not os.path.abspath(filepath).startswith(os.path.abspath(app.config['UPLOAD_FOLDER'])):
            return jsonify({'status': 'error', 'message': '无效的文件路径'}), 400
        if not os.path.exists(filepath):
            return jsonify({'status': 'error', 'message': '文件不存在'}), 404

        # 调用模型识别
        results, error_msg = call_model_api(filepath)
        if not results:
            return jsonify({'status': 'error', 'message': error_msg or '识别失败，请查看日志'}), 500

        # 格式化响应
        formatted_results = []
        for result in results:
            formatted_results.append({
                'nameResult': result.get('product_name', '-'),
                'modelResult': result.get('model', '-'),
                'specResult': result.get('specification', '-'),
                'manufacturerResult': result.get('manufacturer', '-'),
                'productionDateResult': result.get('production_date', ''),
                'shipmentDateResult': result.get('shipment_date', ''),
                'batchNumberResult': result.get('batch_number', '-'),
                'remarkResult': '解析成功' if all(v != '-' for v in result.values()) else '部分字段未识别'
            })

        return jsonify({
            'status': 'success',
            'result': formatted_results
        })

    except Exception as e:
        app.logger.error(f"识别接口异常: {str(e)}")
        return jsonify({'status': 'error', 'message': '服务器内部错误'}), 500


# 路由：保存识别结果
@app.route('/save', methods=['POST'])
def save():
    try:
        data = request.json
        if not data:
            return jsonify({'status': 'error', 'message': '缺少数据'}), 400
        # 兼容单个结果
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return jsonify({'status': 'error', 'message': '数据格式应为数组'}), 400

        # 保存记录
        saved_count = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                history = RecognizeHistory(
                    name=item.get('nameResult', '-'),
                    model=item.get('modelResult', '-'),
                    spec=item.get('specResult', '-'),
                    manufacturer=item.get('manufacturerResult', '-'),
                    production_date=item.get('productionDateResult', ''),
                    shipment_date=item.get('shipmentDateResult', ''),
                    batch_number=item.get('batchNumberResult', '-'),
                    remark=item.get('remarkResult', '')
                )
                db.session.add(history)
                saved_count += 1
            except Exception as e:
                app.logger.warning(f"单条记录保存失败: {str(e)}")

        db.session.commit()
        return jsonify({
            'status': 'success',
            'message': f'成功保存{saved_count}/{len(data)}条记录'
        })

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"保存接口异常: {str(e)}")
        return jsonify({'status': 'error', 'message': '保存失败'}), 500


# 路由：文件上传
@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'status': 'error', 'message': '未找到文件'}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({'status': 'error', 'message': '未选择文件'}), 400

        if file and allowed_file(file.filename):
            # 生成安全文件名
            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = secure_filename(f"doc_{datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}")
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)

            # 保存文件
            file.save(filepath)
            app.logger.info(f"文件上传成功: {filename}，路径: {filepath}")
            return jsonify({
                'status': 'success',
                'message': '上传成功',
                'filename': filename
            })

        return jsonify({'status': 'error', 'message': '仅支持jpg、jpeg、png格式'}), 400

    except Exception as e:
        app.logger.error(f"文件上传失败: {str(e)}")
        return jsonify({'status': 'error', 'message': '上传失败'}), 500


# 路由：历史记录
@app.route('/history')
def history():
    try:
        records = RecognizeHistory.query.order_by(RecognizeHistory.create_time.desc()).all()
        return render_template('History.html', records=records)
    except Exception as e:
        app.logger.error(f"历史记录异常: {str(e)}")
        return render_template('error.html', message='获取历史失败'), 500


# 路由：首页
@app.route('/')
def index():
    return render_template('Index.html')


# 错误处理
@app.errorhandler(413)
def too_large(error):
    return jsonify({'status': 'error', 'message': '文件超过5MB限制'}), 413


@app.errorhandler(400)
def bad_request(error):
    return jsonify({'status': 'error', 'message': '请求格式错误'}), 400


@app.errorhandler(500)
def server_error(error):
    return jsonify({'status': 'error', 'message': '服务器内部错误'}), 500


# 初始化数据库
def init_db():
    with app.app_context():
        db.create_all()
        app.logger.info("数据库初始化完成")


if __name__ == '__main__':
    # 安装依赖：pip install flask flask-caching flask-sqlalchemy flask-cors boto3 requests werkzeug
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5070)