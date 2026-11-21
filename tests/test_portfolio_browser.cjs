const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const media = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const name = url.pathname === '/' ? 'webapp/index.html'
    : url.pathname.startsWith('/data/') ? url.pathname.slice(1) : 'webapp' + url.pathname;
  try {
    const body = await fs.readFile(path.join(root, name));
    res.writeHead(200, { 'Content-Type': media[path.extname(name)] || 'text/plain' });
    res.end(body);
  } catch {
    res.writeHead(404).end();
  }
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch();
    for (const width of [390, 800, 1440]) {
      const page = await browser.newPage({ viewport: { width, height: 800 }, reducedMotion: 'reduce' });
      await page.route('https://telegram.org/**', route => route.abort());
      page.setDefaultTimeout(6000);
      await page.goto('http://127.0.0.1:' + server.address().port);
      const preview = page.locator('#portfolio-preview');
      await preview.waitFor();
      assert.equal(await page.locator('[data-preview-case]').count(), 10);
      const initial = await preview.boundingBox();
      const grid = await page.locator('.portfolio-layout .grid').boundingBox();
      assert.ok(width >= 1100 ? grid.x > initial.x + initial.width && grid.y === initial.y : grid.y > initial.y);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
      const tile = page.locator('[data-preview-case="case_9"]');
      await tile.scrollIntoViewIfNeeded();
      const savedScroll = await page.evaluate(() => scrollY);
      await tile.click();
      if (width >= 1100) {
        await preview.locator('[data-open-case="case_9"]').waitFor();
        assert.equal(await tile.getAttribute('aria-pressed'), 'true');
        await preview.locator('button').click();
      }
      await page.locator('#app[data-screen="case"]').waitFor();
      await page.locator('#back').click();
      await preview.waitFor();
      if (width < 1100) {
        assert.equal(await page.evaluate(() => scrollY), savedScroll);
        assert.equal(await tile.evaluate(el => document.activeElement === el), true);
      }
      console.log(width + ' px: portfolio layout, opening case and back passed');
      await page.close();
    }
  } finally {
    await browser?.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
