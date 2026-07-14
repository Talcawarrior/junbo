// ==========================================
// Junbo Dashboard — Mock Data & Types
// ==========================================

// ---- Common Types ----

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
  conditionId: string;
  amount: number;
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

// ---- Trade History ----

export type TradeResult = "WIN" | "LOSS";

export interface TradeHistoryEntry {
  id: string;
  timestamp: string;
  city: string;
  side: "YES" | "NO";
  entryPrice: number;
  exitPrice: number;
  pnl: number;
  result: TradeResult;
  edge: number;
  duration: string;
  strategy: string;
  conditionId: string;
}

// ---- Model Performance ----

export interface ModelScore {
  name: string;
  brierScore: number;
  accuracy: number;
  weight: number;
  trend: "up" | "down" | "stable";
  sampleCount: number;
}

export interface ModelComparisonPoint {
  date: string;
  ecmwf: number;
  gfs: number;
  hrrr: number;
  nam: number;
  rap: number;
  sref: number;
  gefs: number;
  icon: number;
}

// ---- Slippage ----

export interface SlippageEntry {
  id: string;
  timestamp: string;
  city: string;
  side: "YES" | "NO";
  expectedPrice: number;
  actualPrice: number;
  estimatedSlippage: number;
  actualSlippage: number;
  slippageCost: number;
  size: string;
}

// ==========================================
// Mock Data
// ==========================================

export const kpiData: KpiData = {
  portfolioValue: 10045.2,
  dailyPnl: 45.2,
  totalPnl: 1045.2,
  openPositions: 6,
  winRate: 74.7,
  winRateLabel: "İyi",
  totalTrades: 87,
  wins: 65,
  losses: 22,
  avgEdge: 6.8,
  sharpeRatio: 1.42,
  maxDrawdown: 3.2,
};

export const portfolioData: PortfolioPoint[] = [
  { date: "16 Ara", value: 9250, drawdown: 0 },
  { date: "18 Ara", value: 9310, drawdown: 0.6 },
  { date: "20 Ara", value: 9280, drawdown: 1.1 },
  { date: "22 Ara", value: 9420, drawdown: 0.3 },
  { date: "24 Ara", value: 9380, drawdown: 0.8 },
  { date: "26 Ara", value: 9510, drawdown: 0 },
  { date: "28 Ara", value: 9470, drawdown: 0.5 },
  { date: "30 Ara", value: 9620, drawdown: 0 },
  { date: "01 Oca", value: 9580, drawdown: 0.4 },
  { date: "03 Oca", value: 9710, drawdown: 0 },
  { date: "05 Oca", value: 9650, drawdown: 1.2 },
  { date: "07 Oca", value: 9780, drawdown: 0.2 },
  { date: "09 Oca", value: 9740, drawdown: 0.6 },
  { date: "11 Oca", value: 9850, drawdown: 0 },
  { date: "13 Oca", value: 9920, drawdown: 0 },
  { date: "15 Oca", value: 10045, drawdown: 0 },
];

export const openPositions: OpenPosition[] = [
  { id: "1", city: "Dallas", side: "YES", entryPrice: 0.62, currentPrice: 0.71, pnl: 1.12, edge: 8.2, timeLeft: "2s 17dk", conditionId: "0x7a3f…e91d", amount: 12.50 },
  { id: "2", city: "Miami", side: "NO", entryPrice: 0.38, currentPrice: 0.29, pnl: 0.9, edge: 6.5, timeLeft: "3s 34dk", conditionId: "0x2b1c…a4f2", amount: 10.00 },
  { id: "3", city: "New York", side: "YES", entryPrice: 0.55, currentPrice: 0.48, pnl: -1.05, edge: 4.1, timeLeft: "4s 51dk", conditionId: "0x9d4e…b7c3", amount: 15.00 },
  { id: "4", city: "Chicago", side: "YES", entryPrice: 0.7, currentPrice: 0.78, pnl: 0.64, edge: 9.8, timeLeft: "1s 8dk", conditionId: "0x1f8a…d5e9", amount: 8.00 },
  { id: "5", city: "Phoenix", side: "NO", entryPrice: 0.25, currentPrice: 0.19, pnl: 1.2, edge: 11.3, timeLeft: "2s 25dk", conditionId: "0x6c2b…f1a7", amount: 10.00 },
  { id: "6", city: "Denver", side: "YES", entryPrice: 0.45, currentPrice: 0.52, pnl: 0.84, edge: 5.7, timeLeft: "3s 42dk", conditionId: "0x3e7d…c8b4", amount: 12.00 },
];

export const activityFeed: ActivityItem[] = [
  { id: "a1", time: "14:30", color: "blue", message: "Dallas için YES bet açıldı: $12.50 @ 0.62 (net edge: 8.2%)" },
  { id: "a2", time: "14:25", color: "purple", message: "SIA ağırlık güncellemesi: ECMWF +2%, GFS -1%" },
  { id: "a3", time: "14:20", color: "gray", message: "5 yeni market tarandı, 2'si filtreli (edge düşük)" },
  { id: "a4", time: "13:45", color: "blue", message: "Miami için NO bet açıldı: $10.00 @ 0.38 (net edge: 6.5%)" },
  { id: "a5", time: "13:30", color: "purple", message: "Model uyumu: ECMWF/GFS/HRRR ortalaması hesaplandı" },
  { id: "a6", time: "13:00", color: "orange", message: "Günlük performans: 2 win, 1 loss, net PnL +$8.40" },
  { id: "a7", time: "12:45", color: "teal", message: "Los Angeles marketi çözüldü: YES kazandı, +$3.20" },
  { id: "a8", time: "12:30", color: "red", message: "Seattle marketi çözüldü: NO kaybetti, -$1.50" },
  { id: "a9", time: "12:10", color: "blue", message: "New York için YES bet açıldı: $15.00 @ 0.55 (net edge: 4.1%)" },
];

export const edgeDistribution: EdgeBucket[] = [
  { range: "0-2%", count: 3 },
  { range: "2-4%", count: 5 },
  { range: "4-6%", count: 9 },
  { range: "6-8%", count: 7 },
  { range: "8-10%", count: 10 },
  { range: "10%+", count: 4 },
];

// ---- Trade History ----

export const tradeHistory: TradeHistoryEntry[] = [
  { id: "t1", timestamp: "15 Oca 14:30", city: "Los Angeles", side: "YES", entryPrice: 0.58, exitPrice: 0.72, pnl: 3.20, result: "WIN", edge: 7.1, duration: "3s 22dk", strategy: "SIA-Aggresif", conditionId: "0xaa1b…3f9c" },
  { id: "t2", timestamp: "15 Oca 12:30", city: "Seattle", side: "NO", entryPrice: 0.35, exitPrice: 0.48, pnl: -1.50, result: "LOSS", edge: 3.2, duration: "4s 15dk", strategy: "SIA-Konservatif", conditionId: "0xbb2c…4a8d" },
  { id: "t3", timestamp: "15 Oca 10:00", city: "Houston", side: "YES", entryPrice: 0.41, exitPrice: 0.63, pnl: 4.40, result: "WIN", edge: 9.4, duration: "5s 08dk", strategy: "SIA-Aggresif", conditionId: "0xcc3d…5b7e" },
  { id: "t4", timestamp: "14 Oca 16:45", city: "Atlanta", side: "NO", entryPrice: 0.52, exitPrice: 0.38, pnl: 2.80, result: "WIN", edge: 6.8, duration: "2s 50dk", strategy: "SIA-Balance", conditionId: "0xdd4e…6c6f" },
  { id: "t5", timestamp: "14 Oca 13:20", city: "Boston", side: "YES", entryPrice: 0.65, exitPrice: 0.51, pnl: -2.10, result: "LOSS", edge: 2.8, duration: "6s 33dk", strategy: "SIA-Konservatif", conditionId: "0xee5f…7d5a" },
  { id: "t6", timestamp: "14 Oca 09:15", city: "San Diego", side: "YES", entryPrice: 0.48, exitPrice: 0.69, pnl: 3.15, result: "WIN", edge: 8.5, duration: "3s 47dk", strategy: "SIA-Aggresif", conditionId: "0xff6a…8e4b" },
  { id: "t7", timestamp: "13 Oca 15:30", city: "Portland", side: "NO", entryPrice: 0.42, exitPrice: 0.31, pnl: 1.65, result: "WIN", edge: 7.3, duration: "2s 18dk", strategy: "SIA-Balance", conditionId: "0x117b…9f3c" },
  { id: "t8", timestamp: "13 Oca 11:00", city: "Las Vegas", side: "YES", entryPrice: 0.55, exitPrice: 0.44, pnl: -1.85, result: "LOSS", edge: 2.1, duration: "7s 05dk", strategy: "SIA-Konservatif", conditionId: "0x228c…a02d" },
  { id: "t9", timestamp: "13 Oca 08:40", city: "Austin", side: "YES", entryPrice: 0.39, exitPrice: 0.58, pnl: 2.85, result: "WIN", edge: 8.9, duration: "4s 22dk", strategy: "SIA-Aggresif", conditionId: "0x339d…b11e" },
  { id: "t10", timestamp: "12 Oca 14:50", city: "Nashville", side: "NO", entryPrice: 0.61, exitPrice: 0.45, pnl: 3.20, result: "WIN", edge: 10.2, duration: "1s 55dk", strategy: "SIA-Balance", conditionId: "0x44ae…c20f" },
  { id: "t11", timestamp: "12 Oca 10:30", city: "Detroit", side: "YES", entryPrice: 0.50, exitPrice: 0.62, pnl: 2.40, result: "WIN", edge: 6.4, duration: "5s 40dk", strategy: "SIA-Aggresif", conditionId: "0x55bf…d30a" },
  { id: "t12", timestamp: "12 Oca 07:15", city: "Charlotte", side: "NO", entryPrice: 0.33, exitPrice: 0.40, pnl: -1.10, result: "LOSS", edge: 1.9, duration: "8s 10dk", strategy: "SIA-Konservatif", conditionId: "0x66c0…e49b" },
  { id: "t13", timestamp: "11 Oca 16:00", city: "Orlando", side: "YES", entryPrice: 0.44, exitPrice: 0.61, pnl: 2.97, result: "WIN", edge: 7.8, duration: "3s 30dk", strategy: "SIA-Balance", conditionId: "0x77d1…f58c" },
  { id: "t14", timestamp: "11 Oca 12:20", city: "Dallas", side: "YES", entryPrice: 0.57, exitPrice: 0.70, pnl: 2.60, result: "WIN", edge: 5.9, duration: "4s 15dk", strategy: "SIA-Aggresif", conditionId: "0x88e2…a67d" },
  { id: "t15", timestamp: "11 Oca 09:00", city: "Phoenix", side: "NO", entryPrice: 0.28, exitPrice: 0.18, pnl: 1.80, result: "WIN", edge: 11.7, duration: "2s 45dk", strategy: "SIA-Aggresif", conditionId: "0x99f3…b76e" },
  { id: "t16", timestamp: "10 Oca 15:10", city: "Denver", side: "YES", entryPrice: 0.60, exitPrice: 0.52, pnl: -1.60, result: "LOSS", edge: 3.5, duration: "6s 20dk", strategy: "SIA-Konservatif", conditionId: "0xaa04…c85f" },
  { id: "t17", timestamp: "10 Oca 11:45", city: "Miami", side: "YES", entryPrice: 0.47, exitPrice: 0.65, pnl: 3.60, result: "WIN", edge: 8.1, duration: "3s 10dk", strategy: "SIA-Balance", conditionId: "0xbb15…d94a" },
  { id: "t18", timestamp: "10 Oca 08:30", city: "Chicago", side: "YES", entryPrice: 0.53, exitPrice: 0.68, pnl: 3.00, result: "WIN", edge: 7.5, duration: "4s 50dk", strategy: "SIA-Aggresif", conditionId: "0xcc26…ea3b" },
  { id: "t19", timestamp: "09 Oca 14:00", city: "New York", side: "NO", entryPrice: 0.55, exitPrice: 0.42, pnl: 2.85, result: "WIN", edge: 9.1, duration: "2s 30dk", strategy: "SIA-Balance", conditionId: "0xdd37…fb2c" },
  { id: "t20", timestamp: "09 Oca 10:15", city: "San Francisco", side: "YES", entryPrice: 0.62, exitPrice: 0.49, pnl: -2.30, result: "LOSS", edge: 2.4, duration: "5s 55dk", strategy: "SIA-Konservatif", conditionId: "0xee48…0c1d" },
];

// ---- Model Performance ----

export const modelScores: ModelScore[] = [
  { name: "ECMWF", brierScore: 0.142, accuracy: 78.2, weight: 28, trend: "up", sampleCount: 1240 },
  { name: "GFS", brierScore: 0.158, accuracy: 75.6, weight: 22, trend: "stable", sampleCount: 1180 },
  { name: "HRRR", brierScore: 0.171, accuracy: 73.1, weight: 18, trend: "up", sampleCount: 980 },
  { name: "NAM", brierScore: 0.189, accuracy: 71.4, weight: 12, trend: "down", sampleCount: 860 },
  { name: "RAP", brierScore: 0.195, accuracy: 70.8, weight: 8, trend: "stable", sampleCount: 720 },
  { name: "SREF", brierScore: 0.203, accuracy: 69.5, weight: 5, trend: "down", sampleCount: 640 },
  { name: "GEFS", brierScore: 0.178, accuracy: 72.3, weight: 4, trend: "up", sampleCount: 560 },
  { name: "ICON", brierScore: 0.211, accuracy: 68.1, weight: 3, trend: "stable", sampleCount: 480 },
];

export const modelComparisonData: ModelComparisonPoint[] = [
  { date: "09 Oca", ecmwf: 0.160, gfs: 0.172, hrrr: 0.185, nam: 0.200, rap: 0.210, sref: 0.218, gefs: 0.192, icon: 0.225 },
  { date: "10 Oca", ecmwf: 0.155, gfs: 0.168, hrrr: 0.180, nam: 0.195, rap: 0.208, sref: 0.215, gefs: 0.188, icon: 0.222 },
  { date: "11 Oca", ecmwf: 0.150, gfs: 0.165, hrrr: 0.178, nam: 0.192, rap: 0.202, sref: 0.210, gefs: 0.185, icon: 0.218 },
  { date: "12 Oca", ecmwf: 0.148, gfs: 0.162, hrrr: 0.175, nam: 0.190, rap: 0.198, sref: 0.208, gefs: 0.182, icon: 0.216 },
  { date: "13 Oca", ecmwf: 0.145, gfs: 0.160, hrrr: 0.174, nam: 0.191, rap: 0.197, sref: 0.206, gefs: 0.180, icon: 0.214 },
  { date: "14 Oca", ecmwf: 0.143, gfs: 0.159, hrrr: 0.172, nam: 0.189, rap: 0.196, sref: 0.204, gefs: 0.179, icon: 0.213 },
  { date: "15 Oca", ecmwf: 0.142, gfs: 0.158, hrrr: 0.171, nam: 0.189, rap: 0.195, sref: 0.203, gefs: 0.178, icon: 0.211 },
];

// ---- Slippage ----

export const slippageData: SlippageEntry[] = [
  { id: "s1", timestamp: "15 Oca 14:30", city: "Dallas", side: "YES", expectedPrice: 0.62, actualPrice: 0.63, estimatedSlippage: 0.008, actualSlippage: 0.010, slippageCost: 0.10, size: "$12.50" },
  { id: "s2", timestamp: "15 Oca 13:45", city: "Miami", side: "NO", expectedPrice: 0.38, actualPrice: 0.39, estimatedSlippage: 0.006, actualSlippage: 0.010, slippageCost: 0.10, size: "$10.00" },
  { id: "s3", timestamp: "15 Oca 12:10", city: "New York", side: "YES", expectedPrice: 0.55, actualPrice: 0.56, estimatedSlippage: 0.007, actualSlippage: 0.010, slippageCost: 0.15, size: "$15.00" },
  { id: "s4", timestamp: "15 Oca 10:00", city: "Houston", side: "YES", expectedPrice: 0.41, actualPrice: 0.42, estimatedSlippage: 0.005, actualSlippage: 0.010, slippageCost: 0.10, size: "$10.00" },
  { id: "s5", timestamp: "14 Oca 16:45", city: "Atlanta", side: "NO", expectedPrice: 0.52, actualPrice: 0.53, estimatedSlippage: 0.009, actualSlippage: 0.010, slippageCost: 0.10, size: "$10.00" },
  { id: "s6", timestamp: "14 Oca 13:20", city: "Boston", side: "YES", expectedPrice: 0.65, actualPrice: 0.66, estimatedSlippage: 0.008, actualSlippage: 0.010, slippageCost: 0.10, size: "$10.00" },
  { id: "s7", timestamp: "14 Oca 09:15", city: "San Diego", side: "YES", expectedPrice: 0.48, actualPrice: 0.49, estimatedSlippage: 0.006, actualSlippage: 0.010, slippageCost: 0.10, size: "$10.00" },
  { id: "s8", timestamp: "13 Oca 15:30", city: "Portland", side: "NO", expectedPrice: 0.42, actualPrice: 0.42, estimatedSlippage: 0.007, actualSlippage: 0.000, slippageCost: 0.00, size: "$10.00" },
  { id: "s9", timestamp: "13 Oca 11:00", city: "Las Vegas", side: "YES", expectedPrice: 0.55, actualPrice: 0.57, estimatedSlippage: 0.009, actualSlippage: 0.020, slippageCost: 0.20, size: "$10.00" },
  { id: "s10", timestamp: "13 Oca 08:40", city: "Austin", side: "YES", expectedPrice: 0.39, actualPrice: 0.40, estimatedSlippage: 0.005, actualSlippage: 0.010, slippageCost: 0.10, size: "$10.00" },
  { id: "s11", timestamp: "12 Oca 14:50", city: "Nashville", side: "NO", expectedPrice: 0.61, actualPrice: 0.62, estimatedSlippage: 0.008, actualSlippage: 0.010, slippageCost: 0.10, size: "$10.00" },
  { id: "s12", timestamp: "12 Oca 10:30", city: "Detroit", side: "YES", expectedPrice: 0.50, actualPrice: 0.51, estimatedSlippage: 0.006, actualSlippage: 0.010, slippageCost: 0.10, size: "$10.00" },
];

export const slippageSummary = {
  avgEstimatedSlippage: 0.007,
  avgActualSlippage: 0.009,
  totalSlippageCost: 1.25,
  worstSlippage: 0.020,
  bestSlippage: 0.000,
  slippageHitRate: 91.7,
};

// ---- Health Check ----

export type HealthVerdict = "healthy" | "degraded" | "critical" | "error";
export type FlagSeverity = "critical" | "warning" | "info";

export interface RedFlag {
  severity: FlagSeverity;
  message: string;
  action: string;
}

export interface PassReason {
  market_id: string;
  edge_pct: number;
  reason: string;
  time: string;
}

export interface DailyPnlPoint {
  date: string;
  pnl: number;
  trades: number;
}

export interface HealthData {
  verdict: HealthVerdict;
  verdict_text: string;
  verdict_color: string;
  activity_24h: {
    bets_opened: number;
    pass_reasons: PassReason[];
    total_analyses: number;
  };
  edge_distribution: {
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
  };
  red_flags: RedFlag[];
  daily_pnl_timeline: DailyPnlPoint[];
}

export const healthData: HealthData = {
  verdict: "healthy",
  verdict_text: "SAĞLIKLI",
  verdict_color: "#22c55e",
  activity_24h: {
    bets_opened: 5,
    total_analyses: 42,
    pass_reasons: [
      { market_id: "0x7a3f…e91d", edge_pct: 3.2, reason: "Edge düşük (minimum %5 gerekli)", time: "15 Oca 10:15" },
      { market_id: "0x2b1c…a4f2", edge_pct: 2.8, reason: "Risk limiti aşılıyor (city_cap=4)", time: "15 Oca 09:40" },
      { market_id: "0x9d4e…b7c3", edge_pct: 1.5, reason: "Edge düşük (minimum %5 gerekli)", time: "15 Oca 08:05" },
      { market_id: "0x1f8a…d5e9", edge_pct: 4.1, reason: "Kelly boyutu $1.20 altında", time: "14 Oca 17:30" },
      { market_id: "0x6c2b…f1a7", edge_pct: 2.3, reason: "Edge düşük (minimum %5 gerekli)", time: "14 Oca 15:10" },
    ],
  },
  edge_distribution: {
    avg_net_edge_pct: 6.8,
    min_net_edge_pct: 2.1,
    max_net_edge_pct: 14.5,
    count: 38,
  },
  summary_all: {
    total_settled: 18,
    wins: 14,
    losses: 4,
    win_rate_pct: 77.8,
    total_pnl: 245.60,
    total_stake: 450.00,
    roi_pct: 54.6,
    avg_net_edge_pct: 6.8,
  },
  red_flags: [
    { severity: "info", message: "Son 24 saatte 42 analiz, sadece 5 bet açıldı (%11.9 conversion).", action: "Normal aralıkta, takip et." },
  ],
  daily_pnl_timeline: [
    { date: "09 Oca", pnl: 28.50, trades: 4 },
    { date: "10 Oca", pnl: 42.00, trades: 5 },
    { date: "11 Oca", pnl: 18.37, trades: 4 },
    { date: "12 Oca", pnl: 31.50, trades: 3 },
    { date: "13 Oca", pnl: -8.35, trades: 4 },
    { date: "14 Oca", pnl: 14.15, trades: 3 },
    { date: "15 Oca", pnl: 45.20, trades: 5 },
  ],
};