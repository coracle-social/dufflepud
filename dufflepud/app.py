import requests, functools, re, logging, json
from requests.exceptions import (
    ConnectionError, JSONDecodeError, ReadTimeout, InvalidSchema, MissingSchema,
    InvalidURL, TooManyRedirects)
from base64 import b64decode
from raddoo import env, slurp
from flask import Flask, request, g
from flask_cors import CORS
from dufflepud.util import now
from dufflepud.db import model


app = Flask(__name__)

logger = logging.getLogger(__name__)

cors = CORS(app, resource={
    r"/*":{
        "origins":"*"
    }
})


@app.route('/usage/<session>/<name>', methods=['POST'])
def usage_post(session, name):
    name = b64decode(name).decode('utf-8')

    with model.db.transaction():
        model.insert('usage', {
            'name': name,
            'session': session,
            'created_at': now(),
        })

    return {}


@app.route('/relay', methods=['GET'])
def relay_list():
    try:
        return _get_relays()
    except Exception as exc:
        logger.exception(exc)

        return json.loads(slurp('relays.json'))


@app.route('/relay/info', methods=['POST'])
def relay_info():
    if not request.json.get('url'):
        return {'code': 'invalid-url'}

    return _get_relay_info(request.json['url']) or {}


@app.route('/link/preview', methods=['POST'])
def link_preview():
    if not request.json.get('url'):
        return {'code': 'invalid-url'}

    url = request.json['url']
    res = _req('head', url)

    if (res.headers.get('Content-Type', '') if res else '').startswith('image/'):
        return {'title': "", 'description': "", 'image': url, 'url': url}

    return _get_link_preview(url) or {}


# Utils


@functools.lru_cache()
def _get_relays():
    return requests.get('https://nostr.watch/relays.json').json()


@functools.lru_cache(maxsize=1000)
def _get_relay_info(ws_url):
    http_url = re.sub(r'ws(s?)://', r'http\1://', ws_url)
    headers = {'Accept': 'application/nostr+json'}

    return _req_json('post', http_url, headers=headers, timeout=1)


@functools.lru_cache(maxsize=1000)
def _get_link_preview(url):
    return _req_json('post', 'https://api.linkpreview.net', params={
        'key': env('LINKPREVIEW_API_KEY'),
        'q': url,
    })


def _req(*args, **kwargs):
    try:
        return requests.request(*args, **kwargs)
    except (ConnectionError, ReadTimeout, InvalidSchema, InvalidURL, MissingSchema,
            TooManyRedirects) as exc:
        return None


def _req_json(*args, **kwargs):
    res = _req(*args, **kwargs)

    if not res:
        return None

    try:
        return res.json()
    except JSONDecodeError as exc:
        return None
