"""wengao_ppt.research.fetch

网页内容抓取与快照落盘模块：
- 安全下载页面（带超时与大小上限）
- 提取并清洗正文纯文本
- 落盘快照文件至 evidence/research/snapshots/<fact_id>.txt
- 计算 SHA-256 校验和
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
from typing import Optional

from wengao_ppt.research.netsafe_compat import NetworkSecurityError, safe_urlopen

logger = logging.getLogger(__name__)


def clean_html_to_text(html_content: str) -> str:
    """去除 HTML 标签与脚本，保留可读段落文本。"""
    # 移除 script 和 style
    text = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", html_content, flags=re.DOTALL | re.IGNORECASE)
    # 将块级标签替换为换行
    text = re.sub(r"<(p|br|div|h[1-6]|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # 移除其它所有 html 标签
    text = re.sub(r"<[^>]+>", " ", text)
    # 解码 HTML 实体
    text = html.unescape(text)
    # 合并多余空白，保留换行
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned


def fetch_url_text(url: str, timeout_s: int = 8) -> Optional[str]:
    """抓取页面并提取纯文本。异常或无效时返回 None。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        raw_bytes = safe_urlopen(url, headers=headers, timeout_s=timeout_s, allow_loopback=False)
        # 尝试使用 utf-8 解码，失败回退到 gbk
        try:
            content_str = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            content_str = raw_bytes.decode("gbk", "ignore")

        cleaned_text = clean_html_to_text(content_str)
        if len(cleaned_text.strip()) < 40:
            logger.info("抓取页面正文过短 (%s): 长度 %d", url, len(cleaned_text))
            return None
        return cleaned_text
    except Exception as e:
        logger.warning("抓取页面失败 (%s): %s", url, e)
        return None


def save_snapshot(
    job_dir: str,
    fact_id: str,
    text_content: str,
) -> tuple[str, str]:
    """保存快照至 <job_dir>/evidence/research/snapshots/<fact_id>.txt，返回 (相对路径, sha256)。"""
    rel_path = f"evidence/research/snapshots/{fact_id}.txt"
    abs_path = os.path.join(job_dir, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(text_content)

    content_bytes = text_content.encode("utf-8")
    sha256 = hashlib.sha256(content_bytes).hexdigest()
    return rel_path, sha256
