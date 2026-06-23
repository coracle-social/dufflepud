import requests, functools, re, logging, json, mimetypes, os, redis, asyncio, aiohttp, base64, hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib3.exceptions import LocationParseError
from requests.exceptions import (
    ConnectionError, JSONDecodeError, ReadTimeout, InvalidSchema, MissingSchema,
    InvalidURL, TooManyRedirects)
from raddoo import env, slurp, random_uuid, identity, merge
from flask import Flask, request, Response
from flask_cors import CORS
from werkzeug.exceptions import BadRequest, Unauthorized
from coincurve import PublicKeyXOnly

MAX_CONTENT_LENGTH = env('MAX_CONTENT_LENGTH')
REDIS_URL = env('REDIS_URL')

# Base url of this dufflepud instance, e.g. https://dufflepud.example.com.
# NIP 98 auth events must carry a matching `u` tag.
BASE_URL = env('BASE_URL')

# How long a NIP 98 auth event stays valid after it was signed. Standard NIP 98
# uses a ~60s window; we widen it to 24 hours so a single signed token can
# authenticate many requests, sparing the user repeated signing prompts.
NIP98_TTL = 24 * 60 * 60

# How long a stored kv value lives before redis expires it.
KV_TTL = 7 * 24 * 60 * 60

redis_client = redis.from_url(REDIS_URL)

app = Flask(__name__)

app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

logger = logging.getLogger(__name__)

cors = CORS(app, resource={
    r"/*": {
        "origins": "*"
    }
})

# Web crawlers and link-preview scrapers (e.g. Facebook's meta-externalagent,
# hitting /media/alert via shared coracle.social links) generate load without
# corresponding to a real client. Turn away anything whose User-Agent
# self-identifies as a bot. Matches the common crawler tokens plus a handful of
# named scrapers that don't include "bot" in their UA.
BOT_UA_RE = re.compile(
    r'bot|crawl|spider|slurp|mediapartners|meta-externalagent|'
    r'facebookexternalhit|embedly|bingpreview|skypeuripreview|'
    r'whatsapp|telegram|discord|scrape|monitor|archive\.org',
    re.IGNORECASE)


@app.before_request
def block_bots():
    ua = request.headers.get('User-Agent', '')

    if BOT_UA_RE.search(ua):
        # 410 Gone signals well-behaved crawlers to stop re-crawling the URL.
        return Response("Bots are not welcome here\n", status=410, mimetype='text/plain')


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
async def relay_info():
    urls = get_json('urls')

    results = await asyncio.gather(*[_get_relay_info(url) for url in urls])

    return {'data': [{'url': url, 'info': info} for url, info in zip(urls, results)]}


@app.route('/handle/info', methods=['POST'])
async def handle_info():
    handles = get_json('handles')

    results = await asyncio.gather(*[_get_handle_info(handle) for handle in handles])

    return {'data': [{'handle': handle, 'info': info} for handle, info in zip(handles, results)]}


@app.route('/zapper/info', methods=['POST'])
async def zapper_info():
    lnurls = get_json('lnurls')
    results = await asyncio.gather(*[_get_zapper_info(lnurl) for lnurl in lnurls])

    return {'data': [{'lnurl': lnurl, 'info': info} for lnurl, info in zip(lnurls, results)]}


@app.route('/link/preview', methods=['POST'])
async def link_preview():
    url = get_json('url')
    res = req('head', url)

    if (res.headers.get('Content-Type', '') if res else '').startswith('image/'):
        return {'title': "", 'description': "", 'image': url, 'url': url}

    return await _get_link_preview(url) or {}


@app.route('/media/alert', methods=['POST'])
async def link_alert():
    url = get_json('url')

    return await _get_media_alert(url) or {}


@app.route('/kv/<key>', methods=['GET'])
def kv_get(key):
    pubkey = verify_nip98()

    value = redis_client.get(_kv_key(pubkey, key))

    if value is None:
        return err('not-found', "No value is stored for that key")

    return Response(value, mimetype='text/plain')


@app.route('/kv/<key>', methods=['POST'])
def kv_set(key):
    pubkey = verify_nip98()

    redis_client.setex(_kv_key(pubkey, key), KV_TTL, request.get_data())

    return Response(status=204)


# Utils


def now():
    return datetime.now(timezone.utc)


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
        raise BadRequest(f"`{name}` is a required parameter")


def _kv_key(pubkey, key):
    return f"kv:{pubkey}:{key}"


def _nostr_event_id(event):
    # NIP-01 id: sha256 of the compact, no-whitespace serialization of
    # [0, pubkey, created_at, kind, tags, content].
    serialized = json.dumps(
        [0, event['pubkey'], event['created_at'], event['kind'], event['tags'], event['content']],
        separators=(',', ':'),
        ensure_ascii=False,
    )

    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


def _last_tag_value(tags, name):
    value = None

    for tag in tags:
        if len(tag) >= 2 and tag[0] == name:
            value = tag[1]

    return value


def _check_u_tag(tags):
    if not BASE_URL:
        return

    u = _last_tag_value(tags, 'u')

    if u is None:
        raise Unauthorized("nip98 event is missing a `u` tag")

    want = urlparse(BASE_URL)
    got = urlparse(u)

    if (got.scheme.lower(), got.netloc.lower()) != (want.scheme.lower(), want.netloc.lower()):
        raise Unauthorized("nip98 `u` tag does not match this server")


def verify_nip98():
    auth = request.headers.get('Authorization', '')

    if not auth.startswith('Nostr '):
        raise Unauthorized("Authorization header must use the Nostr scheme")

    try:
        event = json.loads(base64.b64decode(auth[len('Nostr '):].strip()))
        pubkey, eid, sig = event['pubkey'], event['id'], event['sig']
        kind, created_at, tags = event['kind'], int(event['created_at']), event['tags']
    except Exception:
        raise Unauthorized("malformed nip98 authorization event")

    if kind != 27235:
        raise Unauthorized("invalid nip98 event kind")

    # We deliberately do not validate the `method` tag.

    if abs(int(now().timestamp()) - created_at) > NIP98_TTL:
        raise Unauthorized("nip98 event has expired")

    if _nostr_event_id(event) != eid:
        raise Unauthorized("nip98 event id does not match its contents")

    try:
        valid = PublicKeyXOnly(bytes.fromhex(pubkey)).verify(bytes.fromhex(sig), bytes.fromhex(eid))
    except Exception:
        valid = False

    if not valid:
        raise Unauthorized("invalid nip98 event signature")

    _check_u_tag(tags)

    return pubkey


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


async def req_json_async(method, url, **kw):
    async with aiohttp.ClientSession() as session:
        try:
            f = getattr(session, method)

            async with f(url, timeout=10, **kw) as response:
                return json.loads(await response.text())
        except:
            return None


def redis_cache(ns, expiration_time=300):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(key):
            cache_key = f"{ns}:{key}"

            cached_result = redis_client.get(cache_key)
            if cached_result:
                return json.loads(cached_result)

            result = await func(key)

            redis_client.setex(cache_key, expiration_time, json.dumps(result))

            return result
        return wrapper
    return decorator

# Loaders

@functools.lru_cache()
def _get_relays():
    return req_json('get', 'https://nostr.watch/relays.json')


@redis_cache('relay')
async def _get_relay_info(ws_url):
    http_url = re.sub(r'ws(s?)://', r'http\1://', ws_url)
    headers = {'Accept': 'application/nostr+json'}

    return await req_json_async('get', http_url, headers=headers)


@redis_cache('handle')
async def _get_handle_info(handle):
    parts = handle.split('@')
    name = parts[0] if len(parts) > 1 else '_'
    domain = parts[-1]

    res = await req_json_async('get', f'https://{domain}/.well-known/nostr.json?name={name}')

    if not res:
        return None

    pubkey = res.get('names', {}).get(name)

    if not pubkey:
        return None

    return {
        'pubkey': pubkey,
        'relays': res.get('relays', {}).get(pubkey),
        'nip46': res.get('nip46', {}).get(pubkey),
    }


@redis_cache('zapper')
async def _get_zapper_info(lnurl):
    return await req_json_async('get', lnurl)


@redis_cache('link_preview')
async def _get_link_preview(url):
    return await req_json_async('post', 'https://api.linkpreview.net', params={
        'key': env('LINKPREVIEW_API_KEY'),
        'q': url,
    })

@redis_cache('media_alert')
async def _get_media_alert(url):
    return await req_json_async('get', 'https://nostr-media-alert.com/score', params={
        'key': env('MEDIA_ALERT_API_KEY'),
        'url': url,
    })
