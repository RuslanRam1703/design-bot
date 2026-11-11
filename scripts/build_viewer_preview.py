"""Generate an offline desktop/mobile-width harness with synthetic images."""
import json,html
from pathlib import Path
root=Path(__file__).resolve().parents[1]
css=(root/'webapp/css/style.css').read_text();viewer=(root/'webapp/js/case-viewer.js').read_text();app=(root/'webapp/js/app.js').read_text().rsplit('init();',1)[0]
svg='<svg xmlns="http://www.w3.org/2000/svg" width="900" height="2600"><rect width="900" height="2600" fill="#18233f"/>'+''.join(f'<text x="50" y="{90+i*150}" fill="#c2d0ff" font-size="28">Проверка деталей — строка {i+1}</text>' for i in range(17))+'</svg>'
import base64
src='data:image/svg+xml;base64,'+base64.b64encode(svg.encode()).decode()
# imageSrc treats relative paths as backend paths; fixture replaces source after rendering.
seed=json.dumps({'id':'test','title':'Длинный макет — проверка','images':['img/test.svg'],'task':'Демонстрационный материал для проверки просмотра.','solution':'','result':''},ensure_ascii=False)
inner=f'''<!doctype html><html lang="ru"><meta name="viewport" content="width=device-width,initial-scale=1"><style>{css}</style><div id="app"></div><script>{viewer}</script><script>{app}\nstate.currentCase={seed};state.screen='case';state.pricing={{services:[]}};render();document.querySelector('.case-images img').src={json.dumps(src)};</script></html>'''
page='<!doctype html><meta charset="utf-8"><title>Проверка просмотра изображений</title><style>body{background:#111827;color:white;font:16px sans-serif}iframe{height:720px;border:1px solid #667;display:inline-block;vertical-align:top}button{padding:12px;margin:8px}</style><h1>Проверка: изображение в окне приложения</h1><p>Демонстрационный макет. Слева 390 px, справа 800 px. Это браузерная проверка, не Telegram.</p>'+''.join('<iframe title="'+name+'" style="width:'+str(w)+'px" srcdoc="'+html.escape(inner,quote=True)+'"></iframe>' for w,name in [(390,'Телефон'),(800,'Компьютер')])
import tempfile
output = Path(tempfile.gettempdir()) / 'design-viewer-preview.html'
output.write_text(page, encoding='utf-8')
print(output)
