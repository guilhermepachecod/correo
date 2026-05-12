"""
services/email_parser.py
Funções puras de parsing de e-mails IMAP raw → objetos Email.
Sem estado, sem dependência de Flask — testável isoladamente.
"""
import email as email_lib
from email import policy as email_policy
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime
import logging

from database import Email, Attachment

log = logging.getLogger(__name__)


# ── Helpers de decodificação ───────────────────────────────────────────────────

def decode_header_value(value: str) -> str:
    if not value:
        return ''
    try:
        parts = decode_header(value)
        result = []
        for part, charset in parts:
            if isinstance(part, bytes):
                cs = charset or 'utf-8'
                try:
                    result.append(part.decode(cs, errors='replace'))
                except (LookupError, UnicodeDecodeError):
                    result.append(part.decode('latin-1', errors='replace'))
            else:
                result.append(str(part))
        return ''.join(result)
    except Exception:
        return str(value)


def decode_payload(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if not payload:
            return ''
        charset = part.get_content_charset() or 'utf-8'
        try:
            return payload.decode(charset, errors='replace')
        except (LookupError, UnicodeDecodeError):
            return payload.decode('latin-1', errors='replace')
    except Exception:
        return ''


def get_body(msg) -> tuple[str, str]:
    text, html = '', ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get('Content-Disposition', ''))
            if 'attachment' in cd:
                continue
            if ct == 'text/plain' and not text:
                text = decode_payload(part)
            elif ct == 'text/html' and not html:
                html = decode_payload(part)
    else:
        body = decode_payload(msg)
        if msg.get_content_type() == 'text/html':
            html = body
        else:
            text = body
    return text, html


def get_attachments(msg) -> list[dict]:
    attachments = []
    for part in msg.walk():
        cd = str(part.get('Content-Disposition', ''))
        if 'attachment' not in cd and 'inline' not in cd:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_header_value(filename)
        data = part.get_payload(decode=True)
        if not data:
            continue
        attachments.append({
            'filename':     filename,
            'content_type': part.get_content_type(),
            'size':         len(data),
            'data':         data,
        })
    return attachments


# ── Parser principal ───────────────────────────────────────────────────────────

def parse_email_from_raw(raw: bytes, uid: int, folder: str,
                          account_id: int, is_read: bool,
                          resolve_thread_id_fn) -> tuple:
    """
    Parseia um e-mail raw e retorna (Email obj, att_data list).
    resolve_thread_id_fn: callable(message_id, in_reply_to) → str
    """
    msg = email_lib.message_from_bytes(raw, policy=email_policy.compat32)

    subject     = decode_header_value(msg.get('Subject', ''))
    from_full   = decode_header_value(msg.get('From', ''))
    from_name, from_addr = parseaddr(from_full)
    to_addr     = decode_header_value(msg.get('To', ''))
    cc_addr     = decode_header_value(msg.get('Cc', ''))
    reply_to    = decode_header_value(msg.get('Reply-To', ''))
    message_id  = (msg.get('Message-ID') or '').strip()
    in_reply_to = (msg.get('In-Reply-To') or '').strip()

    try:
        date = parsedate_to_datetime(msg.get('Date', ''))
        if date.tzinfo:
            date = date.replace(tzinfo=None)
    except Exception:
        date = datetime.utcnow()

    body_text, body_html = get_body(msg)
    att_data  = get_attachments(msg)
    thread_id = resolve_thread_id_fn(message_id, in_reply_to)

    email_obj = Email(
        account_id=account_id, folder=folder, uid=uid,
        message_id=message_id, in_reply_to=in_reply_to,
        thread_id=thread_id,
        subject=subject or '(sem assunto)',
        from_addr=from_addr, from_name=from_name,
        to_addr=to_addr, cc_addr=cc_addr, reply_to=reply_to,
        date=date, body_text=body_text, body_html=body_html,
        is_read=is_read, has_attachments=bool(att_data),
    )
    return email_obj, att_data
