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

@app.route('/link/preview', methods=['POST'])
def link_preview():
    if not request.json.get('url'):
        return {'code': 'invalid-url'}

    return _get_link_preview(request.json['url'])


@functools.lru_cache(maxsize=1000)
def _get_link_preview(url):
    res = requests.post('https://api.linkpreview.net', params={
        'key': env('LINKPREVIEW_API_KEY'),
        'q': url,
    })

    return res.json()
