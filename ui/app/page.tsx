"use client";

import { useState } from "react";

const example = {
  dataset_id: "replace-with-a-frozen-dataset-id",
  strategies: [{ name: "sma_cross" }, { name: "buy_and_hold" }],
  base: { split: "exploratory" },
  grid: { "strategy.fast": [3, 5], "strategy.slow": [10, 20] },
  max_pending: 32
};

const strategyExample = {
  name: "local-sma-observer", strategy: { name: "sma_cross", fast: 5, slow: 20 },
  source: "alpaca", feed: "iex", symbols: [], mode: "signals",
  missing_policy: "available", min_coverage_fraction: "0.5"
};

export default function Home() {
  const [key, setKey] = useState("");
  const [panels, setPanels] = useState<Record<string, unknown>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [spec, setSpec] = useState(JSON.stringify(example, null, 2));
  const [experiment, setExperiment] = useState("");
  const [offset, setOffset] = useState(0);
  const [account, setAccount] = useState("");
  const [tradingRun, setTradingRun] = useState("");
  const [policy, setPolicy] = useState("{}");
  const [acknowledged, setAcknowledged] = useState(false);
  const [order, setOrder] = useState('{"order":{"symbol":"SPY","side":"buy","qty":"1","type":"limit","limit_price":"1.00"}}');
  const [orderKey, setOrderKey] = useState("");
  const [brokerOrder, setBrokerOrder] = useState("");
  const [command, setCommand] = useState("");
  const [strategySpec, setStrategySpec] = useState(JSON.stringify(strategyExample, null, 2));
  const [strategy, setStrategy] = useState("");
  const [exportSpec, setExportSpec] = useState('{"series_ids":[],"start":"2026-11-12T15:00:00Z","end":"2026-11-12T16:00:00Z"}');
  const [historySpec, setHistorySpec] = useState('{"provider":"alpaca","feed":"iex","symbols":["SPY","QQQ"],"start":"2026-11-12T15:00:00Z","end":"2026-11-12T16:00:00Z"}');
  const [history, setHistory] = useState("");
  const [run, setRun] = useState("");

  async function request(path: string, body?: unknown, identity?: string) {
    const response = await fetch(`/api/control/v1/${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "X-API-Key": key, "Content-Type": "application/json", ...(identity ? { "Idempotency-Key": identity } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store"
    });
    if (!response.ok) throw new Error(`API request failed (${response.status}). Check authentication and readiness.`);
    return response.json();
  }

  async function operate(action: () => Promise<void>) {
    setBusy(true); setError("");
    try { await action(); } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Operation failed");
    } finally { setBusy(false); }
  }

  async function refresh() {
    const paths = ["platform/status", "data/coverage", "trading/accounts", "research/datasets", "strategies/runs"];
    const values = await Promise.all(paths.map(path => request(path)));
    setPanels(Object.fromEntries(paths.map((path, index) => [path, values[index]])));
    const accounts = values[2];
    if (!account && accounts.length === 1) setAccount(accounts[0].account_id);
  }

  function show(name: string, value: unknown) { setPanels(previous => ({ ...previous, [name]: value })); }
  function uuid(value: string) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) throw new Error("Enter a valid UUID");
    return value;
  }
  async function trading(action: string, payload: unknown) {
    const submitted = await request(`trading/accounts/${uuid(account)}/${action}`, payload);
    setCommand(submitted.command_id); show("trading command submitted", submitted);
  }
  async function inspectAccount() {
    const id = uuid(account);
    const values = await Promise.all(["orders", "risk", "audit"].map(path => request(`trading/accounts/${id}/${path}`)));
    values.forEach((value, index) => show(["orders", "risk decisions", "financial audit"][index], value));
  }
  async function download(kind: "artifact" | "fills" | "engine") {
    const runPath = `research/runs/${uuid(run)}`;
    const path = kind === "engine" ? `research/engines/${(await request(runPath)).engine_id}` : `${runPath}/${kind}`;
    const response = await fetch(`/api/control/v1/${path}`, { headers: { "X-API-Key": key }, cache: "no-store" });
    if (!response.ok) throw new Error(`Artifact unavailable (${response.status})`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a"); link.href = url; link.download = `${run}-${kind}.${kind === "fills" ? "jsonl" : "json"}`; link.click();
    URL.revokeObjectURL(url);
  }

  async function inspect(page = offset) {
    if (!/^[0-9a-f-]{36}$/i.test(experiment)) throw new Error("Enter an experiment UUID");
    const result = await request(`research/experiments/${experiment}?offset=${page}&limit=25`);
    setOffset(page); setPanels(previous => ({ ...previous, experiment: result }));
  }

  return <main>
    <header><h1>Bagel</h1><p>Local research &amp; paper operations</p></header>
    <aside>Local validation only—not approved for production. Startup never arms submission. Paper orders require runtime permission, a reconciled account and an explicit expiring arm.</aside>
    <section>
      <h2>Operator access</h2>
      <label>API key <input type="password" autoComplete="off" value={key}
        onChange={event => setKey(event.target.value)} placeholder="BAGEL_API_KEY" /></label>
      <p>The key stays in this page’s memory; it is not saved in browser storage. No provider credentials reach this UI.</p>
      <button disabled={busy || !key} onClick={() => operate(refresh)}>Refresh readiness, data, account &amp; datasets</button>
    </section>
    {error && <p role="alert">{error}</p>}
    {Object.entries(panels).map(([name, value]) => <section key={name}>
      <h2>{name}</h2><pre>{JSON.stringify(value, null, 2)}</pre>
    </section>)}
    <section>
      <h2>Paper account controls</h2>
      <label>Account UUID <input value={account} onChange={event => setAccount(event.target.value)} /></label>
      <label>Trading run UUID <input value={tradingRun} onChange={event => setTradingRun(event.target.value)} /></label>
      <button disabled={busy} onClick={() => setTradingRun(crypto.randomUUID())}>New trading run identity</button>
      <label>Risk policy JSON <textarea rows={5} value={policy} onChange={event => setPolicy(event.target.value)} /></label>
      <label><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} /> I intend to submit orders to Alpaca Paper, not a simulation.</label>
      <button disabled={busy || !key || !acknowledged} onClick={() => operate(() => trading("arm", {
        run_id: uuid(tradingRun), expires_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(), policy: JSON.parse(policy)
      }))}>Explicitly arm this run for 10 minutes</button>
      <button disabled={busy || !key || !account} onClick={() => operate(() => trading("pause", {}))}>Pause new submission</button>
      <button disabled={busy || !key || !account} onClick={() => operate(() => trading("reconcile", {}))}>Pause &amp; request full reconciliation</button>
      <button disabled={busy || !key || !account} onClick={() => operate(inspectAccount)}>Inspect orders, risk &amp; audit</button>
      <p>Pause does not cancel orders already accepted by the broker. Cancel only a known order below. Poll each durable command for its outcome.</p>
      <label>Order JSON <textarea rows={6} value={order} onChange={event => { setOrder(event.target.value); setOrderKey(""); }} /></label>
      <button disabled={busy || !key || !acknowledged || !account} onClick={() => operate(async () => {
        const identity = orderKey || crypto.randomUUID(); setOrderKey(identity);
        const submitted = await request(`trading/accounts/${uuid(account)}/orders`, { ...JSON.parse(order), run_id: uuid(tradingRun), decision_id: identity }, identity);
        setCommand(submitted.command_id); show("trading command submitted", submitted);
      })}>Submit / retry this order identity</button>
      <button disabled={busy} onClick={() => setOrderKey(crypto.randomUUID())}>New intentional order identity</button>
      <p>Retries retain the same identity. Inspect an uncertain order before creating a new identity; never blindly resubmit it.</p>
      <label>Broker order UUID <input value={brokerOrder} onChange={event => setBrokerOrder(event.target.value)} /></label>
      <button disabled={busy || !key || !account || !brokerOrder} onClick={() => operate(() => trading("cancel", { broker_order_id: uuid(brokerOrder) }))}>Request cancellation</button>
      <label>Command UUID <input value={command} onChange={event => setCommand(event.target.value)} /></label>
      <button disabled={busy || !key || !command} onClick={() => operate(async () => show("command outcome", await request(`trading/commands/${uuid(command)}`)))}>Inspect command</button>
    </section>
    <section>
      <h2>Registered strategies</h2>
      <p>Empty symbols selects a frozen snapshot of all ingested members in the chosen feed—not a fixed watchlist. New runs are paused. Signals mode never submits orders.</p>
      <textarea aria-label="Strategy run JSON" rows={13} value={strategySpec} onChange={event => setStrategySpec(event.target.value)} />
      <button disabled={busy || !key} onClick={() => operate(async () => {
        const submitted = await request("strategies/runs", JSON.parse(strategySpec)); setStrategy(submitted.strategy_id); show("strategy created", submitted);
      })}>Create paused strategy run</button>
      <label>Strategy UUID <input value={strategy} onChange={event => setStrategy(event.target.value)} /></label>
      <button disabled={busy || !key || !strategy} onClick={() => operate(async () => show("strategy", await request(`strategies/runs/${uuid(strategy)}/start`, {})))}>Start / resume strategy</button>
      <button disabled={busy || !key || !strategy} onClick={() => operate(async () => show("strategy", await request(`strategies/runs/${uuid(strategy)}/pause`, {})))}>Pause strategy</button>
      <button disabled={busy || !key || !strategy} onClick={() => operate(async () => show("strategy signals", await request(`strategies/runs/${uuid(strategy)}/signals?limit=25`)))}>Inspect latest signals &amp; exact revisions</button>
    </section>
    <section>
      <h2>Historical ingestion &amp; dataset freezing</h2>
      <p>Ingestion calls the configured real provider. Freezing only reads the local database; experiments use the immutable result offline.</p>
      <textarea aria-label="Historical job JSON" rows={7} value={historySpec} onChange={event => setHistorySpec(event.target.value)} />
      <button disabled={busy || !key} onClick={() => operate(async () => {
        const submitted = await request("data/jobs", JSON.parse(historySpec)); setHistory(submitted.job_id); show("history submitted", submitted);
      })}>Submit provider history job</button>
      <label>History job UUID <input value={history} onChange={event => setHistory(event.target.value)} /></label>
      <button disabled={busy || !key || !history} onClick={() => operate(async () => show("history job", await request(`data/jobs/${uuid(history)}`)))}>Inspect history job</button>
      <button disabled={busy || !key || !history} onClick={() => operate(async () => show("history job", await request(`data/jobs/${uuid(history)}/cancel`, {})))}>Cancel job</button>
      <button disabled={busy || !key || !history} onClick={() => operate(async () => show("history job", await request(`data/jobs/${uuid(history)}/retry`, {})))}>Retry job</button>
      <textarea aria-label="Dataset export JSON" rows={5} value={exportSpec} onChange={event => setExportSpec(event.target.value)} />
      <button disabled={busy || !key} onClick={() => operate(async () => show("frozen dataset", await request("research/datasets/export", JSON.parse(exportSpec))))}>Freeze selected local series</button>
    </section>
    <section>
      <h2>Submit a reproducible experiment</h2>
      <p>Use a frozen dataset ID above. Repeated identical submissions reuse the same experiment and run identities.</p>
      <textarea aria-label="Experiment JSON" rows={17} value={spec} onChange={event => setSpec(event.target.value)} />
      <button disabled={busy || !key} onClick={() => operate(async () => {
        const submitted = await request("research/experiments", JSON.parse(spec));
        setExperiment(submitted.experiment_id); setOffset(0);
        setPanels(previous => ({ ...previous, submitted }));
      })}>Submit experiment</button>
    </section>
    <section>
      <h2>Inspect experiment</h2>
      <input aria-label="Experiment ID" value={experiment} onChange={event => setExperiment(event.target.value)} />
      <button disabled={busy || !key} onClick={() => operate(() => inspect(0))}>Inspect</button>
      <button disabled={busy || offset === 0} onClick={() => operate(() => inspect(Math.max(0, offset - 25)))}>Previous 25</button>
      <button disabled={busy || !experiment} onClick={() => operate(() => inspect(offset + 25))}>Next 25</button>
      <button disabled={busy || !key || !experiment} onClick={() => operate(async () => {
        const canceled = await request(`research/experiments/${experiment}/cancel`, {});
        setPanels(previous => ({ ...previous, experiment: canceled }));
      })}>Stop generating trials</button>
      <p>Cancellation stops new trials. Already-generated shared runs retain their own status and run-level cancellation.</p>
    </section>
    <section>
      <h2>Individual research run</h2>
      <label>Run UUID <input value={run} onChange={event => setRun(event.target.value)} /></label>
      <button disabled={busy || !key || !run} onClick={() => operate(async () => show("research run", await request(`research/runs/${uuid(run)}`)))}>Inspect run &amp; attempts</button>
      <button disabled={busy || !key || !run} onClick={() => operate(async () => show("research run", await request(`research/runs/${uuid(run)}/cancel`, {})))}>Cancel run</button>
      <button disabled={busy || !key || !run} onClick={() => operate(async () => show("research run", await request(`research/runs/${uuid(run)}/retry`, {})))}>Retry run</button>
      <button disabled={busy || !key || !run} onClick={() => operate(() => download("artifact"))}>Download result</button>
      <button disabled={busy || !key || !run} onClick={() => operate(() => download("fills"))}>Download streamed fill log</button>
      <button disabled={busy || !key || !run} onClick={() => operate(() => download("engine"))}>Download archived algorithm sources</button>
    </section>
  </main>;
}
