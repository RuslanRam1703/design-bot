"""Export only the existing webapp; no data, server files, tokens or dependencies."""
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ('portfolio', 'about', 'calculator', 'brief', 'myleads')


def build(output: Path, api_origin: str, revision: str = '') -> None:
    url = urlsplit(api_origin)
    if (url.scheme != 'https' or not url.netloc or url.path or url.query or url.fragment
            or url.username or url.password):
        raise ValueError('APP_API_ORIGIN must be an HTTPS origin without a path or credentials')
    output = output.resolve()
    if output.name != 'static-dist' or output == ROOT:
        raise ValueError('Use static-dist or an external build directory')
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(ROOT / 'webapp', output)
    (output / 'js/runtime-config.js').write_text(
        'window.DESIGN_ASSISTANT_CONFIG = Object.freeze(' + json.dumps({'apiOrigin': api_origin}) + ');\n', encoding='utf-8')
    revision = revision if re.fullmatch(r'[0-9a-f]{40}', revision) else ''
    html = (output / 'index.html').read_text(encoding='utf-8')
    if revision:
        html = re.sub(r'((?:src|href)="/(?:js|css)/[^"?]+)(")', rf'\1?v={revision}\2', html)
    (output / 'index.html').write_text(html, encoding='utf-8')
    # Explicit aliases avoid a CDN rewrite dependency and preserve launch paths.
    for route in ROUTES:
        folder = output / route
        folder.mkdir()
        (folder / 'index.html').write_text(html, encoding='utf-8')
    (output / 'frontend-version.json').write_text(json.dumps({'revision': revision}), encoding='utf-8')


if __name__ == '__main__':
    build(ROOT / 'static-dist', os.environ['APP_API_ORIGIN'], os.getenv('RENDER_GIT_COMMIT', ''))
