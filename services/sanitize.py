"""
services/sanitize.py
Sanitização server-side do HTML de e-mails recebidos (S6). Usa nh3 (bindings
Python da lib Rust "ammonia") — por padrão já remove <script>, <iframe>,
<object>, <form>, atributos on*, e bloqueia esquemas de URL perigosos como
javascript:. Aqui só ampliamos a allowlist para cobrir HTML de e-mail comum
(tabelas, estilos inline, fontes) e forçamos target="_blank" nos links.
"""
import re
import nh3

_TAGS = {
    'a', 'b', 'strong', 'i', 'em', 'u', 's', 'strike', 'p', 'br', 'div', 'span',
    'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'h5',
    'h6', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'img', 'hr',
    'sub', 'sup', 'font', 'center', 'small', 'big', 'caption', 'label',
}

_ATTRS = {
    '*':     {'style', 'class', 'align', 'dir', 'bgcolor', 'valign', 'id'},
    'a':     {'href', 'title', 'name'},
    'img':   {'src', 'alt', 'width', 'height', 'border'},
    'font':  {'color', 'size', 'face'},
    'table': {'border', 'cellpadding', 'cellspacing', 'width', 'bgcolor'},
    'td':    {'colspan', 'rowspan', 'width', 'height', 'bgcolor'},
    'th':    {'colspan', 'rowspan', 'width', 'height', 'bgcolor'},
}

_LINK_TAG_RE = re.compile(r'<a(?=[\s>])', re.IGNORECASE)


def sanitize_email_html(html: str) -> str:
    """Remove tudo que possa executar código (script, on*, javascript:, iframe,
    object, form) e mantém só HTML de formatação/exibição. Força
    target=_blank + rel=noopener noreferrer em links."""
    if not html:
        return html
    clean = nh3.clean(
        html,
        tags=_TAGS,
        attributes=_ATTRS,
        link_rel='noopener noreferrer',
        url_schemes={'http', 'https', 'mailto', 'cid'},
    )
    return _LINK_TAG_RE.sub('<a target="_blank"', clean)
