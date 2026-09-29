// Render the fixture with test_nori_chat.py --fixture PATH, then run this
// script with PATH. PLAYWRIGHT_MODULE may point to an existing installation.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let state = 'determined';
    let nextId = 3;
    await page.route('http://nori.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/avatar/')) {
        return route.fulfill({contentType: 'image/png', body: fs.readFileSync(
          path.join(__dirname, '../nori/static/avatars', url.pathname.split('/').pop() + '.png'))});
      }
      if (url.pathname === '/poll') return route.fulfill({json: {state, messages: []}});
      if (url.pathname === '/send') {
        state = state === 'determined' ? 'happy' : 'determined';
        return route.fulfill({json: {ok: true, state, user_id: nextId++, messages: [
          {id: nextId++, role: 'assistant', kind: 'tool', content: 'used set_emotion'},
          {id: nextId++, role: 'assistant', kind: 'tool', content: 'used remember'},
          {id: nextId++, role: 'assistant', kind: 'chat', emotion: state, content: 'Ready.'}
        ]}});
      }
      return route.fulfill({contentType: 'text/html', body: fs.readFileSync(process.argv[2])});
    });
    async function send() {
      await page.locator('#msgInput').fill('Hello');
      await page.locator('#sendBtn').click();
      await page.waitForFunction(() => !document.querySelector('#sendBtn').disabled);
    }
    async function assertFits(selector, width, height) {
      const box = await page.locator(selector).boundingBox();
      assert(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= width + 1 && box.y + box.height <= height + 1,
        `${selector} must fit ${width}x${height}: ${JSON.stringify(box)}`);
      return box;
    }
    for (const [width, height] of [[320, 640], [390, 844], [844, 390]]) {
      await page.setViewportSize({width, height});
      await page.goto('http://nori.test/');
      await page.keyboard.press('Escape');
      assert.equal((await page.locator('#avatarChip').boundingBox()).width, 80);
      for (const link of await page.locator('.chat-actions a').all()) {
        const box = await link.boundingBox();
        assert(box.height >= 44 && box.x >= 0 && box.x + box.width <= width);
      }
      await assertFits('.shell-header', width, height);
      await assertFits('.shell-footer', width, height);
      await page.locator('#avatarChip').click();
      await page.waitForTimeout(350);
      await assertFits('#peek', width, height);
      assert.equal(await page.locator('#peek img').evaluate(el => getComputedStyle(el).objectFit), 'contain');
      await page.locator('#peek').click();
      await page.waitForFunction(() => !document.querySelector('#peek').classList.contains('show'));
      await page.locator('#menuBtn').click();
      assert.equal(await page.locator('#menuSheet').evaluate(el => el.classList.contains('show')), true);
      // An emotion update must not obscure an open menu or close its backdrop.
      await page.evaluate(() => nbShowPeek(false));
      assert.equal(await page.locator('#peek').evaluate(el => el.classList.contains('show')), false);
      await page.keyboard.press('Escape');
    }
    await page.setViewportSize({width: 390, height: 844});
    await page.goto('http://nori.test/');
    await page.keyboard.press('Escape');
    await send();
    assert.equal(await page.locator('.toolline').allTextContents().then(a => a.includes('used set_emotion')), false);
    assert.equal(await page.locator('.toolline').first().textContent(), 'used remember');
    assert.equal(await page.locator('#peek img').getAttribute('src'), '/avatar/happy');
    await page.waitForFunction(() => document.querySelector('#peek').classList.contains('show'));
    await page.waitForTimeout(4200);
    assert.equal(await page.locator('#peek').evaluate(el => el.classList.contains('show')), false);
    await page.locator('#avatarChip').click();
    await page.waitForTimeout(4200);
    assert.equal(await page.locator('#peek').evaluate(el => el.classList.contains('show')), true);
    await page.setViewportSize({width: 1280, height: 800});
    await page.waitForTimeout(350);
    assert.equal(await page.locator('#nbBackdrop').evaluate(el => el.classList.contains('show')), false);
    await page.goto('http://nori.test/');
    assert.equal(await page.locator('.hero-frame img').count(), 1);
    assert.equal(await page.locator('.hero-frame img').getAttribute('src'), '/avatar/determined');
    for (let i = 0; i < 3; i++) {
      await send();
      assert.equal(await page.locator('.hero-frame img').count(), 1);
      assert.equal(await page.locator('.hero-frame img').getAttribute('src'), '/avatar/' + state);
      assert.equal(await page.locator('.hero-frame img').getAttribute('alt'), state);
      assert.equal(await page.locator('#peek').evaluate(el => el.classList.contains('show')), false);
    }
    assert.deepEqual(errors, []);
    console.log('Passed: mobile layouts, full portrait preview/dismissal, menu interaction, live tool filtering, and single desktop avatar across emotion changes.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
