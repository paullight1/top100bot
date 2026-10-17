"""Optional private HTTP bridge. All operations require server credentials and an admin actor."""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from neonize.utils.jid import build_jid
from unipod_bot.announcements import AnnouncementConflict, AnnouncementStore
from unipod_bot.knowledge import MAX_PDF_BYTES
from unipod_bot.pdf_handler import extract_pdf_text

logger = logging.getLogger('unipod_bot')
MAX_BODY = 15 * 1024 * 1024
_ID = r'[a-f0-9]{12}'


def fields(data, allowed):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ValueError('Invalid request fields.')


def optional_text(data, key):
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f'{key} must be text.')
    return value or None


def public_knowledge(value):
    """Hide WhatsApp identity metadata; retain the administrator's literal content."""
    result = dict(value)
    for key in ('created_by', 'published_by', 'retired_by', 'source_chat', 'source_message'):
        item = result.get(key)
        if isinstance(item, str) and not item.startswith('web:') and key != 'source_chat':
            result[key] = 'WhatsApp admin' if key.endswith('_by') else 'WhatsApp'
        elif key == 'source_chat' and item and item != 'web-admin':
            result[key] = 'WhatsApp'
    if 'audit' in result:
        result['audit'] = [{**event, 'actor': event.get('actor') if str(event.get('actor', '')).startswith('web:') else 'WhatsApp admin'} for event in result['audit']]
    return result


class AdminAPI:
    def __init__(self, runtime, token):
        self.runtime = runtime
        self.token = token
        self.announcements = AnnouncementStore(runtime.settings.whatsapp_session_path.parent / 'announcements.sqlite3')

    def target(self):
        targets = self.runtime.settings.test_group_jids
        target = self.runtime.settings.test_group_jid
        if (len(targets) != 1 or targets[0] != target or not target
                or not re.fullmatch(r'[\w-]+@g\.us', target)):
            raise ValueError('Announcements require exactly one configured test group.')
        return target

    def target_configured(self):
        try:
            self.target()
            return True
        except ValueError:
            return False

    async def handle(self, method, path, data, headers):
        if not self.token or len(self.token) < 32 or not self.token.isascii():
            return 503, {'message': 'Admin bridge is disabled.'}
        headers = {k.lower(): v for k, v in headers.items()}
        supplied = headers.get('authorization', '')
        actor = headers.get('x-afl-actor', '')
        if (not supplied.isascii() or not secrets.compare_digest(supplied, 'Bearer ' + self.token)
                or not re.fullmatch(r'web:[A-Za-z0-9_-]{1,120}', actor)):
            return 401, {'message': 'Authenticated admin bridge required.'}
        try:
            return await self.dispatch(method, path, data, actor)
        except AnnouncementConflict as exc:
            return 409, {'message': str(exc)}
        except KeyError:
            return 404, {'message': 'Record not found.'}
        except (ValueError, TypeError) as exc:
            return 400, {'message': str(exc) if isinstance(exc, ValueError) else 'Invalid request values.'}
        except Exception as exc:
            logger.error('event=admin_api_failed error=%s', type(exc).__name__)
            return 500, {'message': 'Bot operation failed. No automatic send retry was attempted.'}

    async def dispatch(self, method, path, data, actor):
        url = urlsplit(path)
        path = url.path
        lib = self.runtime.knowledge
        page = int(parse_qs(url.query).get('page', ['1'])[0])
        if not 1 <= page <= 10000:
            raise ValueError('Invalid page.')
        if method == 'GET' and path == '/v1/admin/status':
            return 200, {'connected': bool(getattr(self.runtime.client, 'is_connected', False)),
                         'knowledge_enabled': lib is not None, 'groq_enabled': self.runtime.groq is not None,
                         'memory_enabled': self.runtime.store is not None,
                         'test_group_configured': self.target_configured(),
                         'reply_policy': 'conversational', 'worker_queue': self.runtime.queue.qsize()}
        if path.startswith('/v1/admin/knowledge'):
            if lib is None:
                return 503, {'message': 'Enable the approved knowledge library first.'}
            if path == '/v1/admin/knowledge':
                if method == 'GET':
                    entries = await asyncio.to_thread(lib.list_entries, limit=25, offset=(page - 1) * 25)
                    return 200, {'items': [public_knowledge({k: v for k, v in asdict(e).items() if k != 'content'}) for e in entries], 'page': page}
                if method == 'POST':
                    fields(data, {'title', 'content', 'audience', 'effective_at', 'expires_at', 'replaces',
                                  'source_url', 'pdf_base64', 'filename', 'request_id'})
                    request_id = optional_text(data, 'request_id')
                    if not request_id or len(request_id) > 100:
                        raise ValueError('A unique request ID is required.')
                    content = optional_text(data, 'content') or ''
                    attachment, filename = None, None
                    if data.get('pdf_base64'):
                        encoded = optional_text(data, 'pdf_base64')
                        attachment = base64.b64decode(encoded, validate=True)
                        if len(attachment) > MAX_PDF_BYTES or not attachment.startswith(b'%PDF-'):
                            raise ValueError('Provide a readable PDF of 10 MB or less.')
                        filename = Path(optional_text(data, 'filename') or 'guide.pdf').name
                        extraction = await asyncio.to_thread(extract_pdf_text, attachment, filename)
                        if extraction is None or not extraction.text.strip():
                            raise ValueError('The PDF has no readable text.')
                        content = (content + '\n\n' + extraction.text).strip()
                    options = {key: optional_text(data, key) for key in ('effective_at', 'expires_at', 'replaces', 'source_url')}
                    entry = await asyncio.to_thread(lib.create_draft, title=optional_text(data, 'title') or '',
                        content=content, audience=optional_text(data, 'audience') or 'admin_only', actor=actor,
                        source_chat='web-admin', source_message=actor + ':' + request_id,
                        attachment=attachment, filename=filename, **options)
                    return 201, public_knowledge(asdict(entry))
            match = re.fullmatch(rf'/v1/admin/knowledge/({_ID})(?:/(publish|retire|audience))?', path)
            if match:
                entry_id, action = match.groups()
                if method == 'GET' and not action:
                    entry = await asyncio.to_thread(lib.get, entry_id)
                    audit = await asyncio.to_thread(lib.audit, entry_id)
                    return 200, public_knowledge({**asdict(entry), 'audit': audit,
                                 'active': await asyncio.to_thread(lib.active, entry_id, allow_admin=True)})
                if method == 'POST' and action:
                    fields(data, {'audience'} if action == 'audience' else set())
                    if action == 'publish':
                        await asyncio.to_thread(lib.publish, entry_id, actor=actor)
                    elif action == 'retire':
                        await asyncio.to_thread(lib.retire, entry_id, actor=actor)
                    else:
                        await asyncio.to_thread(lib.set_audience, entry_id, optional_text(data, 'audience'), actor=actor)
                    return 200, public_knowledge(asdict(await asyncio.to_thread(lib.get, entry_id)))
        if path == '/v1/admin/announcements':
            if method == 'GET':
                return 200, {'items': await asyncio.to_thread(self.announcements.list_entries, page), 'page': page}
            if method == 'POST':
                fields(data, {'title', 'content', 'request_id'})
                rid = optional_text(data, 'request_id')
                if not rid or len(rid) > 100:
                    raise ValueError('A unique request ID is required.')
                entry = await asyncio.to_thread(self.announcements.create, title=data.get('title'),
                    content=data.get('content'), target=self.target(), actor=actor, request_id=rid)
                return 201, entry
        match = re.fullmatch(rf'/v1/admin/announcements/({_ID})(?:/(preview|send))?', path)
        if match:
            entry_id, action = match.groups()
            if method == 'GET' and not action:
                return 200, await asyncio.to_thread(self.announcements.get, entry_id)
            if method == 'POST' and action == 'preview':
                fields(data, set())
                return 200, await asyncio.to_thread(self.announcements.preview, entry_id, actor=actor, target=self.target())
            if method == 'POST' and action == 'send':
                fields(data, {'preview_token'})
                target = self.target()
                entry = await asyncio.to_thread(self.announcements.claim, entry_id, actor=actor,
                    target=target, token=optional_text(data, 'preview_token'))
                try:
                    await self.runtime.client.send_message(build_jid(target.split('@')[0], 'g.us'), entry['content'])
                except Exception as exc:
                    await asyncio.to_thread(self.announcements.finish, entry_id, actor=actor, sent=False)
                    logger.warning('event=announcement_delivery_unconfirmed error=%s', type(exc).__name__)
                    return 502, {'message': 'Delivery could not be confirmed. Check the group before creating any replacement; do not retry this send.'}
                return 200, await asyncio.to_thread(self.announcements.finish, entry_id, actor=actor, sent=True)
        return 404, {'message': 'Unknown bot admin operation.'}


def make_server(api: AdminAPI, loop, bind: str, port: int):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log bodies, actor identifiers or bearer credentials.

        def do_GET(self):
            self.process_request('GET')

        def do_POST(self):
            self.process_request('POST')

        def process_request(self, method):
            self.connection.settimeout(10)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > MAX_BODY:
                    return self.respond(413, {'message': 'Request is too large.'})
                # Authenticate before reading/parsing potentially expensive PDF input.
                headers = dict(self.headers)
                supplied = self.headers.get('Authorization', '')
                if (not api.token or not supplied.isascii() or not secrets.compare_digest(supplied, 'Bearer ' + api.token)
                        or not re.fullmatch(r'web:[A-Za-z0-9_-]{1,120}', self.headers.get('X-AFL-Actor', ''))):
                    return self.respond(401, {'message': 'Authenticated admin bridge required.'})
                if method == 'POST' and not self.headers.get('Content-Type', '').startswith('application/json'):
                    return self.respond(415, {'message': 'JSON request required.'})
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw)
                future = asyncio.run_coroutine_threadsafe(api.handle(method, self.path, data, headers), loop)
                status, body = future.result(timeout=45)
                self.respond(status, body)
            except FutureTimeout:
                self.respond(504, {'message': 'Operation is still being resolved. Refresh its status before retrying.'})
            except (ValueError, OSError):
                self.respond(400, {'message': 'Invalid request.'})
            except Exception:
                self.respond(500, {'message': 'Bot operation failed.'})

        def respond(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)

    return ThreadingHTTPServer((bind, port), Handler)
