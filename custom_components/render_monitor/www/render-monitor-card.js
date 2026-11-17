/*
 * Render Monitor card: one block per Render service, read from the render_monitor entities.
 * Status, plan and monthly price, memory/CPU over 24 h, last deploy, deploys and restarts today,
 * and the integration's advice with a short explanation of each term.
 *
 * Nothing here talks to Render: the card only reads Home Assistant states, and fetches the
 * advice texts that the integration serves next to this file.
 */
(() => {
  "use strict";

  const VERSION = "0.1.0";
  const DOMAIN = "render_monitor";
  const TEXTS_URL = "/render_monitor_static/advice_texts.json";
  const LANG = "en"; // the card is always in English, whatever the Home Assistant language

  // ------------------------------------------------------------------ texts (English only)
  const TEXTS = {
    title: "Render",
    empty: "No Render services were found. Please add the Render Monitor integration or review the service list in the card configuration.",
    status_live: "Live", status_deploying: "Deploying", status_deploy_failed: "Deploy failed",
    status_suspended: "Suspended", status_not_deployed: "Not deployed", status_unavailable: "Unavailable",
    type_web_service: "Web service", type_static_site: "Static site", type_background_worker: "Background worker",
    type_private_service: "Private service", type_cron_job: "Cron job",
    per_month: "{price}/month", free: "Free", price_unknown: "Price unknown",
    memory: "Memory", cpu: "CPU", peak: "peak {value}", avg: "average {value}",
    p95: "p95", slow: "Slow/h", disk: "Disk",
    deploys_today: "Deploys today", restarts_today: "Restarts today", failed_count: "{count} failed",
    last_deploy: "Last deploy", no_deploy: "No deploys yet",
    deploy_live: "live", deploy_failed: "failed", deploy_in_progress: "in progress",
    deploy_canceled: "canceled", deploy_deactivated: "replaced",
    chart_title: "Last 24 hours", chart_aria: "Memory and CPU usage over the last 24 hours; peak memory {memory}, peak CPU {cpu}",
    limit_line: "80%",
    advice: "Advice", no_advice: "No action is required.",
    read_errors: "Some data could not be read ({count}).",
    prices_note: "Prices are as of {date}; please verify them at render.com/pricing.",
    open_dashboard: "Open in Render",
    about: "What is {term}?",
    sev_critical: "Critical", sev_warning: "Warning", sev_info: "Information",
    ed_title: "Title", ed_services: "Services (if none are selected, all services are shown)",
    ed_show_chart: "Show the memory and CPU chart",
    ed_show_advice: "Show advice",
    term_memory_limit: "memory limit",
    tip_memory_limit: "The amount of RAM provided by the instance type. When the service requires more, Render stops the service and starts it again.",
    term_oom: "out of memory (OOM)",
    tip_oom: "The service used more RAM than its instance type provides, so Render stopped it and restarted it. Requests fail while the service restarts.",
    term_instance_type: "instance type",
    tip_instance_type: "The size of the machine on which Render runs the service (CPU and RAM). Each type has a fixed monthly price per instance; a larger type provides more RAM.",
    term_cpu_limit: "CPU %",
    tip_cpu_limit: "The share of the instance type's CPU that is in use. At 100%, all of the CPU is in use and the service slows down.",
    term_p95: "p95",
    tip_p95: "The response time within which 95 out of every 100 requests were answered; the slowest 5 took longer. A sharp increase indicates that users experienced delays.",
    term_slow_request: "slow request",
    tip_slow_request: "A log line beginning with \"slow:\" that the service writes when a request takes a long time. These lines are counted per hour.",
    term_import: "import",
    tip_import: "Log lines that contain the word \"import\", such as the loading of a large file. Large imports consume memory and slow down other requests.",
    term_deploy: "deploy",
    tip_deploy: "The process of building and starting a new version of the service, usually after a commit.",
    term_commit: "commit",
    tip_commit: "The saved change in git from which the deploy was built. It is identified by its first 7 characters.",
    term_zero_downtime: "zero-downtime deploy",
    tip_zero_downtime: "Render starts the new version before it stops the previous one. A service with a disk cannot be deployed this way, so every deploy stops the service for a period of time.",
    term_disk: "disk",
    tip_disk: "Storage that is retained between deploys and billed per GB each month. Only one instance can use it.",
    term_build_filters: "build filters",
    tip_build_filters: "A Render setting that lists the paths whose changes trigger a deploy. With build filters, changes to documentation or tests do not trigger a deploy.",
    term_restart: "restart",
    tip_restart: "The service was started again, for example after a deploy, after a crash, or after Render relocated it.",
    term_suspended: "suspended",
    tip_suspended: "The service is stopped and does not respond to requests. Render does not charge for instance hours while the service is suspended.",
    term_idle: "idle",
    tip_idle: "The service received no requests in the last 24 hours but is still billed.",
    term_monthly_cost: "monthly cost",
    tip_monthly_cost: "Instances x the price of the instance type, plus disk GB x $0.25, based on a fixed price table. While a service is suspended, only its disk is counted. Please verify the prices at render.com/pricing before acting on them.",
  };

  // which terms explain each advice text
  const ADVICE_TERMS = [
    [/^memory_oom/, ["oom", "instance_type", "memory_limit"]],
    [/^memory_high/, ["memory_limit", "instance_type"]],
    [/^deploy_failed/, ["deploy", "commit"]],
    [/^many_deploys/, ["zero_downtime", "disk", "build_filters"]],
    [/^latency_spike/, ["p95"]],
    [/^slow_spike/, ["slow_request"]],
    [/^disk_high/, ["disk"]],
    [/^suspended/, ["suspended", "monthly_cost"]],
    [/^idle/, ["idle"]],
  ];
  const TERMS = ["memory_limit", "oom", "instance_type", "cpu_limit", "p95", "slow_request", "import", "deploy",
    "commit", "zero_downtime", "disk", "build_filters", "restart", "suspended", "idle", "monthly_cost"];
  const STATUS_COLOR = {
    live: "var(--success-color, #43a047)",
    deploying: "var(--info-color, #039be5)",
    deploy_failed: "var(--error-color, #db4437)",
    suspended: "var(--warning-color, #ffa600)",
    not_deployed: "var(--secondary-text-color, #727272)",
    unavailable: "var(--disabled-text-color, #bdbdbd)",
  };
  const SEV_COLOR = {
    critical: "var(--error-color, #db4437)",
    warning: "var(--warning-color, #ffa600)",
    info: "var(--info-color, #039be5)",
  };
  const DEPLOY_STATE = {
    live: "deploy_live", build_failed: "deploy_failed", update_failed: "deploy_failed",
    pre_deploy_failed: "deploy_failed", canceled: "deploy_canceled", deactivated: "deploy_deactivated",
    created: "deploy_in_progress", queued: "deploy_in_progress", build_in_progress: "deploy_in_progress",
    update_in_progress: "deploy_in_progress", pre_deploy_in_progress: "deploy_in_progress",
  };

  // ------------------------------------------------------------------ helpers
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fill = (template, values) => String(template).replace(/\{(\w+)\}/g,
    (_, key) => (values[key] === undefined || values[key] === null ? "-" : String(values[key])));

  /** Same output as the integration's advice.render(): {placeholders}, "-" when missing, reason codes as text. */
  function renderAdvice(texts, item) {
    const template = texts[item.text_key];
    if (!template) return null;
    const values = {};
    for (const [key, value] of Object.entries(item.values || {})) {
      values[key] = value === null || value === undefined ? "-" : value;
    }
    if (values.reason !== undefined && values.reason !== "-") {
      values.reason = texts[`reason_${values.reason}`] || values.reason;
    }
    return fill(template, values);
  }

  const capital = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

  const t = (key, values) => {
    const text = TEXTS[key] !== undefined ? TEXTS[key] : key;
    return values ? fill(text, values) : text;
  };

  function numberText(hass, value, digits = 0, minDigits = 0) {
    const n = Number(value);
    if (value === null || value === undefined || value === "" || !Number.isFinite(n)) return "-";
    try {
      return new Intl.NumberFormat(LANG, { maximumFractionDigits: digits, minimumFractionDigits: minDigits }).format(n);
    } catch (err) {
      return n.toFixed(digits);
    }
  }

  function usd(hass, value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return null;
    return Number.isInteger(n) ? `$${numberText(hass, n)}` : `$${numberText(hass, n, 2, 2)}`;
  }

  function relativeTime(iso) {
    const moment = Date.parse(iso);
    if (!Number.isFinite(moment)) return null;
    const seconds = Math.round((moment - Date.now()) / 1000);
    const steps = [[60, "second"], [3600, "minute"], [86400, "hour"], [86400 * 30, "day"], [Infinity, "month"]];
    const sizes = { second: 1, minute: 60, hour: 3600, day: 86400, month: 86400 * 30 };
    const unit = steps.find(([limit]) => Math.abs(seconds) < limit)[1];
    try {
      return new Intl.RelativeTimeFormat(LANG, { numeric: "auto" }).format(Math.round(seconds / sizes[unit]), unit);
    } catch (err) {
      return new Date(moment).toISOString();
    }
  }

  function absoluteTime(hass, iso) {
    const moment = Date.parse(iso);
    if (!Number.isFinite(moment)) return "";
    const options = { dateStyle: "medium", timeStyle: "short" };
    const zone = hass && hass.config && hass.config.time_zone;
    if (zone && !(hass.locale && hass.locale.time_zone === "local")) options.timeZone = zone;
    try {
      return new Intl.DateTimeFormat(LANG, options).format(new Date(moment));
    } catch (err) {
      return new Date(moment).toISOString();
    }
  }

  let adviceTexts = null; // {key: text} once loaded; false when it could not be loaded
  let adviceTextsPromise = null;
  function loadAdviceTexts() {
    if (!adviceTextsPromise) {
      adviceTextsPromise = fetch(TEXTS_URL, { cache: "no-cache", credentials: "same-origin" })
        .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
        .then((json) => { adviceTexts = json && typeof json === "object" ? json : false; })
        .catch(() => { adviceTexts = false; });
    }
    return adviceTextsPromise;
  }

  /** The render_monitor entities grouped by device (= Render service), keyed by translation_key. */
  function findServices(hass) {
    const out = new Map();
    const entities = (hass && hass.entities) || {};
    for (const entry of Object.values(entities)) {
      if (!entry || entry.platform !== DOMAIN || !entry.device_id || !entry.translation_key) continue;
      if (!out.has(entry.device_id)) {
        const device = (hass.devices && hass.devices[entry.device_id]) || {};
        out.set(entry.device_id, {
          id: entry.device_id,
          name: device.name_by_user || device.name || entry.device_id,
          model: device.model,
          url: device.configuration_url,
          ids: {},
        });
      }
      out.get(entry.device_id).ids[entry.translation_key] = entry.entity_id;
    }
    return [...out.values()].sort((a, b) => a.name.localeCompare(b.name));
  }

  function pickServices(hass, config) {
    const all = findServices(hass);
    const wanted = (config && Array.isArray(config.services)) ? config.services.filter(Boolean) : [];
    if (!wanted.length) return all;
    const byKey = new Map();
    for (const service of all) { byKey.set(service.name, service); byKey.set(service.id, service); }
    return wanted.map((key) => byKey.get(key)).filter(Boolean);
  }

  // ------------------------------------------------------------------ chart
  function chart(memory, cpu, label) {
    const series = [memory, cpu].map((points) => (Array.isArray(points) ? points : [])
      .map(([t, v]) => [Date.parse(t), Number(v)])
      .filter(([t, v]) => Number.isFinite(t) && Number.isFinite(v)));
    const all = series.flat();
    if (!all.length) return "";
    const end = Math.max(...all.map(([t]) => t)) + 30 * 60 * 1000;
    const start = end - 24 * 3600 * 1000;
    const W = 240, H = 56;
    const x = (t) => Math.max(0, Math.min(W, ((t - start) / (end - start)) * W));
    const y = (v) => H - (Math.max(0, Math.min(100, v)) / 100) * H;
    const line = (points) => points.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">
      <line class="limit" x1="0" x2="${W}" y1="${y(80)}" y2="${y(80)}"></line>
      ${series[1].length ? `<polyline class="cpu" points="${line(series[1])}"></polyline>` : ""}
      ${series[0].length ? `<polyline class="mem" points="${line(series[0])}"></polyline>` : ""}
    </svg>`;
  }

  // ------------------------------------------------------------------ card
  const STYLE = `
    :host { display: block; }
    ha-card { display: block; box-sizing: border-box; padding: 12px 16px 16px; color: var(--primary-text-color); background: var(--ha-card-background, var(--card-background-color, #fff)); }
    .head { display: flex; align-items: center; gap: 8px; font-size: 1.15em; font-weight: 500; margin: 2px 0 8px; }
    .svc { border-top: 1px solid var(--divider-color, rgba(127,127,127,.25)); padding: 12px 0 4px; }
    .svc:first-of-type { border-top: 0; }
    .svc.attention { border-inline-start: 3px solid var(--warning-color, #ffa600); padding-inline-start: 10px; }
    .top { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }
    .name { font-weight: 600; font-size: 1.02em; overflow-wrap: anywhere; }
    .type { color: var(--secondary-text-color); font-size: .85em; }
    .chip { border: 0; font: inherit; font-size: .8em; line-height: 1.6; padding: 0 10px; border-radius: 12px; cursor: pointer;
            color: var(--primary-text-color); background: color-mix(in srgb, var(--chip) 22%, transparent);
            box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--chip) 60%, transparent); }
    .chip::before { content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--chip); margin-inline-end: 6px; vertical-align: 1px; }
    .price { margin-inline-start: auto; color: var(--secondary-text-color); font-size: .9em; white-space: nowrap; }
    .link { color: var(--primary-color); font-size: .85em; text-decoration: none; }
    .link:hover { text-decoration: underline; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(92px, 1fr)); gap: 6px; margin-top: 10px; }
    .stat { background: var(--secondary-background-color, rgba(127,127,127,.08)); border-radius: 8px; padding: 6px 8px; min-width: 0;
            border: 0; font: inherit; color: inherit; text-align: start; cursor: pointer; }
    .stat .k { color: var(--secondary-text-color); font-size: .75em; display: flex; align-items: center; gap: 4px; }
    .stat .v { font-size: 1.05em; font-weight: 500; overflow-wrap: anywhere; }
    .stat .s { color: var(--secondary-text-color); font-size: .75em; }
    .chartbox { margin-top: 10px; }
    .chartbox .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: .78em; color: var(--secondary-text-color); margin-bottom: 2px; }
    .legend i { display: inline-block; width: 10px; height: 3px; border-radius: 2px; vertical-align: middle; margin-inline-end: 4px; }
    .chart { width: 100%; height: 56px; display: block; direction: ltr; }
    .chart polyline { fill: none; stroke-width: 1.6; vector-effect: non-scaling-stroke; stroke-linejoin: round; }
    .chart .mem, .legend .mem { stroke: var(--primary-color, #03a9f4); background: var(--primary-color, #03a9f4); }
    .chart .cpu, .legend .cpu { stroke: var(--accent-color, #ff9800); background: var(--accent-color, #ff9800); }
    .chart .limit { stroke: var(--secondary-text-color); stroke-dasharray: 3 3; stroke-width: 1; opacity: .6; vector-effect: non-scaling-stroke; }
    .deploy { margin-top: 10px; font-size: .9em; overflow-wrap: anywhere; }
    .deploy .k { color: var(--secondary-text-color); }
    .deploy code { font-family: var(--code-font-family, monospace); font-size: .9em; }
    .deploy .bad { color: var(--error-color, #db4437); font-weight: 500; }
    .errors { margin-top: 6px; font-size: .8em; color: var(--warning-color, #ffa600); }
    .advice { margin-top: 10px; }
    .advice h4 { margin: 0 0 4px; font-size: .9em; font-weight: 500; color: var(--secondary-text-color); }
    .advice ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
    .advice li { border-inline-start: 3px solid var(--sev); padding: 4px 8px; border-radius: 4px;
                 background: color-mix(in srgb, var(--sev) 9%, transparent); font-size: .9em; line-height: 1.4; overflow-wrap: anywhere; }
    .advice .sev { font-size: .72em; font-weight: 600; text-transform: uppercase; letter-spacing: .03em; color: var(--sev); margin-inline-end: 6px; }
    .none { color: var(--secondary-text-color); font-size: .88em; }
    .terms { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
    .tip { border: 1px solid var(--divider-color, rgba(127,127,127,.4)); background: transparent; color: var(--secondary-text-color);
           font: inherit; font-size: .75em; border-radius: 10px; padding: 0 7px; line-height: 1.6; cursor: pointer; }
    .tip[aria-expanded="true"] { color: var(--primary-text-color); border-color: var(--primary-color); }
    .tip.i { width: 16px; height: 16px; padding: 0; line-height: 14px; border-radius: 50%; font-size: .7em; font-weight: 600; }
    .note { margin-top: 4px; font-size: .82em; color: var(--primary-text-color); background: var(--secondary-background-color, rgba(127,127,127,.08));
            border-radius: 6px; padding: 6px 8px; }
    .note b { font-weight: 600; }
    .foot { margin-top: 10px; color: var(--secondary-text-color); font-size: .75em; }
    .empty { color: var(--secondary-text-color); padding: 8px 0; }
    button:focus-visible, a:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  `;

  class RenderMonitorCard extends HTMLElement {
    static get TEXTS() { return TEXTS; }
    static get TERMS() { return TERMS; }
    static get version() { return VERSION; }

    static getStubConfig() { return { type: "custom:render-monitor-card" }; }

    static getConfigElement() { return document.createElement("render-monitor-card-editor"); }

    constructor() {
      super();
      this._open = new Set();
      this._sig = null;
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
    }

    setConfig(config) {
      if (!config || typeof config !== "object") throw new Error("Invalid configuration");
      if (config.services !== undefined && !Array.isArray(config.services)) {
        throw new Error("services must be a list of service names");
      }
      this._config = { show_chart: true, show_advice: true, ...config };
      this._sig = null;
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      if (adviceTexts === null) loadAdviceTexts().then(() => { this._sig = null; this._render(); });
      this._render();
    }

    get hass() { return this._hass; }

    getCardSize() {
      const count = this._hass ? pickServices(this._hass, this._config).length : 1;
      return 1 + Math.max(1, count) * 5;
    }

    getGridOptions() { return { columns: 12, min_columns: 6 }; }

    _t(key, values) { return t(key, values); }

    /** Re-render only when something the card shows has changed (keeps open tips and focus). */
    _signature(services) {
      const hass = this._hass;
      const parts = [adviceTexts === null ? 0 : adviceTexts ? 1 : 2, JSON.stringify(this._config),
        hass.config && hass.config.time_zone];
      for (const service of services) {
        parts.push(service.id, service.name, service.url);
        for (const [key, id] of Object.entries(service.ids)) {
          const state = hass.states[id];
          parts.push(key, state ? `${state.state}|${state.last_updated || ""}|${JSON.stringify(state.attributes)}` : "-");
        }
      }
      return parts.join("\u0001");
    }

    _render() {
      if (!this._config || !this._hass) return;
      const services = pickServices(this._hass, this._config);
      const sig = this._signature(services);
      if (sig === this._sig) return;
      this._sig = sig;
      const title = this._config.title === undefined ? this._t("title") : this._config.title;
      const body = services.length
        ? services.map((service) => this._service(service)).join("")
        : `<div class="empty">${esc(this._t("empty"))}</div>`;
      this.shadowRoot.innerHTML = `<style>${STYLE}</style>
        <ha-card dir="ltr" lang="${LANG}">
          ${title ? `<div class="head">${esc(title)}</div>` : ""}
          ${body}
          ${services.length ? `<div class="foot">${esc(this._t("prices_note", { date: this._pricesDate(services) }))}</div>` : ""}
        </ha-card>`;
    }

    _pricesDate(services) {
      for (const service of services) {
        const state = this._state(service, "monthly_cost");
        if (state && state.attributes.prices_date) return state.attributes.prices_date;
      }
      return "-";
    }

    _state(service, key) {
      const id = service.ids[key];
      return id ? this._hass.states[id] : undefined;
    }

    _value(service, key) {
      const state = this._state(service, key);
      if (!state || state.state === "unavailable" || state.state === "unknown") return null;
      return state.state;
    }

    _tip(id, term, round = false) {
      const open = this._open.has(id);
      const label = this._t(`term_${term}`);
      return `<button class="tip${round ? " i" : ""}" data-tip="${esc(id)}" data-term="${esc(term)}" aria-expanded="${open}"
        aria-label="${esc(this._t("about", { term: label }))}" title="${esc(this._t("about", { term: label }))}">${round ? "i" : esc(label)}</button>`;
    }

    _notes(prefix, terms) {
      return terms.filter((term) => this._open.has(`${prefix}:${term}`)).map((term) =>
        `<div class="note" role="note" data-note="${esc(`${prefix}:${term}`)}"><b>${esc(capital(this._t(`term_${term}`)))}</b>: ${esc(this._t(`tip_${term}`))}</div>`).join("");
    }

    _service(service) {
      const hass = this._hass;
      const status = this._state(service, "status");
      const statusValue = status && STATUS_COLOR[status.state] ? status.state : "unavailable";
      const attrs = (status && status.attributes) || {};
      const type = attrs.service_type || service.model;
      const attention = this._value(service, "needs_attention") === "on";

      // price
      const cost = this._state(service, "monthly_cost");
      const costValue = this._value(service, "monthly_cost");
      const plan = (cost && cost.attributes.plan) || attrs.plan;
      let price;
      if (costValue === null) price = this._t("price_unknown");
      // "Free" for a free type or a static site; a suspended paid service shows $0/month
      else if (Number(costValue) === 0 && (!plan || plan === "free" || type === "static_site")) price = this._t("free");
      else price = this._t("per_month", { price: usd(hass, costValue) });
      const priceText = [plan, price].filter(Boolean).join(" · ");

      // stats
      const stats = [];
      const statTerms = [];
      const pct = (v) => (v === null ? "-" : `${numberText(hass, v)}%`);
      if (service.ids.memory_peak) {
        const mem = this._state(service, "memory_peak");
        const a = (mem && mem.attributes) || {};
        const sub = a.peak_mb != null && a.limit_mb != null ? `${numberText(hass, a.peak_mb)} / ${numberText(hass, a.limit_mb)} MB` : "";
        stats.push(this._statHtml(service, "memory_peak", this._t("memory"), this._t("peak", { value: pct(this._value(service, "memory_peak")) }), sub));
        statTerms.push("memory_limit");
      }
      if (service.ids.cpu_avg) {
        stats.push(this._statHtml(service, "cpu_avg", this._t("cpu"), this._t("avg", { value: pct(this._value(service, "cpu_avg")) }),
          this._t("peak", { value: pct(this._value(service, "cpu_peak")) })));
        statTerms.push("cpu_limit");
      }
      if (service.ids.latency_p95) {
        const lat = this._state(service, "latency_p95");
        const v = this._value(service, "latency_p95");
        const peak = lat && lat.attributes.peak_ms;
        stats.push(this._statHtml(service, "latency_p95", this._t("p95"), v === null ? "-" : `${numberText(hass, v)} ms`,
          peak != null ? this._t("peak", { value: `${numberText(hass, peak)} ms` }) : ""));
        statTerms.push("p95");
      }
      if (service.ids.slow_requests) {
        const slow = this._state(service, "slow_requests");
        const total = slow && slow.attributes.total_24h;
        stats.push(this._statHtml(service, "slow_requests", this._t("slow"), numberText(hass, this._value(service, "slow_requests")),
          total != null ? `24h: ${numberText(hass, total)}${slow.attributes.truncated ? "+" : ""}` : ""));
        statTerms.push("slow_request");
      }
      if (service.ids.disk_used) {
        const disk = this._state(service, "disk_used");
        const a = (disk && disk.attributes) || {};
        stats.push(this._statHtml(service, "disk_used", this._t("disk"), pct(this._value(service, "disk_used")),
          a.size_gb != null ? `${numberText(hass, a.used_gb, 1)} / ${numberText(hass, a.size_gb, 1)} GB` : ""));
        statTerms.push("disk");
      }
      const deploys = this._state(service, "deploys_today");
      const failedToday = deploys && deploys.attributes.failed_today;
      stats.push(this._statHtml(service, "deploys_today", this._t("deploys_today"), numberText(hass, this._value(service, "deploys_today")),
        failedToday ? this._t("failed_count", { count: numberText(hass, failedToday) }) : ""));
      statTerms.push("deploy");
      if (service.ids.restarts_today) {
        stats.push(this._statHtml(service, "restarts_today", this._t("restarts_today"), numberText(hass, this._value(service, "restarts_today")), ""));
        statTerms.push("restart");
      }

      // chart
      let chartHtml = "";
      if (this._config.show_chart !== false && (service.ids.memory_peak || service.ids.cpu_avg)) {
        const mem = this._state(service, "memory_peak");
        const cpu = this._state(service, "cpu_avg");
        const svg = chart(mem && mem.attributes.series, cpu && cpu.attributes.series,
          this._t("chart_aria", { memory: pct(this._value(service, "memory_peak")), cpu: pct(this._value(service, "cpu_peak")) }));
        if (svg) {
          chartHtml = `<div class="chartbox"><div class="legend"><span>${esc(this._t("chart_title"))}</span>
            <span><i class="mem"></i>${esc(this._t("memory"))}</span><span><i class="cpu"></i>${esc(this._t("cpu"))}</span>
            <span>- - ${esc(this._t("limit_line"))}</span></div>${svg}</div>`;
        }
      }

      // last deploy
      const last = this._state(service, "last_deploy");
      let deployHtml = `<div class="deploy"><span class="k">${esc(this._t("last_deploy"))}:</span> `;
      if (!last || !last.attributes.status) {
        deployHtml += esc(this._t("no_deploy"));
      } else {
        const a = last.attributes;
        const stateKey = DEPLOY_STATE[a.status];
        const failed = stateKey === "deploy_failed";
        const when = a.finished_at || a.created_at;
        deployHtml += `<span class="${failed ? "bad" : ""}">${esc(stateKey ? this._t(stateKey) : a.status)}</span>`;
        if (when) deployHtml += ` · <time datetime="${esc(when)}" title="${esc(absoluteTime(hass, when))}">${esc(relativeTime(when) || "")}</time>`;
        if (a.commit) deployHtml += ` · <code>${esc(a.commit)}</code>`;
        if (a.message) deployHtml += ` <bdi class="msg">${esc(a.message)}</bdi>`;
      }
      deployHtml += "</div>";

      // errors reading this service
      const errors = Array.isArray(attrs.errors) ? attrs.errors : [];
      const errorsHtml = errors.length
        ? `<div class="errors" title="${esc(errors.join("\n"))}">${esc(this._t("read_errors", { count: errors.length }))}</div>` : "";

      // advice
      let adviceHtml = "";
      if (this._config.show_advice !== false && service.ids.advice) {
        const adv = this._state(service, "advice");
        const items = (adv && Array.isArray(adv.attributes.advice)) ? adv.attributes.advice : [];
        const fallback = (adv && Array.isArray(adv.attributes.texts)) ? adv.attributes.texts : [];
        const table = adviceTexts || {};
        const list = items.map((item, index) => {
          const text = renderAdvice(table, item) || fallback[index] || item.text_key;
          const severity = SEV_COLOR[item.severity] ? item.severity : "info";
          const terms = this._adviceTerms(item);
          const prefix = `${service.id}:advice:${index}`;
          return `<li style="--sev:${SEV_COLOR[severity]}" data-code="${esc(item.code)}">
              <span class="sev">${esc(this._t(`sev_${severity}`))}</span><span class="text">${esc(text)}</span>
              ${terms.length ? `<div class="terms">${terms.map((term) => this._tip(`${prefix}:${term}`, term)).join("")}</div>` : ""}
              ${this._notes(prefix, terms)}
            </li>`;
        }).join("");
        adviceHtml = `<div class="advice"><h4>${esc(this._t("advice"))}</h4>
          ${list ? `<ul>${list}</ul>` : `<div class="none">${esc(this._t("no_advice"))}</div>`}</div>`;
      }

      const statPrefix = `${service.id}:stat`;
      const link = service.url && /^https:\/\//.test(service.url)
        ? `<a class="link" href="${esc(service.url)}" target="_blank" rel="noopener noreferrer">${esc(this._t("open_dashboard"))}</a>` : "";
      return `<section class="svc${attention ? " attention" : ""}" data-service="${esc(service.name)}">
        <div class="top">
          <span class="name">${esc(service.name)}</span>
          ${type ? `<span class="type">${esc(this._t(`type_${type}`) === `type_${type}` ? type : this._t(`type_${type}`))}</span>` : ""}
          <button class="chip" style="--chip:${STATUS_COLOR[statusValue]}" data-entity="${esc(service.ids.status || "")}" data-status="${statusValue}">${esc(this._t(`status_${statusValue}`))}</button>
          <span class="price"><span class="ptext"><bdi>${esc(priceText)}</bdi></span> ${this._tip(`${service.id}:price:monthly_cost`, "monthly_cost", true)}</span>
          ${link}
        </div>
        ${this._notes(`${service.id}:price`, ["monthly_cost"])}
        <div class="grid">${stats.join("")}</div>
        <div class="terms">${statTerms.map((term) => this._tip(`${statPrefix}:${term}`, term)).join("")}</div>
        ${this._notes(statPrefix, statTerms)}
        ${chartHtml}
        ${deployHtml}
        ${errorsHtml}
        ${adviceHtml}
      </section>`;
    }

    _statHtml(service, key, label, value, sub) {
      const id = service.ids[key];
      return `<button class="stat" data-entity="${esc(id || "")}" ${id ? "" : "disabled"}>
          <div class="k">${esc(label)}</div>
          <div class="v"><bdi>${esc(value)}</bdi></div>
          ${sub ? `<div class="s"><bdi>${esc(sub)}</bdi></div>` : ""}
        </button>`;
    }

    _adviceTerms(item) {
      const key = String(item.text_key || "");
      const terms = [];
      for (const [pattern, list] of ADVICE_TERMS) if (pattern.test(key)) terms.push(...list);
      if (/import/.test(key)) terms.push("import");
      if (/deploy$/.test(key) && !terms.includes("deploy")) terms.push("deploy");
      return terms;
    }

    _onClick(ev) {
      const tip = ev.target.closest("[data-tip]");
      if (tip) {
        const id = tip.getAttribute("data-tip");
        if (this._open.has(id)) this._open.delete(id); else this._open.add(id);
        this._sig = null;
        this._render();
        const again = this.shadowRoot.querySelector(`[data-tip="${CSS.escape(id)}"]`);
        if (again) again.focus();
        return;
      }
      const target = ev.target.closest("[data-entity]");
      const entityId = target && target.getAttribute("data-entity");
      if (entityId) {
        this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
      }
    }
  }

  // ------------------------------------------------------------------ editor
  class RenderMonitorCardEditor extends HTMLElement {
    setConfig(config) {
      this._config = { ...config };
      this._render();
    }

    set hass(hass) {
      const first = !this._hass;
      this._hass = hass;
      if (this._form) this._form.hass = hass;
      if (first) this._render();
    }

    _t(key) { return t(key); }

    _changed(config) {
      const clean = { ...config };
      if (Array.isArray(clean.services) && !clean.services.length) delete clean.services;
      if (clean.title === "") delete clean.title;
      if (clean.show_chart === true) delete clean.show_chart;
      if (clean.show_advice === true) delete clean.show_advice;
      this._config = clean;
      this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: clean }, bubbles: true, composed: true }));
    }

    _render() {
      if (!this._config || !this._hass) return;
      const names = findServices(this._hass).map((service) => service.name);
      const data = {
        title: this._config.title || "",
        services: Array.isArray(this._config.services) ? this._config.services : [],
        show_chart: this._config.show_chart !== false,
        show_advice: this._config.show_advice !== false,
      };
      if (customElements.get("ha-form")) {
        if (!this._form) {
          this._form = document.createElement("ha-form");
          this._form.computeLabel = (schema) => this._t(`ed_${schema.name}`);
          this._form.addEventListener("value-changed", (ev) => this._changed({ ...this._config, ...ev.detail.value }));
          this.replaceChildren(this._form);
        }
        this._form.hass = this._hass;
        this._form.schema = [
          { name: "title", selector: { text: {} } },
          { name: "services", selector: { select: { multiple: true, mode: "list", options: names.map((n) => ({ value: n, label: n })) } } },
          { name: "show_chart", selector: { boolean: {} } },
          { name: "show_advice", selector: { boolean: {} } },
        ];
        this._form.data = data;
        return;
      }
      // plain fallback (no ha-form loaded yet)
      const box = (name, checked, label) => `<label style="display:block;margin:4px 0">
        <input type="checkbox" data-name="${esc(name)}" ${checked ? "checked" : ""}> ${esc(label)}</label>`;
      this.innerHTML = `<div class="rm-editor">
        <label style="display:block;margin:4px 0">${esc(this._t("ed_title"))}
          <input type="text" data-name="title" value="${esc(data.title)}"></label>
        <fieldset><legend>${esc(this._t("ed_services"))}</legend>
          ${names.map((n) => `<label style="display:block"><input type="checkbox" data-service="${esc(n)}" ${data.services.includes(n) ? "checked" : ""}> ${esc(n)}</label>`).join("")}
        </fieldset>
        ${box("show_chart", data.show_chart, this._t("ed_show_chart"))}
        ${box("show_advice", data.show_advice, this._t("ed_show_advice"))}
      </div>`;
      this.querySelectorAll("input").forEach((input) => input.addEventListener("change", () => {
        const config = { ...this._config };
        config.title = this.querySelector('[data-name="title"]').value;
        config.services = [...this.querySelectorAll("[data-service]")].filter((i) => i.checked).map((i) => i.getAttribute("data-service"));
        config.show_chart = this.querySelector('[data-name="show_chart"]').checked;
        config.show_advice = this.querySelector('[data-name="show_advice"]').checked;
        this._changed(config);
      }));
    }
  }

  if (!customElements.get("render-monitor-card")) customElements.define("render-monitor-card", RenderMonitorCard);
  if (!customElements.get("render-monitor-card-editor")) customElements.define("render-monitor-card-editor", RenderMonitorCardEditor);
  window.customCards = window.customCards || [];
  if (!window.customCards.some((card) => card.type === "render-monitor-card")) {
    window.customCards.push({
      type: "render-monitor-card",
      name: "Render Monitor",
      description: "Render services: status, cost, memory/CPU, deploys and advice.",
      preview: true,
      documentationURL: "https://github.com/niruse/render-monitor",
    });
  }
  // test hook: the same advice renderer the card uses
  RenderMonitorCard.renderAdvice = renderAdvice;
})();
