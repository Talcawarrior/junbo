// ==========================================
// Junbo Dashboard — Real API Client
// ==========================================
"use client";

import { useState, useEffect, useCallback, useRef } from "react";

// ---- API Response Types (matching Python main.py) ----

export interface StatusResponse {
  is_running: boolean;
  locked: boolean;
  portfolio: {
    initial: number;
    current: number;
    daily_pnl: number;
    daily_roi: number;
    unrealized_pnl: number;
    realized_pnl: number;
    total_pnl: number;
    total_roi: number;
    exposure: number;
    max_exposure: number;
  };
  stats: {
    total_signals: number;
    total_bets: number;
    win_count: number;
    loss_count: number;
    total_closed: number;
    last_scan: string | null;
  };
  limits: {
    max_bet_pct: number;
    max_exposure_pct: number;
    daily_stop_loss_pct: number;
    city_cap: number;
  };
  metrics: {
    sharpe_ratio: number;
    max_drawdown_pct: number;
  };
  open_positions?: Array<{
    id: number;
    city: string;
    side: string;
    shares: number;
    current_price: number;
    unrealized_pnl: number;
    amount: number;
  }>;
}

export interface Signal {
  id: number;
  market_id: string;
  city: string;
  outcome: "YES" | "NO";
  entry_price: number;
  current_price: number;
  stake_amount: number;
  unrealized_pnl: number;
  fair_value: number | null;
  edge: number | null;
  entry_edge: number | null;
  live_edge: number | null;
  move_pct: number | null;
  ladder_orders: unknown[];
  placed_at: string | null;
  resolution_date: string | null;
  status: string;
}

export interface HistoryEntry {
  id: number;
  city: string;
  outcome: string;
  entry_price: number;
  stake_amount: number;
  realized_pnl: number;
  roi: number;
  edge: number | null;
  result: "WIN" | "LOSS" | "PARTIAL_TP";
  placed_at: string | null;
  settled_at: string | null;
  closed_at: string | null;  // Early exit time
  exit_type: string;  // ST, TP, SL, TS, TD, OT, PT
}

export interface HistoryStats {
  total_won: number;
  total_lost: number;
  win_rate: number;
  overall_roi: number;
  total_stake: number;
  total_pnl: number;
  total_win_pnl: number;
  total_loss_pnl: number;
  profit_factor: number;
  avg_edge: number;
}

export interface HealthResponse {
  verdict: "healthy" | "degraded" | "critical" | "error";
  verdict_text: string;
  verdict_color: string;
  activity_24h: {
    bets_opened: number;
    pass_reasons: Array<{
      market_id: string;
      edge_pct: number;
      reason: string;
      time: string;
    }>;
    total_analyses: number;
  };
  edge_distribution: {
    values: Array<{
      bet_id: number;
      raw_edge_pct: number | null;
      net_edge_pct: number | null;
      slippage_pct: number | null;
      market_id: string;
      city: string;
      stake: number;
      status: string;
      pnl: number;
    }>;
    avg_net_edge_pct: number;
    min_net_edge_pct: number;
    max_net_edge_pct: number;
    count: number;
  };
  summary_all: {
    total_settled: number;
    wins: number;
    losses: number;
    win_rate_pct: number;
    total_pnl: number;
    total_stake: number;
    roi_pct: number;
    avg_net_edge_pct: number;
    wins_by_exit: Record<string, number>;
    losses_by_exit: Record<string, number>;
  };
  red_flags: Array<{
    severity: "critical" | "warning" | "info";
    message: string;
    action: string;
  }>;
  daily_pnl_timeline: Array<{
    date: string;
    pnl: number;
    stake: number;
    wins: number;
    losses: number;
    total: number;
    win_rate: number;
    roi: number;
  }>;
}

// ---- UI Component Types (matching mock-data.ts) ----

export interface KpiData {
  portfolioValue: number;
  dailyPnl: number;
  totalPnl: number;
  openPositions: number;
  winRate: number;
  winRateLabel: string;
  totalTrades: number;
  wins: number;
  losses: number;
  avgEdge: number;
  sharpeRatio: number;
  maxDrawdown: number;
  // Open positions summary
  openPositionsValue: number;  // Açık pozisyonların toplam stake tutarı
  maxOpenableUsd: number;      // Maksimum açılabilecek USD (gün itibarıyla)
  // Second row metrics
  totalPnlValue: number;       // Total PnL ($)
  realizedPnl: number;         // Kapalı bahislerden realized PnL
  unrealizedPnl: number;       // Açık pozisyonlardan unrealized PnL
  totalStake: number;          // Toplam yatırılan tutar (tüm bahisler)
  totalRoi: number;            // Total ROI (%)
  closedBets: number;          // Kapalı Bahis
  closedWins: number;
  closedLosses: number;
  expectancy: number;          // Ortalama kazanç/bahis ($)
  avgBetSize: number;          // Ortalama Bahis Tutarı ($)
  profitFactor: number;        // Profit Factor
}

export interface PortfolioPoint {
  date: string;
  value: number;
  drawdown?: number;
}

export interface OpenPosition {
  id: string;
  city: string;
  side: "YES" | "NO";
  entryPrice: number;
  currentPrice: number;
  pnl: number;
  edge: number;
  timeLeft: string;
  openedAt: string;   // formatted date string
  conditionId: string;
  amount: number;
  metric: string | null;  // temperature_max → H, temperature_min → L
  threshold: number | null;   // °C
}

export interface ActivityItem {
  id: string;
  time: string;
  color: "blue" | "purple" | "gray" | "orange" | "teal" | "red";
  message: string;
}

export interface EdgeBucket {
  range: string;
  count: number;
}

export interface TradeHistoryEntry {
  id: string;
  timestamp: string;
  city: string;
  side: "YES" | "NO";
  entryPrice: number;
  exitPrice: number;
  pnl: number;
  result: "WIN" | "LOSS" | "PARTIAL_TP";
  edge: number;
  duration: string;
  closedAt: string;  // formatted closing date/time
  closedAtISO: string | null;  // raw ISO date for filtering
  strategy: string;
  conditionId: string;
  exitType: string;  // ST, TP, SL, TS, TD
}

export interface ModelScore {
  name: string;
  brierScore: number | null;
  accuracy: number | null;
  weight: number;
  trend: "up" | "down" | "stable";
  sampleCount: number;
}

export type HealthVerdict = "healthy" | "degraded" | "critical" | "error";
export type FlagSeverity = "critical" | "warning" | "info";
export type SlippageEntry = {
  id: string;
  city: string;
  side: string;
  expected_price: number;
  entry_price: number;
  slippage_pct: number;
  result: string;
  analyzed_at: string | null;
};

// ---- Fetch helpers ----

const REFRESH_INTERVAL = 10000; // 10 seconds for fast endpoints
const HEALTH_REFRESH_INTERVAL = 60000; // 60 seconds for slow health-check

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`API error ${res.status}: ${res.statusText}`);
  return res.json();
}

// ---- Data mapping functions ----

function mapKpiData(
  status: StatusResponse | null,
  health: HealthResponse | null,
  historyStats: HistoryStats | null,
): KpiData {
  if (!status) {
    return {
      portfolioValue: 10000,
      dailyPnl: 0,
      totalPnl: 0,
      openPositions: 0,
      winRate: 0,
      winRateLabel: "Veri yok",
      totalTrades: 0,
      wins: 0,
      losses: 0,
      avgEdge: 0,
      sharpeRatio: 0,
      maxDrawdown: 0,
      openPositionsValue: 0,
      maxOpenableUsd: 0,
        totalPnlValue: 0,
        realizedPnl: 0,
        unrealizedPnl: 0,
        totalStake: 0,
        totalRoi: 0,
      closedBets: 0,
      closedWins: 0,
      closedLosses: 0,
      expectancy: 0,
      avgBetSize: 0,
      profitFactor: 0,
    };
  }
  const s = status.stats;
  const p = status.portfolio;
  
  // Use historyStats.total_pnl (realized only) — consistent with trade history
  const hs = historyStats;
  const realizedPnl = hs?.total_pnl ?? 0;
  const unrealizedPnl = status.open_positions?.reduce((sum: number, pos: Record<string, unknown>) => sum + ((pos.unrealized_pnl as number) || 0), 0) ?? 0;
  const totalPnlValue = realizedPnl + unrealizedPnl;
  const totalStake = (hs?.total_stake ?? 0) + (status.open_positions?.reduce((sum: number, pos: Record<string, unknown>) => sum + ((pos.amount as number) || 0), 0) ?? 0);
  const totalRoi = hs?.overall_roi ?? 0;
  const closedWins = hs?.total_won ?? 0;
  const closedLosses = hs?.total_lost ?? 0;
  const closedBets = closedWins + closedLosses;
  const winRate = closedBets > 0 ? (closedWins / closedBets) * 100 : 0;
  const expectancy = closedBets > 0 ? totalPnlValue / closedBets : 0;
  const avgBetSize = closedBets > 0 && hs?.total_stake ? hs.total_stake / closedBets : 0;
  const profitFactor = hs?.profit_factor ?? 0;

  let winRateLabel = "Veri yok";
  if (closedBets > 0) {
    if (winRate >= 70) winRateLabel = "Mükemmel";
    else if (winRate >= 60) winRateLabel = "İyi";
    else if (winRate >= 50) winRateLabel = "Orta";
    else winRateLabel = "Zayıf";
  }

  const avgEdge = health?.edge_distribution?.avg_net_edge_pct ?? 0;

  // Calculate open positions total value (sum of stake amounts — cash locked)
  const openPositionsValue = status.open_positions?.reduce(
    (sum, pos) => sum + (pos.amount || 0), 0
  ) ?? 0;

  // Use the conservative max_exposure from the backend API
  // (initial + realized_before_today) × TOTAL_EXPOSURE_PCT
  // NOT current portfolio — that would inflate the cap with unrealized gains
  const maxExposure = status.portfolio.max_exposure;

  return {
    portfolioValue: p.initial + p.realized_pnl + p.unrealized_pnl,
    dailyPnl: p.daily_pnl,
    totalPnl: hs?.total_pnl ?? 0,
    openPositions: s.total_bets,
    winRate: Math.round(winRate * 10) / 10,
    winRateLabel,
    totalTrades: closedBets,
    wins: closedWins,
    losses: closedLosses,
    avgEdge: Math.round(avgEdge * 10) / 10,
    sharpeRatio: status.metrics?.sharpe_ratio ?? 0,
    maxDrawdown: status.metrics?.max_drawdown_pct ?? 0,
    // Open positions summary
    openPositionsValue: Math.round(openPositionsValue * 100) / 100,
    maxOpenableUsd: Math.round(maxExposure * 100) / 100,
    // Second row
    totalPnlValue,
    realizedPnl,
    unrealizedPnl,
    totalStake,
    totalRoi,
    closedBets,
    closedWins,
    closedLosses,
    expectancy: Math.round(expectancy * 100) / 100,
    avgBetSize: Math.round(avgBetSize * 100) / 100,
    profitFactor,
  };
}

function mapPortfolioData(status: StatusResponse | null, history: HistoryEntry[]): PortfolioPoint[] {
  // Build equity curve from settled bets
  if (!status && history.length === 0) return [];

  const initial = status?.portfolio?.initial ?? 10000;
  const settled = history
    .filter((h) => h.settled_at)
    .sort((a, b) => new Date(a.settled_at!).getTime() - new Date(b.settled_at!).getTime());

  if (settled.length === 0) {
    // Return a single point with current value
    const current = status ? initial + status.portfolio.realized_pnl + status.portfolio.unrealized_pnl : initial;
    return [{ date: "Bugün", value: Math.round(current) }];
  }

  const points: PortfolioPoint[] = [];
  let running = initial;

  // Group by date
  const byDate = new Map<string, number>();
  for (const h of settled) {
    const d = new Date(h.settled_at!);
    const key = `${d.getDate()} ${d.toLocaleDateString("tr-TR", { month: "short" })}`;
    byDate.set(key, (byDate.get(key) ?? 0) + h.realized_pnl);
  }

  let peak = initial;
  for (const [date, pnl] of byDate) {
    running += pnl;
    if (running > peak) peak = running;
    const drawdown = peak > 0 ? ((peak - running) / peak) * 100 : 0;
    points.push({ date, value: Math.round(running), drawdown: Math.round(drawdown * 10) / 10 });
  }

  // Add current value as last point
  if (status) {
    const current = initial + status.portfolio.realized_pnl + status.portfolio.unrealized_pnl;
    const today = new Date();
    const todayKey = `${today.getDate()} ${today.toLocaleDateString("tr-TR", { month: "short" })}`;
    if (points.length === 0 || points[points.length - 1].date !== todayKey) {
      points.push({ date: todayKey, value: Math.round(current) });
    } else {
      points[points.length - 1].value = Math.round(current);
    }
  }

  return points.length > 0 ? points : [{ date: "Bugün", value: initial }];
}

function mapOpenPositions(signals: Signal[]): OpenPosition[] {
  return signals.map((s) => {
    // Use entry_edge (edge at bet placement) — live_edge (fair_value - current) goes
    // negative as price rises and is misleading in the table.
    const edge = s.entry_edge ?? s.live_edge ?? s.edge ?? 0;
    const edgePct = Math.round(edge * 1000) / 10; // Convert to percentage

    // Format resolution_date as closing date
    let closesAt = "—";
    if (s.resolution_date) {
      closesAt = new Date(s.resolution_date).toLocaleDateString("tr-TR", {
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    }

    const openedAt = s.placed_at
      ? new Date(s.placed_at).toLocaleDateString("tr-TR", {
          day: "numeric",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "—";

    return {
      id: String(s.id),
      city: s.city,
      side: s.outcome as "YES" | "NO",
      entryPrice: Math.round(s.entry_price * 100) / 100,
      currentPrice: Math.round(s.current_price * 100) / 100,
      pnl: Math.round(s.unrealized_pnl * 100) / 100,
      edge: Math.round(edgePct * 10) / 10,
      timeLeft: closesAt,
      openedAt,
      conditionId: s.market_id ? `${s.market_id.slice(0, 6)}…${s.market_id.slice(-4)}` : "—",
      amount: s.stake_amount ?? 0,
      threshold: (s as Record<string, unknown>).threshold as number ?? null,
      metric: (s as Record<string, unknown>).metric as string ?? null,
    };
  });
}

function mapActivityFeed(signals: Signal[], history: HistoryEntry[], status?: Record<string, unknown> | null, health?: Record<string, unknown> | null, weights?: Record<string, { weight: number; brier_score?: number | null; accuracy?: number | null; num_predictions?: number; last_updated?: string | null }> | null): ActivityItem[] {
  const items: Array<{ item: ActivityItem; sortDate: number }> = [];
  let idCounter = 0;

  const fmtActivityTime = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleDateString("tr-TR", { day: "numeric", month: "short" }) + " " +
      d.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
  };

  // System events: last scan time
  const statsObj = status && typeof status === "object" ? (status as Record<string, unknown>).stats : null;
  if (statsObj && typeof statsObj === "object") {
    const lastScan = (statsObj as Record<string, unknown>).last_scan;
    if (lastScan && typeof lastScan === "string") {
      idCounter++;
      items.push({
        item: {
          id: `sys_scan_${idCounter}`,
          time: fmtActivityTime(lastScan),
          color: "purple",
          message: "🔄 Tarama tamamlandı — tüm piyasalar analiz edildi",
        },
        sortDate: new Date(lastScan).getTime(),
      });
    }
  }

  // System events: total analyses from health
  const actObj = health && typeof health === "object" ? (health as Record<string, unknown>).activity_24h : null;
  if (actObj && typeof actObj === "object") {
    const totalAnalyses = (actObj as Record<string, unknown>).total_analyses;
    const betsOpened = (actObj as Record<string, unknown>).bets_opened;
    if (typeof totalAnalyses === "number" && totalAnalyses > 0) {
      idCounter++;
      items.push({
        item: {
          id: `sys_analysis_${idCounter}`,
          time: "son 24 saat",
          color: "purple",
          message: `🧠 ISA-Karpathy: ${totalAnalyses} analiz, ${betsOpened} bahis açıldı`,
        },
        sortDate: Date.now() - 1000, // slightly older than real-time
      });
    }
  }

  // System events: ASI weight update
  if (weights && typeof weights === "object") {
    let latestUpdate: string | null = null;
    let modelName: string | null = null;
    for (const [name, w] of Object.entries(weights)) {
      if (w && typeof w === "object" && w.last_updated) {
        if (!latestUpdate || w.last_updated > latestUpdate) {
          latestUpdate = w.last_updated;
          modelName = name;
        }
      }
    }
    if (latestUpdate) {
      idCounter++;
      items.push({
        item: {
          id: `sys_asi_${idCounter}`,
          time: fmtActivityTime(latestUpdate),
          color: "purple",
          message: `⚡ ASI-Evolve: ${modelName ?? "model"} ağırlıkları güncellendi`,
        },
        sortDate: new Date(latestUpdate).getTime(),
      });
    }
  }

  // Recent signals (open bets) — show up to 10
  for (const s of signals.slice(0, 10)) {
    idCounter++;
    const time = s.placed_at ? fmtActivityTime(s.placed_at) : "?";
    const edge = s.entry_edge ?? s.live_edge ?? s.edge ?? 0;
    const edgePct = (Math.round(edge * 1000) / 10).toFixed(1);
    const sortDate = s.placed_at ? new Date(s.placed_at).getTime() : 0;
    items.push({
      item: {
        id: `s${idCounter}`,
        time,
        color: "blue",
        message: `${s.city} için ${s.outcome} bet: $${s.stake_amount?.toFixed(2) ?? "?"} @ ${s.entry_price.toFixed(2)} (net edge: ${edgePct}%)`,
      },
      sortDate,
    });
  }

  // Recent history (settled bets) — show up to 10
  for (const h of history.slice(0, 10)) {
    idCounter++;
    const time = h.settled_at ? fmtActivityTime(h.settled_at)
      : h.closed_at ? fmtActivityTime(h.closed_at)
      : h.placed_at ? fmtActivityTime(h.placed_at)
      : "?";
    const color = h.result === "WIN" ? "teal" : "red";
    const pnlStr = h.realized_pnl >= 0 ? `+$${h.realized_pnl.toFixed(2)}` : `-$${Math.abs(h.realized_pnl).toFixed(2)}`;
    const exitLabels: Record<string, string> = {
      TP: "💰 Take Profit",
      SL: "🛑 Stop Loss",
      TS: "📉 Trailing Stop",
      TD: "⏰ Time Decay",
      ST: "📊 Settlement",
      OT: "⏰ Timeout",
    };
    const exitLabel = exitLabels[h.exit_type] ?? "";
    const msg = exitLabel
      ? `${h.city}: ${h.outcome} ${h.result === "WIN" ? "kazandı" : "kaybetti"} ${pnlStr} — ${exitLabel}`
      : `${h.city} marketi çözüldü: ${h.outcome} ${h.result === "WIN" ? "kazandı" : "kaybetti"}, ${pnlStr}`;
    const sortDate = h.settled_at ? new Date(h.settled_at).getTime()
      : h.closed_at ? new Date(h.closed_at).getTime()
      : h.placed_at ? new Date(h.placed_at).getTime()
      : 0;
    items.push({
      item: {
        id: `h${idCounter}`,
        time,
        color,
        message: msg,
      },
      sortDate,
    });
  }

  // Sort by actual timestamp descending
  items.sort((a, b) => b.sortDate - a.sortDate);
  return items.slice(0, 25).map(({ item }) => item);
}

function mapEdgeDistribution(health: HealthResponse | null): EdgeBucket[] {
  const buckets: EdgeBucket[] = [
    { range: "0-5%", count: 0 },
    { range: "5-10%", count: 0 },
    { range: "10-15%", count: 0 },
    { range: "15-20%", count: 0 },
    { range: "20-30%", count: 0 },
    { range: "30%+", count: 0 },
  ];

  if (!health?.edge_distribution?.values?.length) return buckets;

  // Distribute each trade's net_edge_pct into the correct bucket
  for (const v of health.edge_distribution.values) {
    const edge = v.net_edge_pct;
    if (edge === null || edge === undefined) continue;
    if (edge <= 5) buckets[0].count++;
    else if (edge <= 10) buckets[1].count++;
    else if (edge <= 15) buckets[2].count++;
    else if (edge <= 20) buckets[3].count++;
    else if (edge <= 30) buckets[4].count++;
    else buckets[5].count++;
  }

  return buckets;
}

function mapTradeHistory(history: HistoryEntry[]): TradeHistoryEntry[] {
  return history.map((h) => {
    const placedDate = h.placed_at ? new Date(h.placed_at) : new Date();
    const timestamp = placedDate.toLocaleDateString("tr-TR", {
      day: "numeric",
      month: "short",
    }) + " " + placedDate.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

    // Compute duration if possible
    let duration = "—";
    if (h.placed_at && h.settled_at) {
      const diff = new Date(h.settled_at).getTime() - new Date(h.placed_at).getTime();
      const hours = Math.floor(diff / (1000 * 60 * 60));
      const mins = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
      duration = hours > 0 ? `${hours}s ${mins}dk` : `${mins}dk`;
    }

    // Compute exit price from pnl and entry
    const stake = h.stake_amount || 10;
    const exitPrice = h.result === "PARTIAL_TP"
      ? Math.min(1.0, h.entry_price * (1.0 + h.realized_pnl / stake))
      : h.result === "WIN"
        ? Math.min(1.0, h.entry_price * (1.0 + h.realized_pnl / stake))
        : Math.max(0, h.entry_price * (1.0 - Math.abs(h.realized_pnl) / stake));

    // closed_at for early exits, settled_at for normal settlements
    const closeDate = h.closed_at || h.settled_at;
    const closedAt = closeDate
      ? new Date(closeDate).toLocaleDateString("tr-TR", {
          day: "numeric",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
        })
      : "—";

    return {
      id: String(h.id),
      timestamp,
      city: h.city,
      side: (h.outcome as "YES" | "NO") || "YES",
      entryPrice: h.entry_price,
      exitPrice: Math.round(exitPrice * 100) / 100,
      pnl: h.realized_pnl,
      result: h.result,
      edge: h.edge ?? (h.roi ? Math.round(h.roi * 10) / 10 : 0),
      duration,
      closedAt,
      closedAtISO: h.closed_at || h.settled_at || null,
      strategy: "SIA",
      conditionId: "—",
      exitType: h.exit_type || "ST",
    };
  });
}

function mapModelScores(weights: Record<string, number | { weight: number; brier_score?: number | null; accuracy?: number | null; num_predictions?: number }>): ModelScore[] {
  const modelNames: Record<string, string> = {
    gfs_seamless: "GFS Seamless",
    ecmwf_ifs04: "ECMWF IFS04",
    ecmwf_aifs025: "ECMWF AIFS",
    gfs025: "GFS 0.25",
    ncep_gfs_seamless: "NCEP GFS",
    ecmwf_seamless: "ECMWF Seamless",
    icon_seamless: "ICON",
    gfs_seamless_04: "GFS 0.04",
  };

  return Object.entries(weights)
    .filter(([k]) => !k.startsWith("_"))
    .sort((a, b) => {
      const wA = typeof a[1] === "object" ? a[1].weight : a[1];
      const wB = typeof b[1] === "object" ? b[1].weight : b[1];
      return wB - wA;
    })
    .map(([key, val]) => {
      const w = typeof val === "object" ? val.weight : val;
      const perf = typeof val === "object" ? val : null;
      return {
        name: modelNames[key] ?? key,
        brierScore: perf?.brier_score ?? null,
        accuracy: perf?.accuracy ?? null,
        weight: Math.round(w * 100),
        trend: "stable" as const,
        sampleCount: perf?.num_predictions ?? 0,
      };
    });
}

// ---- Custom Hook ----

export function useApiData() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [historyStats, setHistoryStats] = useState<HistoryStats | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [weights, setWeights] = useState<Record<string, number | { weight: number; brier_score?: number | null; accuracy?: number | null; num_predictions?: number }>>({});
  const [slippageData, setSlippageData] = useState<SlippageEntry[]>([]);
  const [equityCurve, setEquityCurve] = useState<{ initial: number; points: Array<{ date: string; value: number; pnl: number; count: number }> } | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const healthAbortRef = useRef<AbortController | null>(null);

  const fetchHealth = useCallback(async () => {
    // Separate controller for slow health-check — not aborted by fast refresh
    healthAbortRef.current?.abort();
    const controller = new AbortController();
    healthAbortRef.current = controller;
    try {
      const res = await fetchJson<HealthResponse>("/api/health-check", controller.signal);
      if (!controller.signal.aborted) setHealth(res);
    } catch {
      // silently ignore abort or network errors
    }
  }, []);

  const fetchData = useCallback(async () => {
    // Cancel previous in-flight request
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const [statusRes, signalsRes, historyRes, weightsRes, slippageRes, equityRes] = await Promise.allSettled([
        fetchJson<StatusResponse>("/api/status", controller.signal),
        fetchJson<{ signals: Signal[]; count: number }>("/api/signals", controller.signal),
        fetchJson<{ history: HistoryEntry[]; stats: HistoryStats }>("/api/history", controller.signal),
        fetchJson<Record<string, number | { weight: number; brier_score?: number | null; accuracy?: number | null; num_predictions?: number }>>("/api/asi/weights", controller.signal),
        fetchJson<{ slippage: SlippageEntry[] }>("/api/slippage", controller.signal),
        fetchJson<{ initial: number; points: Array<{ date: string; value: number; pnl: number; count: number }> }>("/api/equity-curve", controller.signal),
      ]);

      if (controller.signal.aborted) return;

      if (statusRes.status === "fulfilled") setStatus(statusRes.value);
      if (signalsRes.status === "fulfilled") setSignals(signalsRes.value.signals ?? []);
      if (historyRes.status === "fulfilled") {
        setHistory(historyRes.value.history ?? []);
        setHistoryStats(historyRes.value.stats ?? null);
      }
      if (weightsRes.status === "fulfilled") setWeights(weightsRes.value);
      if (slippageRes.status === "fulfilled") setSlippageData(slippageRes.value.slippage ?? []);
      if (equityRes.status === "fulfilled") setEquityCurve(equityRes.value);

      setError(null);
      setLastUpdated(new Date());
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      setError(err instanceof Error ? err.message : "API bağlantı hatası");
    } finally {
      if (!controller.signal.aborted) setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    fetchHealth(); // fetch health immediately
    const interval = setInterval(fetchData, REFRESH_INTERVAL);
    const healthInterval = setInterval(fetchHealth, HEALTH_REFRESH_INTERVAL);
    return () => {
      clearInterval(interval);
      clearInterval(healthInterval);
      abortRef.current?.abort();
      healthAbortRef.current?.abort();
    };
  }, [fetchData, fetchHealth]);

  // Map data to UI types
  const kpiData = mapKpiData(status, health, historyStats);
  // Use equity curve from backend (all dates, no 300 limit)
  const portfolioData: PortfolioPoint[] = equityCurve?.points?.map((p) => ({
    date: p.date,
    value: Math.round(p.value),
    drawdown: 0,
  })) ?? mapPortfolioData(status, history);
  const openPositions = mapOpenPositions(signals);
  const activityFeed = mapActivityFeed(signals, history, status, health, weights);
  const edgeDistribution = mapEdgeDistribution(health);
  const tradeHistory = mapTradeHistory(history);
  const modelScores = mapModelScores(weights);

  return {
    status,
    signals,
    history,
    historyStats,
    health,
    weights,
    kpiData,
    portfolioData,
    openPositions,
    activityFeed,
    edgeDistribution,
    tradeHistory,
    modelScores,
    slippageData,
    isLoading,
    error,
    lastUpdated,
  };
}
