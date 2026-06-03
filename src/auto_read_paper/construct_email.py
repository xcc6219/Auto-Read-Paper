from .protocol import Paper
import html as _html
import math
import re


framework = """
<!DOCTYPE HTML>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body { background: #f3f4f6; margin: 0; padding: 24px 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Arial, sans-serif; }
    .container { max-width: 760px; margin: 0 auto; padding: 0 12px; }
    .digest-header { text-align: center; padding: 20px 16px; background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); border-radius: 12px; color: #fff; margin-bottom: 20px; }
    .digest-header h1 { margin: 0; font-size: 22px; letter-spacing: 0.5px; }
    .digest-header .sub { margin-top: 6px; font-size: 13px; opacity: 0.9; }
    .paper-card { background: #ffffff; border: 1px solid #e5e7eb; border-left: 5px solid #4f46e5; border-radius: 10px; padding: 18px 20px; margin-bottom: 18px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
    .paper-title-en { font-size: 17px; font-weight: 700; color: #111827; line-height: 1.4; margin: 0 0 4px 0; }
    .paper-title-zh { font-size: 14px; font-weight: 500; color: #4b5563; line-height: 1.4; margin: 0 0 10px 0; }
    .paper-meta { font-size: 13px; color: #6b7280; line-height: 1.55; margin-bottom: 10px; }
    .paper-meta .aff { color: #9ca3af; font-style: italic; }
    .score-badge { display: inline-block; background: #eef2ff; color: #4338ca; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 999px; margin-bottom: 12px; }
    .section { margin: 8px 0; font-size: 14px; line-height: 1.65; color: #1f2937; }
    .section-label { display: inline-block; font-weight: 700; color: #fff; padding: 2px 8px; border-radius: 4px; margin-right: 6px; font-size: 12px; letter-spacing: 0.3px; }
    .label-core { background: #2563eb; }
    .label-novel { background: #059669; }
    .label-value { background: #d97706; }
    .pdf-btn { display: inline-block; text-decoration: none; font-size: 13px; font-weight: 600; color: #fff; background: #dc2626; padding: 7px 16px; border-radius: 6px; margin-top: 10px; }
    .pdf-btn:hover { background: #b91c1c; }
    .footer { text-align: center; font-size: 12px; color: #9ca3af; margin-top: 20px; padding: 16px; }
  </style>
</head>
<body>
<div class="container">
    __CONTENT__
    <div class="footer">
        To unsubscribe, remove your email in your Github Action setting.
    </div>
</div>
</body>
</html>
"""


def get_empty_html():
    return """
    <div class="paper-card" style="text-align:center;">
        <div class="paper-title-en">No Papers Today. Take a Rest! 🌿</div>
        <div class="paper-title-zh">今日暂无新论文，休息一下吧</div>
    </div>
    """


_ANCHOR_CSS = {
    "[CORE]": "label-core",
    "[INNOVATION]": "label-novel",
    "[VALUE]": "label-value",
}

# Localized pill labels per language. "english" covers English; "chinese" is the
# default fallback (original behaviour). Unknown languages fall back to English.
_PILL_LABELS = {
    "chinese": {"[CORE]": "核心工作", "[INNOVATION]": "主要创新", "[VALUE]": "潜在价值"},
    "english": {"[CORE]": "Core Work", "[INNOVATION]": "Key Innovation", "[VALUE]": "Potential Value"},
    "japanese": {"[CORE]": "コア研究", "[INNOVATION]": "主な革新", "[VALUE]": "潜在価値"},
    "korean": {"[CORE]": "핵심 작업", "[INNOVATION]": "주요 혁신", "[VALUE]": "잠재 가치"},
    "french": {"[CORE]": "Travail clé", "[INNOVATION]": "Innovation clé", "[VALUE]": "Valeur potentielle"},
    "german": {"[CORE]": "Kernarbeit", "[INNOVATION]": "Kerninnovation", "[VALUE]": "Potenzieller Wert"},
    "spanish": {"[CORE]": "Trabajo clave", "[INNOVATION]": "Innovación clave", "[VALUE]": "Valor potencial"},
}

# Legacy Chinese labels — older cached tldrs in score_history.json may still use these.
_LEGACY_ALIASES = {
    "【核心工作】": "[CORE]",
    "【主要创新】": "[INNOVATION]",
    "【潜在价值】": "[VALUE]",
}


def _pill_labels_for(lang: str) -> dict:
    return _PILL_LABELS.get((lang or "chinese").strip().lower(), _PILL_LABELS["english"])


def _format_tldr(tldr: str, lang: str = "Chinese") -> str:
    """Parse [CORE]/[INNOVATION]/[VALUE] anchors and render localized pills."""
    if not tldr:
        return ""
    text = tldr.replace("<br>", "\n").strip()

    # Backward compat: convert any legacy Chinese labels to anchor tokens.
    for legacy, anchor in _LEGACY_ALIASES.items():
        text = text.replace(legacy, anchor)

    parts = re.split(r"(\[CORE\]|\[INNOVATION\]|\[VALUE\])", text)
    if len(parts) <= 1:
        return f'<div class="section">{_html.escape(text).replace(chr(10), "<br>")}</div>'

    pill = _pill_labels_for(lang)
    out = []
    current = None
    for chunk in parts:
        if chunk in _ANCHOR_CSS:
            current = chunk
            continue
        content = chunk.strip(" \n:：")
        if not content:
            continue
        if current and current in _ANCHOR_CSS:
            css = _ANCHOR_CSS[current]
            label = pill.get(current, current.strip("[]").title())
            safe = _html.escape(content).replace("\n", "<br>")
            out.append(
                f'<div class="section"><span class="section-label {css}">{_html.escape(label)}</span>{safe}</div>'
            )
            current = None
    if not out:
        return f'<div class="section">{_html.escape(text).replace(chr(10), "<br>")}</div>'
    return "".join(out)


def get_block_html(title_en: str, title_zh: str, authors: str, rate, tldr: str, pdf_url: str, affiliations: str = None, lang: str = "Chinese"):
    title_zh_html = (
        f'<div class="paper-title-zh">{_html.escape(title_zh)}</div>'
        if title_zh else ""
    )
    aff_html = f'<span class="aff">{_html.escape(affiliations or "")}</span>' if affiliations else ""
    rate_html = f'<div class="score-badge">⭐ Relevance {rate}</div>' if rate != "Unknown" else ""
    return f"""
    <div class="paper-card">
        <div class="paper-title-en">{_html.escape(title_en)}</div>
        {title_zh_html}
        <div class="paper-meta">{_html.escape(authors)}<br>{aff_html}</div>
        {rate_html}
        {_format_tldr(tldr, lang)}
        <a href="{pdf_url}" class="pdf-btn">📄 PDF</a>
    </div>
    """


def get_stars(score: float):
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low = 6
    high = 8
    if score <= low:
        return ''
    elif score >= high:
        return full_star * 5
    interval = (high - low) / 10
    star_num = math.ceil((score - low) / interval)
    full_star_num = int(star_num / 2)
    half_star_num = star_num - full_star_num * 2
    return '<div class="star-wrapper">' + full_star * full_star_num + half_star * half_star_num + '</div>'


_HEADER_TEXT = {
    "chinese": ("📚 今日论文速递 · Auto-Read-Paper", "多智能体阅读 · 分级评分 · AI 解读"),
    "english": ("📚 Today's arXiv Digest · Auto-Read-Paper", "Multi-agent reading · ranked scoring · AI commentary"),
}

_EMPTY_TEXT = {
    "chinese": ("No Papers Today. Take a Rest! 🌿", "今日暂无新论文，休息一下吧"),
    "english": ("No Papers Today. Take a Rest! 🌿", ""),
}


def _header_for(lang: str) -> str:
    key = (lang or "chinese").strip().lower()
    title, sub = _HEADER_TEXT.get(key, _HEADER_TEXT["english"])
    return (
        f'<div class="digest-header">'
        f'<h1>{title}</h1>'
        f'<div class="sub">{sub}</div>'
        f'</div>'
    )


def _empty_for(lang: str) -> str:
    key = (lang or "chinese").strip().lower()
    en, sub = _EMPTY_TEXT.get(key, _EMPTY_TEXT["english"])
    sub_html = f'<div class="paper-title-zh">{sub}</div>' if sub else ""
    return f"""
    <div class="paper-card" style="text-align:center;">
        <div class="paper-title-en">{en}</div>
        {sub_html}
    </div>
    """


# Notice email shown when the run produced no papers because the LLM provider
# ran out of balance / quota. Bilingual; falls back to English for unknown langs.
_BILLING_ERROR_TEXT = {
    "chinese": {
        "h1": "⚠️ 今日论文未能推送",
        "sub": "Auto-Read-Paper 运行通知",
        "title": "LLM 余额不足 / 配额用尽",
        "body": (
            "今天的论文摘要没有生成成功：配置的 LLM 服务商在调用时返回了"
            "「余额不足 / insufficient balance」错误，导致论文评分、深度解读"
            "和标题翻译全部失败，因此今天没有可推送的论文。"
        ),
        "action_label": "如何恢复",
        "actions": [
            "前往你的 LLM 服务商后台充值（如 MiniMax / DeepSeek / OpenAI 等）；",
            "或在 GitHub 仓库 Settings → Secrets and variables → Actions 中，把 "
            "LLM_API_KEY / LLM_API_BASE / LLM_MODEL 换成其它仍有余额的服务；",
            "完成后重新触发一次工作流，即可补发今天的论文。",
        ],
        "detail_label": "服务商返回的原始错误",
    },
    "english": {
        "h1": "⚠️ Today's digest could not be sent",
        "sub": "Auto-Read-Paper notification",
        "title": "LLM account out of balance / quota",
        "body": (
            "Today's paper summaries were not generated: the configured LLM "
            "provider returned an \"insufficient balance\" error on every call, "
            "so scoring, deep-read and title translation all failed and no "
            "papers could be shipped today."
        ),
        "action_label": "How to recover",
        "actions": [
            "Top up your LLM provider account (e.g. MiniMax / DeepSeek / OpenAI);",
            "or swap LLM_API_KEY / LLM_API_BASE / LLM_MODEL under the GitHub repo "
            "Settings → Secrets and variables → Actions for a provider that still "
            "has balance;",
            "then re-trigger the workflow to resend today's digest.",
        ],
        "detail_label": "Raw provider error",
    },
}


def render_billing_error_email(detail: str = "", lang: str = "Chinese") -> str:
    """Render a notice email explaining that the LLM account is out of balance.

    Sent in place of the daily digest when a fatal insufficient-balance/quota
    error left the run with no papers to ship — so the user finds out the
    account dried up instead of silently receiving nothing.
    """
    key = (lang or "chinese").strip().lower()
    t = _BILLING_ERROR_TEXT.get(key, _BILLING_ERROR_TEXT["english"])
    header = (
        '<div class="digest-header" '
        'style="background: linear-gradient(135deg, #dc2626 0%, #d97706 100%);">'
        f'<h1>{t["h1"]}</h1>'
        f'<div class="sub">{t["sub"]}</div>'
        '</div>'
    )
    actions_html = "".join(f"<li>{_html.escape(a)}</li>" for a in t["actions"])
    detail_html = ""
    if detail and detail.strip():
        safe = _html.escape(detail.strip())
        detail_html = (
            '<div class="paper-meta" style="margin-top:14px;">'
            f'<b>{_html.escape(t["detail_label"])}</b><br>'
            '<code style="display:block; background:#f3f4f6; padding:10px; '
            'border-radius:6px; color:#b91c1c; word-break:break-all; '
            f'margin-top:6px;">{safe}</code></div>'
        )
    card = (
        '<div class="paper-card" style="border-left-color:#dc2626;">'
        f'<div class="paper-title-en">{_html.escape(t["title"])}</div>'
        f'<div class="section" style="margin-top:10px;">{_html.escape(t["body"])}</div>'
        f'<div class="section" style="margin-top:10px;"><b>{_html.escape(t["action_label"])}</b>'
        f'<ol style="margin:8px 0 0 18px; padding:0; line-height:1.7;">{actions_html}</ol></div>'
        f'{detail_html}'
        '</div>'
    )
    return framework.replace("__CONTENT__", header + card)


def render_email(papers: list[Paper], lang: str = "Chinese") -> str:
    header = _header_for(lang)

    if len(papers) == 0:
        return framework.replace("__CONTENT__", header + _empty_for(lang))

    parts = [header]
    for p in papers:
        rate = round(p.score, 1) if p.score is not None else "Unknown"
        author_list = [a for a in p.authors]
        num_authors = len(author_list)
        if num_authors <= 5:
            authors = ", ".join(author_list)
        else:
            authors = ", ".join(author_list[:3] + ["..."] + author_list[-2:])
        if p.affiliations is not None and len(p.affiliations) > 0:
            affs = p.affiliations[:5]
            affiliations = ", ".join(affs)
            if len(p.affiliations) > 5:
                affiliations += ", ..."
        else:
            affiliations = ""
        parts.append(
            get_block_html(
                p.title,
                getattr(p, "title_zh", None) or "",
                authors,
                rate,
                p.tldr or "",
                p.pdf_url,
                affiliations,
                lang,
            )
        )

    return framework.replace("__CONTENT__", "\n".join(parts))
