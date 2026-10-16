// Investing views: portfolio panel, trending picks, long-term plans, quotes,
// order cards and the investor-profile questions. Every figure shown comes
// from the gateway; suggestions are rule-based and always list their risks.

import {formatFundingSource, formatTransactionalMoney} from "./transaction_display.mjs";

const pct = value => `${value >= 0 ? "+" : ""}${Number(value).toFixed(1)}%`;
const riskDots = level => "●".repeat(level) + "○".repeat(5 - level);
const marketMoney = (currency, value) => {
  const symbol = currency === "USD" ? "US$" : currency === "SGD" ? "S$" : `${currency} `;
  return `${symbol}${Number(value).toLocaleString("en-SG", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
};

const wealthFunds = [
  {
    symbol: "ES3", name: "State Street SPDR Straits Times Index ETF", kind: "ETF", sector: "Singapore equities",
    factLabel: "Tracks", fact: "Straits Times Index", summary: "SGX-listed exposure to the 30 large companies in Singapore’s Straits Times Index.",
    sourceName: "State Street Global Advisors", sourceUrl: "https://www.ssga.com/sg/en/individual/etfs/state-street-spdr-straits-times-index-etf-es3",
  },
  {
    symbol: "VOO", name: "Vanguard S&P 500 ETF", kind: "ETF", sector: "U.S. large-cap equities",
    factLabel: "Tracks", fact: "S&P 500 Index", summary: "Broad exposure to large U.S. companies through the S&P 500 Index.",
    sourceName: "Vanguard", sourceUrl: "https://advisors.vanguard.com/investments/products/voo/vanguard-sp-500-etf",
  },
  {
    symbol: "VXUS", name: "Vanguard Total International Stock ETF", kind: "ETF", sector: "International equities",
    factLabel: "Tracks", fact: "FTSE Global All Cap ex US Index", summary: "Developed and emerging-market stocks outside the United States.",
    sourceName: "Vanguard", sourceUrl: "https://advisors.vanguard.com/investments/products/vxus/vanguard-total-international-stock-etf",
  },
  {
    symbol: "A35", name: "ABF Singapore Bond Index Fund", kind: "BOND_ETF", sector: "SGD bonds",
    factLabel: "Tracks", fact: "iBoxx ABF Singapore Index", summary: "SGX-listed bond index fund focused on SGD-denominated government and government-related debt.",
    sourceName: "Amova Asset Management", sourceUrl: "https://www.nikkoam.com.sg/funds/abf-singapore-bond-index-fund",
  },
  {
    symbol: "BND", name: "Vanguard Total Bond Market ETF", kind: "BOND_ETF", sector: "U.S. investment-grade bonds",
    factLabel: "Tracks", fact: "Bloomberg U.S. Aggregate Float Adjusted Index", summary: "Broad exposure to taxable, investment-grade U.S. dollar bonds.",
    sourceName: "Vanguard", sourceUrl: "https://advisors.vanguard.com/investments/products/bnd/vanguard-total-bond-market-etf",
  },
];

const wealthEquities = [
  {symbol: "AAPL", name: "Apple Inc.", kind: "STOCK", sector: "Technology", factLabel: "Company", fact: "Consumer devices and services", summary: "Technology company behind iPhone, Mac, Apple Watch, and its software and services platforms.", sourceName: "Apple", sourceUrl: "https://investor.apple.com/"},
  {symbol: "MSFT", name: "Microsoft Corporation", kind: "STOCK", sector: "Technology", factLabel: "Company", fact: "Software, cloud, and AI", summary: "Technology company spanning software, cloud services, and AI platforms.", sourceName: "Microsoft", sourceUrl: "https://www.microsoft.com/en-us/Investor/"},
  {symbol: "JNJ", name: "Johnson & Johnson", kind: "STOCK", sector: "Healthcare", factLabel: "Company", fact: "Innovative medicine and MedTech", summary: "Healthcare company focused on medicines and medical technologies.", sourceName: "Johnson & Johnson", sourceUrl: "https://www.jnj.com/our-company"},
  {symbol: "WMT", name: "Walmart Inc.", kind: "STOCK", sector: "Consumer & retail", factLabel: "Company", fact: "Omnichannel retail", summary: "Retailer operating stores, clubs, and e-commerce businesses.", sourceName: "Walmart", sourceUrl: "https://corporate.walmart.com/about"},
  {symbol: "CAT", name: "Caterpillar Inc.", kind: "STOCK", sector: "Manufacturing", factLabel: "Company", fact: "Industrial and construction equipment", summary: "Manufacturer of construction and mining equipment, engines, and industrial power systems.", sourceName: "Caterpillar", sourceUrl: "https://www.cat.com/en_US/company.html"},
];

const wealthSectorClass = sector => `wealth-sector-${String(sector || "Other").toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
const wealthSectorsBySymbol = {
  ...Object.fromEntries(wealthEquities.map(stock => [stock.symbol, stock.sector])),
  NVDA: "Technology",
};
const wealthStartingHoldings = wealthEquities.map(stock => ({
  ...stock,
  cost_funding_minor: 200_000,
}));
// Keep the starter scenario aligned with plan_suggestions() in investing.py.
const WEALTH_STARTER_MONTHLY_MINOR = 30_000;

function normalizeCatalogValue(value) {
  return String(value || "").trim().toLowerCase();
}

function matchesCatalogKind(itemKind, requestedKind) {
  const kind = normalizeCatalogValue(requestedKind);
  if (!kind || kind === "all") return true;
  if (kind.includes("fund") || kind.includes("etf") || kind.includes("bond")) {
    return itemKind !== "STOCK";
  }
  if (kind.includes("equity") || kind.includes("equiti") || kind.includes("stock")) return itemKind === "STOCK";
  return normalizeCatalogValue(itemKind) === kind;
}

export function filterWealthCatalog(items = [], {query = "", kind = "All", sector = ""} = {}) {
  const normalizedQuery = normalizeCatalogValue(query);
  const normalizedSector = normalizeCatalogValue(sector);
  return items.filter(item => {
    const searchable = `${item.name || ""} ${item.symbol || ""}`.toLowerCase();
    const matchesQuery = !normalizedQuery || searchable.includes(normalizedQuery);
    const matchesSector = !normalizedSector || normalizeCatalogValue(item.sector).includes(normalizedSector);
    return matchesQuery && matchesCatalogKind(item.kind, kind) && matchesSector;
  });
}

function catalogKindLabel(item) {
  return item.kind === "BOND_ETF" ? "Bond ETF" : item.kind === "ETF" ? "ETF" : "Equity";
}

export function renderWealthCatalog(items = [], {escapeHtml = value => String(value)} = {}) {
  if (!items.length) {
    return `<div class="wealth-explore-empty" role="status"><strong>No matching instruments</strong><p>Try a different name, symbol, sector, or category.</p></div>`;
  }
  return items.map(item => {
    const askLabel = item.askPrompt || (item.kind === "STOCK" ? `Tell me about ${item.symbol} stock` : `Explain ${item.name}`);
    return `<article class="wealth-explore-row">
      <div class="wealth-explore-row-main"><div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.symbol)} · ${escapeHtml(item.sector)}</span></div><span class="wealth-explore-kind">${escapeHtml(catalogKindLabel(item))}</span></div>
      <details><summary>View details</summary><p>${escapeHtml(item.summary)}</p><dl class="wealth-explore-fact"><div><dt>${escapeHtml(item.factLabel)}</dt><dd>${escapeHtml(item.fact)}</dd></div></dl><div class="wealth-explore-actions"><a class="wealth-source-link" href="${escapeHtml(item.sourceUrl)}" target="_blank" rel="noopener noreferrer">Source: ${escapeHtml(item.sourceName)} <span aria-hidden="true">↗</span></a><button type="button" class="wealth-ask-button" data-say="${escapeHtml(askLabel)}">Ask Dimon</button></div></details>
    </article>`;
  }).join("");
}

export function digiWealthInvestedMinor(overview = {}) {
  return [
    ...wealthStartingHoldings,
    ...(overview.paper_equity?.holdings || []),
  ].reduce((total, holding) => total + Number(holding.cost_funding_minor || 0), 0);
}

export function realStockBriefCard(brief, {escapeHtml}) {
  const instrument = brief.instrument;
  const market = brief.market;
  const currency = instrument.quote_currency;
  const pe = market.trailing_pe == null ? "Unavailable" : `${Number(market.trailing_pe).toFixed(1)}x`;
  const oneYear = `${market.one_year_change_pct >= 0 ? "Up" : "Down"} ${Math.abs(Number(market.one_year_change_pct)).toFixed(1)}%`;
  const fx = brief.fx_estimate;
  const fxTarget = fx?.pair?.split("/")[1];
  const shortfall = Number(brief.funding_shortfall || 0);
  const sizingBasis = Number.isInteger(brief.requested_quantity)
    ? `${brief.requested_quantity} whole share${brief.requested_quantity === 1 ? "" : "s"}`
    : `${marketMoney(brief.allocation_currency, brief.requested_allocation)} allocation`;
  return `<article class="invest-card real-stock-card">
    <header>
      <div><strong>${escapeHtml(instrument.name)}</strong><small>${escapeHtml(instrument.symbol)} · ${escapeHtml(instrument.exchange)} · verified equity</small></div>
      <div class="invest-price"><strong>${escapeHtml(marketMoney(currency, market.price))}</strong><small>${escapeHtml(market.market_state)}</small></div>
    </header>
    <dl class="invest-stats">
      <div><dt>Trailing P/E</dt><dd>${escapeHtml(pe)}</dd></div>
      <div><dt>Past two weeks</dt><dd>${escapeHtml(`${marketMoney(currency, market.two_week_low)} – ${marketMoney(currency, market.two_week_high)}`)}</dd></div>
      <div><dt>From one year ago</dt><dd>${escapeHtml(oneYear)}</dd></div>
    </dl>
    <p class="invest-why">${escapeHtml(brief.allocation_assumption || sizingBasis)}</p>
    <div class="portfolio-summary">
      <span><small>Whole shares</small><strong>${escapeHtml(`${brief.whole_shares} whole shares`)}</strong></span>
      <span><small>Estimated spend</small><strong>${escapeHtml(marketMoney(currency, brief.estimated_spend))}</strong></span>
      <span><small>Cash left</small><strong>${escapeHtml(marketMoney(currency, brief.unallocated_cash))}</strong></span>
    </div>
    ${fx ? `<p class="invest-note">Indicative ${escapeHtml(fx.pair)} ${escapeHtml(Number(fx.rate).toFixed(4))}: about ${escapeHtml(marketMoney(fxTarget, fx.converted_amount))}.</p>` : ""}
    ${shortfall > 0 ? `<p class="invest-fit above">Your ${escapeHtml(brief.funding_account_label)} balance is ${escapeHtml(marketMoney(brief.funding_currency, brief.funding_balance))}; you’re short ${escapeHtml(marketMoney(brief.funding_currency, shortfall))}.</p>` : ""}
    <p class="invest-fit fits">${escapeHtml(brief.next_question)}</p>
  </article>`;
}

export function requestOrderApproval(order, signed, fetchImpl = globalThis.fetch) {
  return fetchImpl(`/api/orders/${order.order_id}/approve`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({receipt_digest: order.order_digest, ...signed}),
  }).then(async response => response.ok
    ? {kind: "RESULT", result: await response.json()}
    : {kind: "UNKNOWN", message: `Gateway returned HTTP ${response.status}`})
    .catch(error => ({kind: "UNKNOWN", message: error instanceof Error ? error.message : String(error)}));
}

function sparkline(history, width = 260, height = 56) {
  const closes = (history || []).map(point => point.close_minor);
  if (closes.length < 2) return "";
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const span = max - min || 1;
  const points = closes.map((value, index) =>
    `${(index / (closes.length - 1) * width).toFixed(1)},${(height - (value - min) / span * (height - 4) - 2).toFixed(1)}`).join(" ");
  const up = closes[closes.length - 1] >= closes[0];
  return `<svg class="spark ${up ? "up" : "down"}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="90-day price chart"><polyline points="${points}"/></svg>`;
}

export function renderInvestPanel(root, overview, {escapeHtml, formatSgd}) {
  if (!root || !overview) return;
  const total = root.querySelector("#invest-total");
  const holdings = root.querySelector("#invest-holdings");
  const marketStocks = overview.paper_equity?.holdings || [];
  const profile = overview.profile;
  total.textContent = overview.holdings.length
    ? `${formatSgd(overview.total_value_minor)} · ${overview.total_gain_minor >= 0 ? "+" : "−"}${formatSgd(Math.abs(overview.total_gain_minor))}${marketStocks.length ? ` · ${marketStocks.length} stock ${marketStocks.length === 1 ? "position" : "positions"}` : ""}`
    : marketStocks.length ? `${marketStocks.length} stock ${marketStocks.length === 1 ? "position" : "positions"} below` : "No holdings yet";
  holdings.innerHTML = [
    ...overview.holdings.map(h => `<div class="holding-row">
        <span><strong>${escapeHtml(h.name)}</strong><small>${escapeHtml(h.quantity)} × ${escapeHtml(formatSgd(h.price_minor))} · ${escapeHtml(h.symbol)}</small></span>
        <span class="holding-value"><strong>${escapeHtml(formatSgd(h.value_minor))}</strong><small class="${h.gain_minor >= 0 ? "gain" : "loss"}">${escapeHtml(pct(h.gain_pct))}</small></span>
      </div>`),
    ...overview.plans.map(p => `<div class="holding-row plan-row">
        <span><strong>${escapeHtml(p.name)}</strong><small>${escapeHtml(formatSgd(p.monthly_minor))} a month · next ${escapeHtml(new Date(p.next_run_at).toLocaleDateString())}</small></span>
        <span class="holding-value"><small>Monthly plan</small></span>
      </div>`),
    ...(marketStocks.length ? ['<p class="paper-stock-section-title">Shares bought</p>'] : []),
    ...marketStocks.map(stock => {
      const averageEntryMinor = stock.quantity ? Math.round(stock.cost_quote_minor / stock.quantity) : 0;
      const quoteCurrency = stock.quote_currency || "USD";
      const formatQuote = (minor) => `${quoteCurrency === "USD" ? "US$" : quoteCurrency === "SGD" ? "S$" : `${quoteCurrency} `}${(Number(minor) / 100).toLocaleString("en-SG", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
      return `<div class="holding-row paper-stock-row">
        <span><strong>${escapeHtml(stock.name)}</strong><small>${escapeHtml(stock.quantity)} ${Number(stock.quantity) === 1 ? "share" : "shares"} · ${escapeHtml(stock.symbol)} · avg entry ${escapeHtml(formatQuote(averageEntryMinor))}</small></span>
        <span class="holding-value"><small>Total entry value</small><strong>${escapeHtml(formatQuote(stock.cost_quote_minor))}</strong></span>
      </div>`;
    }),
  ].join("") || `<p class="invest-empty">Ask “What's trending?” or “Suggest a long-term plan” to get started.</p>`;
  root.querySelector("#invest-profile").textContent = profile
    ? `Risk profile: ${profile.risk_label} (${profile.risk_score} of 5) · ${profile.horizon_years}-year horizon`
    : "No risk profile yet: you'll answer four questions before your first investment.";
}

// Approved share orders still waiting: queued for the market to open, or a
// limit order waiting for its price. Each can be cancelled until it fills.
function stockOrderMoney(currency, minor) {
  const value = (Number(minor || 0) / 100).toLocaleString("en-SG", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  return currency === "USD" ? `US$${value}` : currency === "SGD" ? `S$${value}` : `${currency} ${value}`;
}

export function renderPendingStockOrders(orders, {escapeHtml}) {
  if (!orders.length) return "";
  return `<section class="wealth-pending-orders" aria-label="Orders waiting to fill">
    <p class="paper-stock-section-title">Waiting to fill</p>
    ${orders.map(order => {
      const instrument = order.instrument || {};
      const verb = order.side === "SELL" ? "Sell" : "Buy";
      const shares = `${order.quantity} ${Number(order.quantity) === 1 ? "share" : "shares"}`;
      const when = order.status === "OPEN" && order.limit_price_minor
        ? `${order.side === "SELL" ? "at" : "at or below"} ${stockOrderMoney(instrument.quote_currency, order.limit_price_minor)}`
          + (order.side === "SELL" ? " or better" : "")
        : "when the market opens";
      return `<article class="wealth-holding-row wealth-pending-row">
        <span class="wealth-holding-name"><strong>${escapeHtml(`${verb} ${shares} of ${instrument.name || instrument.symbol}`)}</strong><small>${escapeHtml(`${order.order_type === "LIMIT" ? "Limit" : "Market"} order · fills ${when}`)}</small></span>
        <span class="wealth-holding-value"><button type="button" class="text-button" data-cancel-stock-order="${escapeHtml(order.order_id)}" data-order-digest="${escapeHtml(order.digest)}">Cancel</button></span>
      </article>`;
    }).join("")}
  </section>`;
}

export function renderDigiWealthPage(root, overview, {escapeHtml, formatSgd}) {
  if (!root || !overview) return;

  const holdingsRoot = root.querySelector("#wealth-holdings");
  const totalRoot = root.querySelector("#wealth-invested-total");
  const allocationRoot = root.querySelector("#wealth-allocation-chart");
  const legendRoot = root.querySelector("#wealth-allocation-legend");
  const stocks = [
    ...wealthStartingHoldings,
    ...(overview.paper_equity?.holdings || []).map(stock => ({
      ...stock,
      sector: wealthSectorsBySymbol[stock.symbol] || "Other",
    })),
  ];
  const totalInvested = stocks.reduce((total, stock) => total + Number(stock.cost_funding_minor || 0), 0);

  if (totalRoot) totalRoot.textContent = formatSgd(totalInvested);
  if (allocationRoot && legendRoot) {
    allocationRoot.replaceChildren();
    legendRoot.replaceChildren();
    const sectors = [...stocks.reduce((groups, stock) => {
      const sector = wealthSectorsBySymbol[stock.symbol] || "Other";
      groups.set(sector, (groups.get(sector) || 0) + Number(stock.cost_funding_minor || 0));
      return groups;
    }, new Map())].sort((a, b) => b[1] - a[1]);
    const radius = 48;
    let percentOffset = 0;
    allocationRoot.setAttribute("aria-label", sectors.length
      ? `Portfolio allocation by sector: ${sectors.map(([sector, invested]) => `${sector} ${totalInvested ? (invested / totalInvested * 100).toFixed(1) : 0}%`).join(", ")}`
      : "No investments in the portfolio yet");
    sectors.forEach(([sector, invested], index) => {
      const percent = totalInvested ? invested / totalInvested * 100 : 0;
      const degrees = percent * 3.6;
      const startAngle = percentOffset * 3.6 * Math.PI / 180;
      const endAngle = (percentOffset + percent) * 3.6 * Math.PI / 180;
      const point = angle => ({
        x: 64 + radius * Math.cos(angle),
        y: 64 + radius * Math.sin(angle),
      });
      const start = point(startAngle);
      const end = point(endAngle);
      const segment = document.createElementNS("http://www.w3.org/2000/svg", "path");
      segment.setAttribute("class", `wealth-ring-segment ${wealthSectorClass(sector)}`);
      segment.setAttribute("stroke-linecap", "butt");
      segment.setAttribute("d", degrees >= 359.999
        ? `M 112 64 A ${radius} ${radius} 0 1 1 16 64 A ${radius} ${radius} 0 1 1 112 64`
        : `M ${start.x} ${start.y} A ${radius} ${radius} 0 ${degrees > 180 ? 1 : 0} 1 ${end.x} ${end.y}`);
      segment.setAttribute("aria-hidden", "true");
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${sector}: ${percent.toFixed(1)}%`;
      segment.append(title);
      if (degrees > 0) allocationRoot.append(segment);
      percentOffset += percent;

      const item = document.createElement("li");
      const swatch = document.createElement("span");
      swatch.className = `wealth-allocation-swatch ${wealthSectorClass(sector)}`;
      swatch.setAttribute("aria-hidden", "true");
      const name = document.createElement("span");
      name.textContent = sector;
      const weight = document.createElement("strong");
      weight.textContent = `${percent.toFixed(1)}%`;
      item.append(swatch, name, weight);
      legendRoot.append(item);
    });
    if (!sectors.length) {
      const item = document.createElement("li");
      item.textContent = "No holdings to allocate yet";
      legendRoot.append(item);
    }
    const ring = root.querySelector(".wealth-ring");
    if (ring) ring.setAttribute("aria-label", sectors.length
      ? `Portfolio allocation by sector: ${sectors.map(([sector, invested]) => `${sector} ${totalInvested ? (invested / totalInvested * 100).toFixed(1) : 0}%`).join(", ")}`
      : "No investments in the portfolio yet");
  }

  if (holdingsRoot) {
    const pending = renderPendingStockOrders(overview.paper_equity?.pending_orders || [], {escapeHtml});
    holdingsRoot.innerHTML = pending + (stocks.length
      ? stocks.map(stock => {
          const sector = wealthSectorsBySymbol[stock.symbol] || "Other";
          const percent = totalInvested ? Number(stock.cost_funding_minor || 0) / totalInvested * 100 : 0;
          const sectorClass = wealthSectorClass(sector);
          const owned = stock.instrument_id && Number(stock.quantity) > 0;
          const count = owned ? ` · ${Number(stock.quantity).toLocaleString("en-SG")} ${Number(stock.quantity) === 1 ? "share" : "shares"}` : "";
          const shortName = String(stock.name || stock.symbol).replace(/,?\s+(Inc\.?|Corporation|Corp\.?|Ltd\.?|Limited|Holdings|plc)$/i, "");
          return `<article class="wealth-holding-row">
            <span class="wealth-holding-symbol ${sectorClass}" aria-hidden="true"></span>
            <span class="wealth-holding-name"><strong>${escapeHtml(stock.name)} <span class="wealth-stock-ticker">(${escapeHtml(stock.symbol)})</span></strong><small><i class="wealth-sector-dot ${sectorClass}" aria-hidden="true"></i>${escapeHtml(sector)}${escapeHtml(count)}</small></span>
            <span class="wealth-holding-value"><strong>${escapeHtml(formatSgd(Number(stock.cost_funding_minor || 0)))}</strong><small>${percent.toFixed(1)}% of invested</small>${owned ? `<button type="button" class="wealth-ask-button wealth-sell-button" data-say="${escapeHtml(`Sell my ${shortName} shares`)}">Sell</button>` : ""}</span>
          </article>`;
        }).join("")
      : `<div class="wealth-empty"><strong>Your digiPortfolio is ready</strong><p>Your completed share purchases will appear here.</p><button type="button" class="wealth-ask-button" data-say="Show me equities">Explore equities</button></div>`);
  }

  const starterRoot = root.querySelector("#wealth-starter");
  if (starterRoot) {
    starterRoot.innerHTML = `<strong>A steady place to start</strong>
      <p>Explore a ${escapeHtml(formatSgd(WEALTH_STARTER_MONTHLY_MINOR))} monthly plan alongside your existing portfolio. You can change the monthly amount before confirming a plan.</p>
      <button type="button" class="wealth-ask-button" data-say="Suggest a long-term plan with S$${WEALTH_STARTER_MONTHLY_MINOR / 100} per month">Explore a ${escapeHtml(formatSgd(WEALTH_STARTER_MONTHLY_MINOR))}/month plan</button>
      <small>Projection only. Nothing starts or gets invested until you review and confirm.</small>`;
  }

  const profile = overview.profile;
  const profileRoot = root.querySelector("#wealth-investor-profile");
  if (profileRoot) {
    profileRoot.textContent = profile
      ? `Investor profile · ${profile.risk_label} risk · ${profile.horizon_years}-year horizon`
      : "Set an investor profile to tailor investment ideas to your goals.";
  }

  const catalogItems = [...wealthFunds, ...wealthEquities];
  const catalogRoot = root.querySelector("#wealth-explore-results");
  const searchRoot = root.querySelector("#wealth-explore-search");
  const kindRoot = root.querySelector("#wealth-explore-kind");
  const sectorRoot = root.querySelector("#wealth-explore-sector");
  const countRoot = root.querySelector("#wealth-explore-count");
  if (catalogRoot) {
    const updateCatalog = () => {
      const filtered = filterWealthCatalog(catalogItems, {
        query: searchRoot?.value,
        kind: kindRoot?.value,
        sector: sectorRoot?.value,
      });
      catalogRoot.innerHTML = renderWealthCatalog(filtered, {escapeHtml});
      if (countRoot) countRoot.textContent = `${filtered.length} instrument${filtered.length === 1 ? "" : "s"}`;
    };
    if (!catalogRoot.dataset.bound) {
      searchRoot?.addEventListener("input", updateCatalog);
      kindRoot?.addEventListener("change", updateCatalog);
      sectorRoot?.addEventListener("change", updateCatalog);
      catalogRoot.dataset.bound = "true";
    }
    updateCatalog();
  }
}

function instrumentCard(item, {escapeHtml, formatSgd}, actions) {
  return `<article class="invest-card">
    <header>
      <div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.symbol)} · ${escapeHtml(item.kind.replace("_", " "))} · ${escapeHtml(item.sector)}</small></div>
      <div class="invest-price"><strong>${escapeHtml(formatSgd(item.price_minor))}</strong><small class="${item.change_1d_pct >= 0 ? "gain" : "loss"}">${escapeHtml(pct(item.change_1d_pct))} today</small></div>
    </header>
    ${item.why ? `<p class="invest-why">${escapeHtml(item.why)}</p>` : ""}
    <dl class="invest-stats">
      <div><dt>1 month</dt><dd>${escapeHtml(pct(item.change_1m_pct))}</dd></div>
      <div><dt>3 months</dt><dd>${escapeHtml(pct(item.change_3m_pct))}</dd></div>
      <div><dt>1 year</dt><dd>${escapeHtml(pct(item.change_1y_pct))}</dd></div>
      <div><dt>Dividend</dt><dd>${escapeHtml(item.dividend_yield_pct.toFixed(1))}%</dd></div>
      <div><dt>Risk</dt><dd title="${item.risk_level} of 5">${riskDots(item.risk_level)}</dd></div>
    </dl>
    <div class="invest-points">
      <div><p class="points-label">Strengths</p><ul>${item.strengths.map(s => `<li>${escapeHtml(s)}</li>`).join("")}</ul></div>
      <div><p class="points-label risks">Risks</p><ul>${item.risks.map(r => `<li>${escapeHtml(r)}</li>`).join("")}</ul></div>
    </div>
    ${item.fit_note ? `<p class="invest-fit ${item.fits_profile ? "fits" : "above"}">${escapeHtml(item.fit_note)}</p>` : ""}
    <div class="invest-card-actions">${actions}</div>
  </article>`;
}

function planCard(plan, {escapeHtml, formatSgd}) {
  const p = plan.projection_minor;
  return `<article class="invest-card plan-card${plan.best_fit ? " best" : ""}">
    <header>
      <div><strong>${escapeHtml(plan.name)}</strong>${plan.best_fit ? '<b class="payee-flag">Best fit for you</b>' : ""}${plan.fits_profile ? "" : '<b class="payee-flag warn">Above your risk profile</b>'}<small>Risk ${riskDots(plan.risk_level)}</small></div>
    </header>
    <p class="invest-why">${escapeHtml(plan.idea)}</p>
    <div class="alloc-bar">${plan.allocations.map(a => `<span style="flex:${a.percent}" title="${escapeHtml(a.name)} ${a.percent}%">${a.percent}%</span>`).join("")}</div>
    <ul class="alloc-list">${plan.allocations.map(a => `<li>${a.percent}% ${escapeHtml(a.name)} (${escapeHtml(a.symbol)})</li>`).join("")}</ul>
    <p class="projection">${escapeHtml(formatSgd(plan.monthly_minor))} a month for ${plan.years} years: you pay in ${escapeHtml(formatSgd(plan.contributed_minor))}. At ${plan.assumed_returns_pct[1]}% a year that could become about <strong>${escapeHtml(formatSgd(p.middle))}</strong>; at ${plan.assumed_returns_pct[0]}% about ${escapeHtml(formatSgd(p.cautious))}, at ${plan.assumed_returns_pct[2]}% about ${escapeHtml(formatSgd(p.strong))}.</p>
    <div class="invest-card-actions"><button type="button" class="candidate-choice" data-say="invest S$${(plan.monthly_minor / 100).toFixed(0)} a month in the ${escapeHtml(plan.name)} plan">Start with ${escapeHtml(formatSgd(plan.monthly_minor))} a month</button></div>
  </article>`;
}

export function renderInvestingAnswer(list, investing, helpers, runCommand, cancelPlan) {
  const {escapeHtml, formatSgd} = helpers;
  let html = "";
  if (investing.view === "TRENDING") {
    const buy = item => `<button type="button" class="candidate-choice" data-say="buy S$100 of ${escapeHtml(item.name)}">Buy S$100</button>
      <button type="button" class="text-button" data-say="how is ${escapeHtml(item.name)} doing">Price chart</button>`;
    html = `<p class="match-label">Trending: rising over one and three months</p>
      ${investing.trending.map(item => instrumentCard(item, helpers, buy(item))).join("") || "<p>Nothing is rising right now.</p>"}
      <p class="match-label">Steady income within your risk profile</p>
      ${investing.income.map(item => instrumentCard(item, helpers, buy(item))).join("")}`;
  } else if (investing.view === "PLANS") {
    html = `<form class="plan-inputs">
        <label>Monthly <input name="monthly" type="number" min="50" max="10000" step="50" value="${investing.monthly_minor / 100}"></label>
        <label>Years <input name="years" type="number" min="1" max="40" value="${investing.years}"></label>
        <button type="submit" class="text-button">Recalculate</button>
      </form>
      ${investing.plans.map(plan => planCard(plan, helpers)).join("")}
      <p class="invest-note">${escapeHtml(investing.note)}</p>`;
  } else if (investing.view === "QUOTE") {
    const item = investing.instrument;
    html = instrumentCard(item, helpers, `<button type="button" class="candidate-choice" data-say="buy S$100 of ${escapeHtml(item.name)}">Buy S$100</button>
      <button type="button" class="text-button" data-say="buy 10 shares of ${escapeHtml(item.name)}">Buy 10 units</button>`)
      .replace("</header>", `</header>${sparkline(investing.history)}<small class="spark-label">Last 90 days</small>`);
  } else if (investing.view === "REAL_STOCK_BRIEF") {
    html = realStockBriefCard(investing.brief, helpers);
  } else if (investing.view === "PORTFOLIO") {
    html = `<div class="portfolio-summary">
        <span><small>Invested value</small><strong>${escapeHtml(formatSgd(investing.total_value_minor))}</strong></span>
        <span><small>Gain / loss</small><strong class="${investing.total_gain_minor >= 0 ? "gain" : "loss"}">${escapeHtml(formatSgd(investing.total_gain_minor))}</strong></span>
        <span><small>Cash in savings</small><strong>${escapeHtml(formatSgd(investing.cash_minor))}</strong></span>
      </div>
      ${investing.holdings.map(h => `<div class="holding-row">
        <span><strong>${escapeHtml(h.name)}</strong><small>${escapeHtml(h.quantity)} ${h.quantity === "1" ? "unit" : "units"} · ${h.weight_pct}% of portfolio · paid ${escapeHtml(formatSgd(h.cost_minor))}</small></span>
        <span class="holding-value"><strong>${escapeHtml(formatSgd(h.value_minor))}</strong><small class="${h.gain_minor >= 0 ? "gain" : "loss"}">${escapeHtml(pct(h.gain_pct))}</small>
        <button type="button" class="text-button" data-say="sell all my ${escapeHtml(h.name)}">Sell</button></span>
      </div>`).join("")}
      ${investing.plans.map(p => `<div class="holding-row plan-row">
        <span><strong>${escapeHtml(p.name)}</strong><small>${escapeHtml(formatSgd(p.monthly_minor))} a month · next ${escapeHtml(new Date(p.next_run_at).toLocaleDateString())}</small></span>
        <span class="holding-value"><button type="button" class="text-button" data-cancel-plan="${escapeHtml(p.plan_id)}">Stop plan</button></span>
      </div>`).join("")}`;
  }
  list.hidden = false;
  const disclaimer = investing.view === "REAL_STOCK_BRIEF" || !investing.disclaimer
    ? ""
    : `<p class="invest-disclaimer">${escapeHtml(investing.disclaimer)}</p>`;
  list.innerHTML = `${html}${disclaimer}`;
  list.querySelectorAll("[data-say]").forEach(button => button.addEventListener("click", () => runCommand(button.dataset.say)));
  list.querySelectorAll("[data-cancel-plan]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    await cancelPlan(button.dataset.cancelPlan);
    button.closest(".plan-row").remove();
  }));
  const form = list.querySelector(".plan-inputs");
  form?.addEventListener("submit", async event => {
    event.preventDefault();
    const monthly = Math.round(Number(form.monthly.value) * 100);
    const years = Number(form.years.value);
    const response = await fetch(`/api/invest/plans?monthly=${monthly}&years=${years}`);
    if (response.ok) renderInvestingAnswer(list, {view: "PLANS", ...(await response.json())}, helpers, runCommand, cancelPlan);
  });
}

export function renderOrderCard(list, order, {escapeHtml, formatSgd}, onApprove) {
  const quoteCurrency = order.quote_currency || order.instrument?.quote_currency || "SGD";
  const fundingCurrency = order.funding_currency || "SGD";
  const facts = order.plan
    ? order.plan.allocations.map(a => `<span><small>${a.percent}%</small><strong>${escapeHtml(a.name)}</strong></span>`).join("")
    : `<span><small>${order.kind === "BUY" ? "Buy" : "Sell"}</small><strong>${escapeHtml(order.quantity)} × ${escapeHtml(order.instrument.name)}</strong></span>
       <span><small>Quote</small><strong>${escapeHtml(formatTransactionalMoney(quoteCurrency, order.quote_price_minor))}</strong></span>
       <span><small>${order.kind === "BUY" ? "Price limit" : "Lowest price"}</small><strong>${escapeHtml(formatTransactionalMoney(quoteCurrency, order.limit_price_minor))}</strong></span>
       <span><small>Fee</small><strong>${escapeHtml(formatTransactionalMoney(fundingCurrency, order.fee_minor))}</strong></span>`;
  list.hidden = false;
  list.innerHTML = `<article class="receipt-choice order-card">
    <p class="match-label">${escapeHtml(order.plan ? "Monthly investment plan" : "Investment order")} · ready for your review</p>
    <div class="receipt-facts">${facts}
      <span><small>${order.kind === "SELL" ? "Paid into savings" : order.plan ? "Each month" : "At most from savings"}</small><strong>${escapeHtml(formatTransactionalMoney(fundingCurrency, order.total_minor))}</strong></span>
      <span><small>Funding source</small><strong>${escapeHtml(formatFundingSource(order))}</strong></span>
    </div>
    ${order.risk_ack_required ? `<p class="invest-fit above">Riskier than your profile (${escapeHtml(order.profile?.risk_label || "")}). You'll be asked to confirm you understand.</p>` : ""}
    <button type="button" class="candidate-choice">Review &amp; confirm with your face</button>
  </article>`;
  list.querySelector(".candidate-choice").addEventListener("click", () => onApprove(order));
}

export function openProfileDialog(questions) {
  const dialog = document.querySelector("#profile-dialog");
  const form = dialog.querySelector("#profile-questions");
  const ack = dialog.querySelector("#profile-ack");
  const status = dialog.querySelector("#profile-status");
  const save = dialog.querySelector("#profile-save");
  const cancel = dialog.querySelector("#profile-cancel");
  form.innerHTML = questions.map(q => `<fieldset><legend>${q.text}</legend>
    ${q.options.map((option, index) => `<label><input type="radio" name="${q.id}" value="${index + 1}"> ${option}</label>`).join("")}
  </fieldset>`).join("");
  ack.checked = false;
  status.textContent = "";
  return new Promise(resolve => {
    const close = saved => { save.onclick = null; cancel.onclick = null; dialog.close(); resolve(saved); };
    cancel.onclick = () => close(null);
    dialog.oncancel = event => { event.preventDefault(); close(null); };
    save.onclick = async () => {
      const answers = Object.fromEntries(questions.map(q => [q.id, Number(form.querySelector(`input[name="${q.id}"]:checked`)?.value || 0)]));
      if (Object.values(answers).some(value => !value)) { status.textContent = "Please answer all four questions."; return; }
      if (!ack.checked) { status.textContent = "Please confirm you understand investment risk."; return; }
      const response = await fetch("/api/invest/profile", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({answers, acknowledged: true}),
      });
      if (!response.ok) { status.textContent = "Could not save your profile. Please try again."; return; }
      close(await response.json());
    };
    dialog.showModal();
  });
}

// Singpass authorisation for an investment order: the request goes to the
// (mock) Singpass app, which shows the same order and match code. Resolves
// true once approved there; false if rejected, expired or cancelled.
export function authoriseWithSingpass(order, {escapeHtml, formatSgd}, options = {}) {
  const dialog = document.querySelector("#singpass-dialog");
  const details = dialog.querySelector("#singpass-details");
  const code = dialog.querySelector("#singpass-code");
  const status = dialog.querySelector("#singpass-status");
  const open = dialog.querySelector("#singpass-open");
  const cancel = dialog.querySelector("#singpass-cancel");
  return new Promise(async resolve => {
    let timer = null;
    const finish = result => {
      clearInterval(timer);
      open.onclick = null;
      cancel.onclick = null;
      dialog.oncancel = null;
      dialog.close();
      resolve(result);
    };
    const requestUrl = options.requestUrl || "/api/singpass/authorisations";
    const requestBody = options.requestBody || {order_id: order.order_id, order_digest: order.order_digest};
    const response = await fetch(requestUrl, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(requestBody),
    });
    const request = await response.json().catch(() => ({}));
    if (!response.ok) {
      resolve({approved: false, reason: request.detail || `HTTP_${response.status}`});
      return;
    }
    details.innerHTML = `<strong>${escapeHtml(request.title)}</strong><span>${escapeHtml(formatTransactionalMoney(request.currency || order.funding_currency || "SGD", request.total_minor))}</span>`;
    code.textContent = request.match_code;
    status.textContent = "Waiting for your approval in the Singpass app…";
    status.className = "approval-note";
    open.onclick = () => window.open("/mock-singpass", "dimon-singpass", "width=420,height=760");
    cancel.onclick = () => finish({approved: false, reason: "SINGPASS_CANCELLED"});
    dialog.oncancel = event => { event.preventDefault(); finish({approved: false, reason: "SINGPASS_CANCELLED"}); };
    dialog.showModal();
    timer = setInterval(async () => {
      const current = await fetch(`/api/singpass/authorisations/${encodeURIComponent(request.auth_id)}`, {cache: "no-store"})
        .then(r => r.json()).catch(() => ({}));
      if (current.status === "APPROVED") {
        status.textContent = "Approved in Singpass.";
        setTimeout(() => finish({approved: true}), 500);
      } else if (["REJECTED", "EXPIRED", "SUPERSEDED"].includes(current.status)) {
        finish({approved: false, reason: `SINGPASS_${current.status}`});
      }
    }, options.pollIntervalMs || 1500);
  });
}
