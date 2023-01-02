import requests, functools
from raddoo import env
from flask import Flask, request, g
from flask_cors import CORS


app = Flask(__name__)

cors = CORS(app, resource={
    r"/*":{
        "origins":"*"
    }
})


@app.route('/relay', methods=['GET'])
def relay_list():
    return _get_relays()


@app.route('/relay/info', methods=['POST'])
def relay_info():
    if not request.json.get('url'):
        return {'code': 'invalid-url'}

    return _get_relay_info(request.json['url'])


@app.route('/link/preview', methods=['POST'])
def link_preview():
    if not request.json.get('url'):
        return {'code': 'invalid-url'}

    url = request.json['url']

    content_type = requests.head(url).headers.get('Content-Type')

    if content_type.startswith('image/'):
        return {'title': "", 'description': "", 'image': url, 'url': url}

    return _get_link_preview(request.json['url'])


@functools.lru_cache()
def _get_relays():
    return requests.get('https://nostr.watch/relays.json').json()


@functools.lru_cache(maxsize=1000)
def _get_relay_info(url):
    res = requests.post(url.replace('wss://', 'https://'), headers={
        'Accept': 'application/nostr_json',
    })

    try:
        return res.json()
    except requests.exceptions.JSONDecodeError:
        return {}


@functools.lru_cache(maxsize=1000)
def _get_link_preview(url):
    res = requests.post('https://api.linkpreview.net', params={
        'key': env('LINKPREVIEW_API_KEY'),
        'q': url,
    })

    return res.json()

