import requests, functools, re, logging, json, mimetypes, os
from urllib3.exceptions import LocationParseError
from requests.exceptions import (
    ConnectionError, JSONDecodeError, ReadTimeout, InvalidSchema, MissingSchema,
    InvalidURL, TooManyRedirects)
from base64 import b64decode
from raddoo import env, slurp, random_uuid, identity, merge
from flask import Flask, request
from flask_cors import CORS
from dufflepud.util import now
from dufflepud.db import model

MAX_CONTENT_LENGTH = env('MAX_CONTENT_LENGTH')

app = Flask(__name__)

app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

logger = logging.getLogger(__name__)

cors = CORS(app, resource={
    r"/*": {
        "origins": "*"
    }
})


@app.route('/usage/<ident>/<session>/<name>', methods=['POST'])
def usage_post(ident, session, name):
    name = b64decode(name).decode('utf-8')

    with model.db.transaction():
        model.insert('usage', {
            'name': name,
            'ident': ident,
            'session': session,
            'created_at': now(),
        })

    return {}


@app.route('/relay', methods=['GET'])
def relay_list():
    try:
        result = _get_relays()
    except Exception as exc:
        logger.exception(exc)

    if not result:
        result = json.loads(slurp('relays.json'))

    return result

@app.route('/relay/info', methods=['POST'])
def relay_info():
    urls = get_json('urls') if request.json.get('urls') else [get_json('url')]

    return {'data': [{'url': url, 'info': _get_relay_info(url)} for url in urls]}


@app.route('/handle/info', methods=['POST'])
def handle_info():
    handles = get_json('handles') if request.json.get('handles') else [get_json('handle')]

    return {'data': [{'handle': handle, 'info': _get_handle_info(handle)} for handle in handles]}


@app.route('/zapper/info', methods=['POST'])
def zapper_info():
    lnurls = get_json('lnurls') if request.json.get('lnurls') else [get_json('lnurl')]

    return {'data': [{'lnurl': lnurl, 'info': _get_zapper_info(lnurl)} for lnurl in lnurls]}


@app.route('/link/preview', methods=['POST'])
def link_preview():
    url = get_json('url')
    res = req('head', url)

    if (res.headers.get('Content-Type', '') if res else '').startswith('image/'):
        return {'title': "", 'description': "", 'image': url, 'url': url}

    return _get_link_preview(url) or {}


# Utils


def err(code, message):
    if code == 'not-found':
        status = 404
    elif code in {'invalid-json', 'invalid-file'}:
        status = 400
    else:
        raise ValueError(code)

    return {'code': code, 'message': message}, status


def coerce_str(s, max_length=1024):
    if len(s) > max_length:
        raise ValueError("Name is too long")

    return s


def get_json(name, coerce=identity):
    try:
        return coerce(request.json[name])
    except (ValueError, KeyError):
        return err('invalid-json', f"`{name}` is a required parameter")


def req(*args, **kwargs):
    try:
        return requests.request(*args, **kwargs)
    except (ConnectionError, ReadTimeout, InvalidSchema, InvalidURL, MissingSchema,
            TooManyRedirects, UnicodeError, LocationParseError):
        return None


def req_json(*args, **kwargs):
    res = req(*args, **kwargs)

    if not res:
        return None

    try:
        return res.json()
    except JSONDecodeError:
        return None


@functools.lru_cache()
def _get_relays():
    return req_json('get', 'https://nostr.watch/relays.json')


@functools.lru_cache(maxsize=2000)
def _get_relay_info(ws_url):
    http_url = re.sub(r'ws(s?)://', r'http\1://', ws_url)
    headers = {'Accept': 'application/nostr+json'}

    return req_json('post', http_url, headers=headers, timeout=1)


@functools.lru_cache(maxsize=10000)
def _get_handle_info(handle):
    m = re.match(r'^(?:([\w.+-]+)@)?([\w.-]+)$', handle)

    if not m:
        return {'pubkey': None}

    name, domain = m.groups()
    res = req_json('get', f'https://{domain}/.well-known/nostr.json?name={name}') or {}

    return {
        'relays': res.get('relays', {}).get(name),
        'pubkey': res.get('names', {}).get(name),
        'nip46': res.get('nip46', {}).get(name),
    }


@functools.lru_cache(maxsize=10000)
def _get_zapper_info(lnurl):
    return req_json('get', lnurl)


@functools.lru_cache(maxsize=5000)
def _get_link_preview(url):
    return req_json('post', 'https://api.linkpreview.net', params={
        'key': env('LINKPREVIEW_API_KEY'),
        'q': url,
    })
