"""The bundled Lovelace card in a real browser (Chromium via Playwright), on a fake hass.

The fake hass is card_hass.json: real entity states written by the integration on the replayed
day (tests/test_card_fixture.py keeps it in sync). Pages are served by Playwright's router from
the files on disk, so nothing goes to the network.

Run: pip install pytest playwright && python -m playwright install chromium
     python -m pytest -q tests_card
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).parent.parent
WWW = ROOT / "custom_components" / "render_monitor" / "www"
FIXTURE = json.loads((Path(__file__).parent / "card_hass.json").read_text(encoding="utf-8"))
ORIGIN = "http://card.test"
NON_LATIN = "[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in ((0x400, 0x4FF), (0x590, 0x5FF), (0x600, 0x6FF))) + "]"
SHOTS = os.environ.get("RENDER_MONITOR_CARD_SHOTS")  # a folder: also save screenshots there

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --primary-text-color:#212121; --secondary-text-color:#727272; --card-background-color:#ffffff;
          --primary-color:#03a9f4; --accent-color:#ff9800; --divider-color:rgba(0,0,0,.12);
          --secondary-background-color:#f5f5f5; }
  body.dark { --primary-text-color:#e1e1e1; --secondary-text-color:#9b9b9b; --card-background-color:#1c1c1c;
              --divider-color:rgba(225,225,225,.12); --secondary-background-color:#282828; background:#111; }
  body { margin: 0; padding: 16px; background: #fafafa; font-family: sans-serif; }
  ha-card { display: block; border-radius: 12px; }
</style></head><body>
<script src="/render_monitor_static/render-monitor-card.js?v=1"></script>
<script>
  window.FIXTURE = __FIXTURE__;
  window.makeHass = (lang, extra) => Object.assign({
    language: lang, locale: { language: lang, time_zone: "server" },
    config: { time_zone: "UTC" }, themes: { darkMode: false },
    states: JSON.parse(JSON.stringify(window.FIXTURE.states)),
    entities: window.FIXTURE.entities, devices: window.FIXTURE.devices,
  }, extra || {});
  window.mount = (config, lang, extra) => {
    document.querySelectorAll("render-monitor-card").forEach((el) => el.remove());
    const card = document.createElement("render-monitor-card");
    card.setConfig(Object.assign({ type: "custom:render-monitor-card" }, config || {}));
    document.body.appendChild(card);
    card.hass = window.makeHass(lang || "en", extra);
    return card;
  };
</script></body></html>"""


@pytest.fixture(scope="module")
def browser():
    with playwright_sync.sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    """A page on the fake origin. page.requests lists every URL asked for; page.errors every JS error."""
    context = browser.new_context(viewport={"width": 900, "height": 1400})
    pg = context.new_page()
    pg.requests, pg.errors, pg.texts_status = [], [], 200

    def route(r):
        url = r.request.url
        pg.requests.append(url)
        path = url.split(ORIGIN, 1)[-1].split("?", 1)[0]
        if path == "/":
            return r.fulfill(body=PAGE.replace("__FIXTURE__", json.dumps(FIXTURE)), content_type="text/html")
        if path == "/render_monitor_static/render-monitor-card.js":
            return r.fulfill(body=(WWW / "render-monitor-card.js").read_text(encoding="utf-8"), content_type="text/javascript")
        if path == "/render_monitor_static/advice_texts.json":
            if pg.texts_status != 200:
                return r.fulfill(status=pg.texts_status, body="nope")
            return r.fulfill(body=(WWW / "advice_texts.json").read_text(encoding="utf-8"), content_type="application/json")
        return r.fulfill(status=404, body="")

    context.route("**/*", route)
    pg.on("pageerror", lambda err: pg.errors.append(str(err)))
    pg.on("console", lambda msg: pg.errors.append(msg.text) if msg.type == "error" else None)
    pg.clock.set_fixed_time("2026-11-16T20:00:00Z")

    def open_(texts_status=200):
        pg.texts_status = texts_status
        pg.goto(ORIGIN + "/")
        pg.wait_for_function("!!customElements.get('render-monitor-card')")
    pg.open = open_
    yield pg
    context.close()


def mount(page, config=None, lang="en", extra=None, wait_texts=True):
    page.evaluate("([c, l, e]) => { window.card = window.mount(c, l, e); }", [config or {}, lang, extra])
    if wait_texts:  # advice texts arrive by fetch; the card re-renders when they do
        page.wait_for_function("() => window.card.shadowRoot.querySelector('ha-card') && "
                               "[...window.card.shadowRoot.querySelectorAll('.advice li .text')].every(e => !/^[a-z_]+$/.test(e.textContent))")
        page.wait_for_timeout(50)


def shadow(page, js):
    """Evaluate js with `root` = the card's shadow root."""
    return page.evaluate(f"(() => {{ const root = window.card.shadowRoot; return {js}; }})()")


def services(page):
    return shadow(page, "[...root.querySelectorAll('section.svc')].map(s => s.dataset.service)")


def section(page, name, js):
    return shadow(page, f"(() => {{ const s = root.querySelector('section[data-service=\"{name}\"]'); return {js}; }})()")


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=True)


# ------------------------------------------------------------------ rendering

def test_one_block_per_service_from_real_states(page):
    page.open()
    mount(page)
    shot(page, "en_light")
    assert services(page) == ["demo-api", "demo-shop", "demo-site", "demo-worker"]
    assert shadow(page, "root.querySelector('ha-card').getAttribute('dir')") == "ltr"

    shop = lambda js: section(page, "demo-shop", js)  # noqa: E731
    assert shop("s.querySelector('.chip').dataset.status") == "live"
    assert shop("s.querySelector('.chip').textContent") == "Live"
    assert shop("s.querySelector('.type').textContent") == "Web service"
    assert shop("s.querySelector('.price .ptext').textContent") == "starter · $7.25/month"
    stats = shop("[...s.querySelectorAll('.stat')].map(b => [b.querySelector('.k').textContent, b.querySelector('.v').textContent, (b.querySelector('.s')||{}).textContent || ''])")
    assert stats == [
        ["Memory", "peak 86%", "440 / 512 MB"],
        ["CPU", "average 13%", "peak 90%"],
        ["p95", "120 ms", "peak 2,400 ms"],
        ["Slow/h", "0", "24h: 13"],
        ["Disk", "30%", "0.3 / 1 GB"],
        ["Deploys today", "15", "1 failed"],
        ["Restarts today", "14", ""],
    ]
    assert shop("s.querySelectorAll('.chart polyline').length") == 2
    assert shop("s.querySelector('.chart').getAttribute('aria-label')") == (
        "Memory and CPU usage over the last 24 hours; peak memory 86%, peak CPU 90%")
    assert shop("s.querySelector('.deploy').textContent.replace(/\\s+/g, ' ').trim()") == (
        "Last deploy: live · 1 hour ago · 0000000 Small change 1")
    assert shop("s.querySelector('time').getAttribute('title')") == "Nov 16, 2026, 6:54 PM"
    assert shop("[...s.querySelectorAll('.advice li')].map(li => li.dataset.code)") == [
        "memory", "many_deploys", "latency_spike", "slow_spike", "deploy_failed"]
    assert shop("s.classList.contains('attention')") is True
    assert shop("s.querySelector('a.link').href") == "https://dashboard.render.com/web/srv-00000000000000000004"

    api = lambda js: section(page, "demo-api", js)  # noqa: E731
    assert api("s.querySelector('.chip').dataset.status") == "deploy_failed"
    assert api("s.querySelector('.price .ptext').textContent") == "standard · $27.50/month"
    assert api("s.querySelector('.advice li .sev').textContent") == "Critical"
    assert api("s.querySelector('.deploy .bad').textContent") == "failed"

    site = lambda js: section(page, "demo-site", js)  # noqa: E731
    assert site("s.querySelector('.price .ptext').textContent") == "Free"
    # a suspended paid service costs $0 now, but it is not a free type: no "Free"
    worker = lambda js: section(page, "demo-worker", js)  # noqa: E731
    assert worker("s.querySelector('.price .ptext').textContent") == "starter · $0/month"
    assert site("s.querySelector('.chart')") is None
    assert site("s.querySelector('.advice .none').textContent") == "No action is required."
    assert site("s.classList.contains('attention')") is False

    assert section(page, "demo-worker", "s.querySelector('.chip').dataset.status") == "suspended"
    assert section(page, "demo-worker", "s.querySelector('.stat .v').textContent") == "peak -"  # no metrics while suspended
    assert shadow(page, "root.querySelector('.foot').textContent") == (
        "Prices are as of 2026-11-16; please verify them at render.com/pricing.")
    assert page.errors == []


@pytest.mark.parametrize("lang", ["en", "he", "ru", "ar", "de"])
def test_card_is_english_whatever_the_ha_language(page, lang):
    """Same English templates filled the same way as advice.render(), left to right, in any HA language."""
    page.open()
    mount(page, lang=lang)
    checked = 0
    for entity_id, expected in FIXTURE["expected_texts"].items():
        device = FIXTURE["entities"][entity_id]["device_id"]
        name = FIXTURE["devices"][device]["name"]
        shown = section(page, name, "[...s.querySelectorAll('.advice li .text')].map(e => e.textContent)")
        assert shown == expected, name
        checked += len(shown)
    assert checked >= 6
    assert shadow(page, "root.querySelector('ha-card').getAttribute('dir')") == "ltr"
    assert shadow(page, "root.querySelector('ha-card').getAttribute('lang')") == "en"
    shop = lambda js: section(page, "demo-shop", js)  # noqa: E731
    assert shop("s.querySelector('.chip').textContent") == "Live"
    assert shop("s.querySelector('.advice h4').textContent") == "Advice"
    assert section(page, "demo-api", "s.querySelector('.advice li .sev').textContent") == "Critical"
    # numbers and times in English too (no other digits or separators)
    assert shop("s.querySelectorAll('.stat .s')[2].textContent") == "peak 2,400 ms"
    assert shop("s.querySelector('time').textContent") == "1 hour ago"
    assert not re.search(NON_LATIN, shadow(page, "root.textContent"))
    assert page.errors == []


def test_card_texts_are_one_english_table(page):
    page.open()
    texts = page.evaluate("customElements.get('render-monitor-card').TEXTS")
    terms = page.evaluate("customElements.get('render-monitor-card').TERMS")
    assert all(isinstance(value, str) for value in texts.values())  # flat: no language codes
    for key, value in texts.items():
        assert value.isascii(), key
        assert value.strip() or key == "title", key
    for term in terms:
        assert f"term_{term}" in texts and f"tip_{term}" in texts, term
        tip = texts[f"tip_{term}"]
        assert tip[0].isupper() and tip.endswith("."), term  # info tips are complete sentences


# ------------------------------------------------------------------ behaviour

def test_info_tip_opens_closes_and_survives_updates(page):
    page.open()
    mount(page)
    first = "s.querySelector('.advice li .tip')"
    assert section(page, "demo-shop", f"{first}.textContent") == "memory limit"
    page.evaluate("() => window.card.shadowRoot.querySelector('section[data-service=\"demo-shop\"] .advice li .tip').click()")
    assert section(page, "demo-shop", f"{first}.getAttribute('aria-expanded')") == "true"
    note = section(page, "demo-shop", "s.querySelector('.advice li .note').textContent")
    assert note.startswith("Memory limit: The amount of RAM provided by the instance type.")
    assert shadow(page, "root.activeElement && root.activeElement.dataset.tip") == section(page, "demo-shop", f"{first}.dataset.tip")
    # a new hass object with the same states (HA sends one on every state change anywhere)
    page.evaluate("() => { window.card.hass = window.makeHass('en'); }")
    page.evaluate("() => { const h = window.makeHass('en'); h.states['sensor.demo_shop_deploys_today'].state = '16'; window.card.hass = h; }")
    assert section(page, "demo-shop", f"{first}.getAttribute('aria-expanded')") == "true"
    assert section(page, "demo-shop", "[...s.querySelectorAll('.stat .v')][5].textContent") == "16"
    page.evaluate("() => window.card.shadowRoot.querySelector('section[data-service=\"demo-shop\"] .advice li .tip').click()")
    assert section(page, "demo-shop", "s.querySelector('.advice li .note')") is None
    # the price tip
    page.evaluate("() => window.card.shadowRoot.querySelector('section[data-service=\"demo-shop\"] .price .tip').click()")
    assert section(page, "demo-shop", "s.querySelector('.note').textContent").startswith("Monthly cost: Instances x")


def test_term_tips_per_advice(page):
    page.open()
    mount(page)
    terms = section(page, "demo-shop", "[...s.querySelectorAll('.advice li')].map(li => [...li.querySelectorAll('.tip')].map(t => t.dataset.term))")
    assert terms == [
        ["memory_limit", "instance_type", "import"],
        ["zero_downtime", "disk", "build_filters"],
        ["p95", "import"],
        ["slow_request", "import"],
        ["deploy", "commit"],
    ]


def test_status_chip_and_stats_open_more_info(page):
    page.open()
    mount(page)
    page.evaluate("() => { window.moreInfo = []; window.addEventListener('hass-more-info', e => window.moreInfo.push(e.detail.entityId)); }")
    page.evaluate("() => { const s = window.card.shadowRoot.querySelector('section[data-service=\"demo-shop\"]');"
                  " s.querySelector('.chip').click(); s.querySelector('.stat').click(); }")
    assert page.evaluate("window.moreInfo") == ["sensor.demo_shop_status", "sensor.demo_shop_memory_peak_24_h"]


def test_text_from_render_is_never_html(page):
    """Commit messages, names and errors come from Render/git: shown as text, never run."""
    page.open()
    evil = '<img src=x onerror="window.__xss=1">'
    page.evaluate("""(evil) => {
        const h = window.makeHass('en');
        h.states['sensor.demo_shop_last_deploy'].attributes.message = evil;
        h.states['sensor.demo_shop_status'].attributes.errors = [evil];
        h.devices = JSON.parse(JSON.stringify(h.devices));
        h.devices['device_4'].name = 'demo-shop' + evil;
        h.devices['device_4'].configuration_url = 'javascript:alert(1)';
        const card = document.createElement('render-monitor-card');
        card.setConfig({type: 'custom:render-monitor-card', title: evil});
        document.body.appendChild(card); card.hass = h; window.card = card;
    }""", evil)
    page.wait_for_timeout(200)
    assert shadow(page, "root.querySelectorAll('img').length") == 0
    assert page.evaluate("window.__xss") is None
    assert shadow(page, "root.querySelector('.head').textContent") == evil
    assert evil in shadow(page, "[...root.querySelectorAll('.deploy .msg')].map(e => e.textContent)")
    assert shadow(page, "[...root.querySelectorAll('.name')].some(e => e.textContent === 'demo-shop' + " + json.dumps(evil) + ")")
    assert evil in shadow(page, "root.querySelector('.errors').title")
    assert shadow(page, "[...root.querySelectorAll('a')].map(a => a.href).filter(h => !h.startsWith('https://'))") == []


def test_falls_back_to_the_integration_texts_when_the_json_cannot_be_read(page):
    page.open(texts_status=404)
    mount(page, wait_texts=False)
    page.wait_for_timeout(200)
    for entity_id in FIXTURE["expected_texts"]:
        name = FIXTURE["devices"][FIXTURE["entities"][entity_id]["device_id"]]["name"]
        shown = section(page, name, "[...s.querySelectorAll('.advice li .text')].map(e => e.textContent)")
        assert shown == FIXTURE["states"][entity_id]["attributes"]["texts"]


def test_service_list_options(page):
    page.open()
    mount(page, {"services": ["demo-shop", "demo-api"], "show_chart": False, "show_advice": False, "title": ""})
    assert services(page) == ["demo-shop", "demo-api"]  # the order given
    assert shadow(page, "root.querySelector('.chart, .advice, .head')") is None
    mount(page, {"services": ["no-such-service"]}, wait_texts=False)
    assert shadow(page, "root.querySelector('.empty').textContent").startswith("No Render services were found.")


def test_no_integration_shows_how_to_add_it(page):
    page.open()
    mount(page, lang="he", extra={"entities": {}, "devices": {}, "states": {}}, wait_texts=False)
    texts = page.evaluate("customElements.get('render-monitor-card').TEXTS")
    assert shadow(page, "root.querySelector('.empty').textContent") == texts["empty"]
    assert texts["empty"].startswith("No Render services were found.")
    assert shadow(page, "root.querySelector('ha-card').getAttribute('dir')") == "ltr"


def test_bad_config_is_refused(page):
    page.open()
    error = page.evaluate("""() => { try { document.createElement('render-monitor-card').setConfig({services: 'demo-shop'}); }
                                  catch (e) { return e.message; } return null; }""")
    assert error == "services must be a list of service names"


def test_dark_theme_uses_the_theme_colours(page):
    page.open()
    page.evaluate("() => document.body.classList.add('dark')")
    mount(page, extra={"themes": {"darkMode": True}})
    shot(page, "en_dark")
    colours = shadow(page, "(() => { const c = getComputedStyle(root.querySelector('ha-card'));"
                           " return [c.color, c.backgroundColor]; })()")
    assert colours == ["rgb(225, 225, 225)", "rgb(28, 28, 28)"]


def test_phone_width_has_no_sideways_scroll(page):
    page.set_viewport_size({"width": 375, "height": 900})
    page.open()
    mount(page)
    shot(page, "en_phone")
    widths = page.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert widths[0] <= widths[1]
    overflow = shadow(page, "[...root.querySelectorAll('section.svc *')].filter(e => e.getBoundingClientRect().right > root.host.getBoundingClientRect().right + 1 || e.getBoundingClientRect().left < root.host.getBoundingClientRect().left - 1).map(e => e.className)")
    assert overflow == []


def test_only_the_card_and_its_texts_are_requested(page):
    page.open()
    mount(page)
    paths = sorted({u.split(ORIGIN, 1)[-1].split("?", 1)[0] for u in page.requests})
    assert paths == ["/", "/render_monitor_static/advice_texts.json", "/render_monitor_static/render-monitor-card.js"]


# ------------------------------------------------------------------ editor and picker

def test_card_picker_and_stub(page):
    page.open()
    assert page.evaluate("window.customCards.filter(c => c.type === 'render-monitor-card').length") == 1
    assert page.evaluate("customElements.get('render-monitor-card').getStubConfig()") == {"type": "custom:render-monitor-card"}


def test_editor_writes_the_config(page):
    page.open()
    page.evaluate("""() => {
        window.changes = [];
        const ed = customElements.get('render-monitor-card').getConfigElement();
        ed.addEventListener('config-changed', e => window.changes.push(e.detail.config));
        document.body.appendChild(ed);
        ed.setConfig({type: 'custom:render-monitor-card'});
        ed.hass = window.makeHass('en');
        window.ed = ed;
    }""")
    labels = page.evaluate("[...window.ed.querySelectorAll('[data-service]')].map(i => i.dataset.service)")
    assert labels == ["demo-api", "demo-shop", "demo-site", "demo-worker"]
    page.evaluate("() => window.ed.querySelector('[data-service=\"demo-shop\"]').click()")
    page.evaluate("() => window.ed.querySelector('[data-name=\"show_chart\"]').click()")
    assert page.evaluate("window.changes") == [
        {"type": "custom:render-monitor-card", "services": ["demo-shop"]},
        {"type": "custom:render-monitor-card", "services": ["demo-shop"], "show_chart": False},
    ]
    page.evaluate("() => window.ed.querySelector('[data-service=\"demo-shop\"]').click()")
    assert page.evaluate("window.changes.at(-1)") == {"type": "custom:render-monitor-card", "show_chart": False}
