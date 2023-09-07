import requests, functools, re, logging, json, mimetypes, os
from requests.exceptions import (
    ConnectionError, JSONDecodeError, ReadTimeout, InvalidSchema, MissingSchema,
    InvalidURL, TooManyRedirects)
from base64 import b64decode
from raddoo import env, slurp, random_uuid, identity, merge
from flask import Flask, request, abort, render_template
from flask_cors import CORS
from dufflepud.util import now
from dufflepud.db import model
from dufflepud import s3

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
    return _get_relay_info(get_json('url')) or {}


@app.route('/link/preview', methods=['POST'])
def link_preview():
    url = get_json('url')
    res = _req('head', url)

    if (res.headers.get('Content-Type', '') if res else '').startswith('image/'):
        return {'title': "", 'description': "", 'image': url, 'url': url}

    return _get_link_preview(url) or {}


@app.route('/handle/info', methods=['POST'])
def handle_info():
    return _get_handle_info(get_json('handle')) or {}


@app.route('/zapper/info', methods=['POST'])
def zapper_info():
    return _get_zapper_info(get_json('lnurl')) or {}


@app.route('/upload/quote', methods=['POST'])
def upload_quote():
    abort(404)

    uploads = []
    for upload in get_json('uploads'):
        try:
            size = int(upload['size'])
        except (ValueError, KeyError):
            return err('invalid-json', f"`uploads.size` is a required parameter")

        if size > MAX_CONTENT_LENGTH:
            return err('invalid-json', f"File size must be less than {MAX_CONTENT_LENGTH}")

        uploads.append({'id': random_uuid(), 'size': size})

    quote_id = random_uuid()
    invoice = None

    with model.db.transaction():
        model.insert('quote', {'id': quote_id, 'invoice': invoice})

        for upload in uploads:
            model.insert('upload', merge(upload, {'quote': quote_id}))

    return {
        'id': quote_id,
        'invoice': invoice,
        'uploads': uploads,
        'terms': (
            "You certify that your content is free of pornography and illegal content. "
            "Content may be deleted at the discretion of the host."
        ),
    }


@app.route('/upload/<upload_id>', methods=['POST'])
def upload_create(upload_id):
    abort(404)

    with model.db.transaction():
        upload = model.get_by_id('upload', upload_id)

    if not upload:
        return err('not-found', f"Upload `{id}` not found")

    try:
        fh = request.files['file']
    except KeyError:
        return err('invalid-file', "Invalid file `file`")

    if get_size(fh) > upload['size']:
        return err('invalid-file', "File is larger than quoted size")

    mimetype = fh.mimetype
    ext = mimetypes.guess_extension(mimetype)
    key = f'uploads/{upload_id}{ext}'

    s3.put(key, fh, mimetype)

    return {"url": s3.get_url(key)}


@app.route('/moderate', methods=['GET'])
def moderate():
    abort(404)

    return render_template('moderate.html', media=json.dumps(s3.get_moderation_list()))


@app.route('/<path:key>', methods=['DELETE'])
def upload_delete(key):
    abort(404)

    s3.delete(key.split('/', 1)[1])

    return {}



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


def get_size(fh):
    # https://stackoverflow.com/a/23601025
    size = fh.seek(0, os.SEEK_END)
    fh.seek(0, os.SEEK_SET)

    return size


@functools.lru_cache()
def _get_relays():
    return _req_json('get', 'https://nostr.watch/relays.json')


@functools.lru_cache(maxsize=2000)
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


@functools.lru_cache(maxsize=10000)
def _get_handle_info(handle):
    name, domain = re.match(r'^(?:([\w.+-]+)@)?([\w.-]+)$', handle).groups()
    res = _req_json('get', f'https://{domain}/.well-known/nostr.json?name={name}')

    return {'pubkey': res.get('names', {}).get(name) if res else None}


@functools.lru_cache(maxsize=10000)
def _get_zapper_info(lnurl):
    return _req_json('get', lnurl)


def _req(*args, **kwargs):
    try:
        return requests.request(*args, **kwargs)
    except (ConnectionError, ReadTimeout, InvalidSchema, InvalidURL, MissingSchema,
            TooManyRedirects, UnicodeError):
        return None


def _req_json(*args, **kwargs):
    res = _req(*args, **kwargs)

    if not res:
        return None

    try:
        return res.json()
    except JSONDecodeError:
        return None
