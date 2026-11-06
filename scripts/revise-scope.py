"""原地修订既有技术方案，保留原始DOCX及修订版的其余结构。"""
import os
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[1]
path = next(root.glob('*Python*修订版.docx'))
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
with zipfile.ZipFile(path) as archive:
    entries = [(item, archive.read(item.filename)) for item in archive.infolist()]
xml = ET.fromstring(dict((item.filename, value) for item, value in entries)['word/document.xml'])
body = xml.find(W + 'body')
marker = '当前基础版范围修订（字幕、音频与私信）'
paragraphs = [
    marker,
    '以用户最新要求为准：本轮基础版实现视频字幕获取、音频下载与可选本地ASR、带来源的总结、基础私信收发、对话长期记忆和角色连续性。暂不实现图像识别；真实播放器、截帧与视觉模型不作为基础版依赖或验收条件。下文保留的视觉和陪看内容属于后续可选扩展。',
    '私信纳入当前版：验证实际登录UID；读取个人会话与文本消息；按账号、对话对象隔离历史和记忆；生成回复草稿；显式启用发送后按指定会话发布；可启用有界的回复轮询。首次监听只处理启动后的消息，不补发历史积压。未知发送结果保留待核对状态，不自动重发。',
    '视频内容链路：优先使用带时间戳字幕；显式请求时下载音频，或在启用本地ASR后作为缺字幕降级。音频只取平台返回的音频轨，限制下载大小、缓存与并发；转写结果标记为机器识别，不能推断画面、非语言声音含义或未经验证的事实。',
    '长期记忆分为公开视频分析经历与私信对话记忆。私信上下文仅包含同一登录账号和对方UID下的记录；摘要保留消息来源ID。支持检索与遗忘，删除后保留不含正文的去重标记，避免下一轮同步恢复已删除内容。已发送消息、宿主日志和外部备份不承诺物理删除。',
    '工程结构统一为：src/bili_agent/ 是 Python 业务核心，worker.py 是进程入口；plugin/src/ 是 DSH 原生接入，host.ts 只组装生命周期，config.ts 定义配置，tools.ts 注册工具。tests/python/ 与 tests/node/ 分别保存回归测试；requirements/ 集中依赖，legacy/comment_bot/ 归档旧评论原型，docs/ 保存使用与开发说明。正式插件不依赖旧原型；构建目录 dist/ 不手工修改。',
    '交付顺序：精简基础安装依赖 → 字幕/音频/转写和总结 → 私信收件与安全发送队列 → 对话总结、隔离记忆与遗忘 → 宿主工具和真实环境联调。已完成代码、测试替身验证和真实平台验证分别记录；第一版完成前不提交Git。',
]
existing = list(body)
for index, paragraph in enumerate(existing):
    if ''.join(paragraph.itertext()) == marker:
        for old in existing[index:]:
            is_last = ''.join(old.itertext()).startswith('交付顺序：')
            body.remove(old)
            if is_last:
                break
        break
for index, text in enumerate(paragraphs):
    paragraph = ET.Element(W + 'p')
    run = ET.SubElement(paragraph, W + 'r')
    ET.SubElement(run, W + 't').text = text
    body.insert(index, paragraph)
for paragraph in body.iter(W + 'p'):
    text = ''.join(paragraph.itertext())
    if '自动投稿和私信运营不纳入本版完整目标' in text:
        updated = text.replace('自动投稿和私信运营不纳入本版完整目标', '自动投稿和批量私信运营不纳入本版完整目标；基础一对一私信收发按最新范围纳入')
        for node in list(paragraph):
            if node.tag != W + 'pPr':
                paragraph.remove(node)
        ET.SubElement(ET.SubElement(paragraph, W + 'r'), W + 't').text = updated
result = ET.tostring(xml, encoding='utf-8', xml_declaration=True)
temporary = path.with_suffix('.tmp.docx')
try:
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for item, value in entries:
            archive.writestr(item, result if item.filename == 'word/document.xml' else value)
    with zipfile.ZipFile(temporary) as archive:
        assert archive.testzip() is None
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
print('Updated existing revised DOCX; original design unchanged.')
