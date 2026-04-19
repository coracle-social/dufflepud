import requests, functools, re, logging, json, mimetypes, os, redis, asyncio, aiohttp
from datetime import datetime, timezone
from urllib3.exceptions import LocationParseError
from requests.exceptions import (
    ConnectionError, JSONDecodeError, ReadTimeout, InvalidSchema, MissingSchema,
    InvalidURL, TooManyRedirects)
from raddoo import env, slurp, random_uuid, identity, merge
from flask import Flask, request
from flask_cors import CORS
from werkzeug.exceptions import BadRequest

MAX_CONTENT_LENGTH = env('MAX_CONTENT_LENGTH')
REDIS_URL = env('REDIS_URL')

redis_client = redis.from_url(REDIS_URL)

app = Flask(__name__)

app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

logger = logging.getLogger(__name__)

cors = CORS(app, resource={
    r"/*": {
        "origins": "*"
    }
})

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

    # Namecoin NIP-05: `.bit` domains are resolved from the Namecoin blockchain
    # instead of a DNS-backed HTTPS server. See NIP-05 + d/<name> convention.
    if domain.lower().endswith('.bit'):
        res = await _get_namecoin_nostr_record(domain)
    else:
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


async def _get_namecoin_nostr_record(domain):
    """Resolve a .bit domain's NIP-05 record from the Namecoin blockchain.

    Returns a dict shaped like a standard `/.well-known/nostr.json` payload
    (i.e. `{names, relays, nip46}`) so callers can treat the two resolution
    paths uniformly.

    Resolution strategy (opt-in; see `_namecoin_name_show`):
      1. If NAMECOIN_RPC_URL is set, query namecoind's JSON-RPC `name_show`
         directly (trustless, recommended for production).
      2. Else if NAMECOIN_HTTP_GATEWAY is set, fetch `<gateway>/<name>`
         which is expected to return a `name_show`-compatible JSON object.
      3. If neither is configured, `.bit` handles fail to resolve.

    Name records follow the d/<label> convention, e.g. `alice.bit` ->
    `d/alice`. The `value` is parsed as JSON; the nostr record can live
    under either `value.nostr` (Namecoin dNS/NIP-05 convention) or the
    whole value may itself be a NIP-05 `{names, relays, nip46}` object.
    """
    label = domain[:-4].lower()  # strip '.bit'
    if not label or '/' in label or '\0' in label:
        return None

    name_show = await _namecoin_name_show(f'd/{label}')
    if not name_show:
        return None

    raw_value = name_show.get('value')
    if not raw_value:
        return None

    try:
        value = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except (ValueError, TypeError):
        return None

    if not isinstance(value, dict):
        return None

    # Preferred layout: value.nostr = { names, relays, nip46 }
    nostr = value.get('nostr')
    if isinstance(nostr, dict) and 'names' in nostr:
        return nostr

    # Fallback: value itself is the NIP-05 record
    if 'names' in value:
        return value

    return None


async def _namecoin_name_show(name):
    """Call Namecoin `name_show` via either JSON-RPC or an HTTPS gateway.

    Returns `None` if neither backend is configured (opt-in behavior), which
    causes `.bit` handles to resolve to `None` just like an unreachable DNS
    NIP-05 host would. Regular DNS-based NIP-05 is unaffected.
    """
    rpc_url = env('NAMECOIN_RPC_URL')
    if rpc_url:
        payload = {
            'jsonrpc': '1.0',
            'id': 'dufflepud',
            'method': 'name_show',
            'params': [name],
        }
        res = await req_json_async(
            'post', rpc_url,
            json=payload,
            headers={'Content-Type': 'application/json'},
        )
        if not res:
            return None
        # JSON-RPC error or missing result -> treat as unresolved
        if res.get('error'):
            return None
        return res.get('result')

    gateway = env('NAMECOIN_HTTP_GATEWAY')
    if gateway:
        gateway = gateway.rstrip('/')
        return await req_json_async('get', f'{gateway}/{name}')

    # Namecoin resolution is opt-in; without config, `.bit` handles simply
    # fail to resolve (same observable behavior as an unreachable host).
    return None


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
