import requests, functools, re, logging, json, mimetypes, os
from requests.exceptions import (
    ConnectionError, JSONDecodeError, ReadTimeout, InvalidSchema, MissingSchema,
    InvalidURL, TooManyRedirects)
from base64 import b64decode
from raddoo import env, slurp, random_uuid, identity, merge
from flask import Flask, request
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
    return _get_relay_info(get_json('url')) or {}


@app.route('/link/preview', methods=['POST'])
def link_preview():
    url = get_json('url')
    res = _req('head', url)

    if (res.headers.get('Content-Type', '') if res else '').startswith('image/'):
        return {'title': "", 'description': "", 'image': url, 'url': url}

    return _get_link_preview(url) or {}


@app.route('/upload/quote', methods=['POST'])
def upload_quote():
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
    return requests.get('https://nostr.watch/relays.json').json()


@functools.lru_cache(maxsize=100)
def _get_relay_info(ws_url):
    http_url = re.sub(r'ws(s?)://', r'http\1://', ws_url)
    headers = {'Accept': 'application/nostr+json'}

    return _req_json('post', http_url, headers=headers, timeout=1)


@functools.lru_cache(maxsize=100)
def _get_link_preview(url):
    return _req_json('post', 'https://api.linkpreview.net', params={
        'key': env('LINKPREVIEW_API_KEY'),
        'q': url,
    })


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
