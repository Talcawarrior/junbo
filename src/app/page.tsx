"use client";

import React, { useState, useEffect, useMemo, useRef } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  BarChart,
  Bar,
  PieChart,
  Pie,
  Cell,
  LineChart,
  Line,
  Legend,
  ResponsiveContainer,
} from "recharts";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  useApiData,
  type KpiData,
  type PortfolioPoint,
  type OpenPosition,
  type ActivityItem,
  type EdgeBucket,
  type TradeHistoryEntry,
  type ModelScore,
  type HealthResponse,
  type Signal,
  type HistoryEntry,
} from "@/lib/api";
import {
  TrendingUp,
  Moon,
  Sun,
  Wallet,
  Activity,
  Target,
  BarChart3,
  History,
  Brain,
  Filter,
  Search,
  ArrowUpRight,
  ArrowDownRight,
  Minus,
  HeartPulse,
  ShieldCheck,
  ShieldAlert,
  ShieldX,
  AlertTriangle,
  Info,
  XCircle,
  Loader2,
  HelpCircle,
  Calendar,
} from "lucide-react";

// ---- Color constants ----
const TEAL = "#20B2AA";
const TEAL_LIGHT = "rgba(32, 178, 170, 0.15)";
const RED = "#FF6B6B";
const RED_LIGHT = "rgba(255, 107, 107, 0.15)";
const GREEN = "#22C55E";
const GREEN_LIGHT = "rgba(34, 197, 94, 0.15)";
const TEXT_PRIMARY = "#374151";
const TEXT_MUTED = "#9CA3AF";
const BORDER = "#E5E7EB";

const MODEL_COLORS = ["#20B2AA", "#3B82F6", "#8B5CF6", "#F59E0B", "#EF4444", "#EC4899", "#06B6D4", "#6B7280"];

const dotColorMap: Record<ActivityItem["color"], string> = {
  blue: "#3B82F6",
  purple: "#8B5CF6",
  gray: "#9CA3AF",
  orange: "#F59E0B",
  teal: "#20B2AA",
  red: "#FF6B6B",
};

function fmtUsd(v: number) {
  const sign = v >= 0 ? "+" : "";
  return `${sign}$${Math.abs(v).toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// Turkish number format without sign (for prices)
function fmtPrice(v: number) {
  return v.toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Turkish format for general numbers (percentages, ratios)
function fmtNum(v: number, decimals = 2) {
  return v.toLocaleString("tr-TR", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

// Turkish format for integers (thousands separator, no decimals)
function fmtInt(v: number) {
  return v.toLocaleString("tr-TR", { maximumFractionDigits: 0 });
}

// ---- Loading skeleton ----
function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-md bg-gray-200 ${className}`} />;
}
function MetricSkeleton() {
  return <Card className="py-4 gap-2 shadow-sm"><CardContent className="px-4 pb-0 pt-0 space-y-2"><Skeleton className="h-3 w-16" /><Skeleton className="h-5 w-24" /></CardContent></Card>;
}
function MiniMetricSkeleton() {
  return <Card className="py-2 gap-0 shadow-sm"><CardContent className="px-3 pb-0 pt-0 space-y-1.5"><Skeleton className="h-2.5 w-14" /><Skeleton className="h-4 w-20" /></CardContent></Card>;
}
function ChartSkeleton({ height }: { height?: number }) {
  return <div className="flex items-center justify-center w-full" style={{ height: height ?? 260 }}><Skeleton className="w-full h-full rounded-lg" /></div>;
}

// ---- Client-only chart wrapper (skeleton until hydrated) ----
function ChartWrapper({ children, height, width }: { children: React.ReactNode; height?: number; width?: number }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);
  if (!mounted) return <ChartSkeleton height={height} />;
  return <div className="w-full" style={{ height: height ?? 260, width: width ?? "100%" }}>{children}</div>;
}

// ---- Tab type ----
type TabId = "overview" | "trades" | "models" | "health";

const TABS: { id: TabId; label: string; icon: React.ReactNode }[] = [
  { id: "overview", label: "Genel Bakış", icon: <BarChart3 className="h-3.5 w-3.5" /> },
  { id: "trades", label: "İşlem Geçmişi", icon: <History className="h-3.5 w-3.5" /> },
  { id: "models", label: "Model Performansı", icon: <Brain className="h-3.5 w-3.5" /> },
  { id: "health", label: "Sağlık", icon: <HeartPulse className="h-3.5 w-3.5" /> },
];

// ==========================================
// Tooltip Components
// ==========================================
function PortfolioTooltip({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number }>; label?: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-lg text-xs">
      <p className="font-medium text-gray-500 mb-1">{label}</p>
      <p className="font-mono font-semibold" style={{ color: TEAL }}>
        ${payload[0].value.toLocaleString("tr-TR", { minimumFractionDigits: 0 })}
      </p>
    </div>
  );
}

function EdgeTooltip({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number }>; label?: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-lg text-xs">
      <p className="font-medium text-gray-500 mb-1">Edge: {label}</p>
      <p className="font-mono font-semibold" style={{ color: GREEN }}>{payload[0].value} trade</p>
    </div>
  );
}

// Metric info tooltip
function MetricTooltip({ children, title, description, formula, example }: { 
  children: React.ReactNode; 
  title: string; 
  description: string; 
  formula?: string; 
  example?: string; 
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="relative inline-flex" onMouseEnter={() => setShow(true)} onMouseLeave={() => setShow(false)}>
      {children}
      {show && (
        <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-80 rounded-lg border border-gray-200 bg-white px-3 py-2.5 shadow-lg text-xs z-50">
          <div className="flex items-center gap-1.5 mb-1.5">
            <HelpCircle className="h-3.5 w-3.5" style={{ color: TEAL }} />
            <p className="font-semibold text-gray-900">{title}</p>
          </div>
          <p className="text-gray-600 mb-2 text-[11px] leading-relaxed">{description}</p>
          {formula && (
            <div className="mb-2 p-2 rounded bg-gray-50 font-mono text-[10px] text-gray-700">
              <span className="font-medium">Formül:</span> {formula}
            </div>
          )}
          {example && (
            <div className="p-2 rounded bg-teal-50 font-mono text-[10px] text-teal-800">
              <span className="font-medium">Örnek:</span> {example}
            </div>
          )}
          <div className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-gray-200" />
        </div>
      )}
    </div>
  );
}

// ==========================================
// OVERVIEW TAB
// ==========================================
function OverviewTab({ kpiData, portfolioData, openPositions, activityFeed, edgeDistribution, isLoading }: {
  kpiData: KpiData;
  portfolioData: PortfolioPoint[];
  openPositions: OpenPosition[];
  activityFeed: ActivityItem[];
  edgeDistribution: EdgeBucket[];
  isLoading?: boolean;
}) {
  const winLossData = [
    { name: "Kazanan", value: kpiData.wins, color: TEAL },
    { name: "Kaybeden", value: kpiData.losses, color: RED },
  ];

  return (
    <div className="space-y-6">
      {/* KPI Cards — skeleton while loading */}
      {isLoading ? (
        <>
          <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {Array.from({ length: 4 }).map((_, i) => <MetricSkeleton key={i} />)}
          </section>
          <section className="grid grid-cols-3 sm:grid-cols-6 gap-2">
            {Array.from({ length: 6 }).map((_, i) => <MiniMetricSkeleton key={i} />)}
          </section>
        </>
      ) : (
        <>
          {/* Unified Metric Cards - first row */}
          <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {[
              { 
                label: "Portföy Değeri", 
                value: `$${kpiData.portfolioValue.toLocaleString("tr-TR", { minimumFractionDigits: 2 })}`, 
                icon: <Wallet className="h-4 w-4" />, 
                color: TEAL, 
                sub: `Toplam: ${fmtUsd(kpiData.totalPnl)}`,
                tooltip: "Nakit + açık pozisyon PnL = güncel portföy değeri"
              },
              { 
                label: "Bugünkü PnL", 
                value: `${kpiData.dailyPnl >= 0 ? "▲" : "▼"} ${fmtUsd(kpiData.dailyPnl)}`, 
                icon: <TrendingUp className="h-4 w-4" />, 
                color: kpiData.dailyPnl >= 0 ? "#16A34A" : RED, 
                sub: "",
                tooltip: "Son 24 saatteki gerçekleşen + iri PnL"
              },
              { 
                label: "Açık Bahisler", 
                value: fmtInt(kpiData.openPositions), 
                icon: <Activity className="h-4 w-4" />, 
                color: TEXT_PRIMARY, 
                sub: "",
                tooltip: "Henüz çözülmemiş (open/pending) bahis sayısı"
              },
              { 
                label: "Win Rate", 
                value: `%${fmtNum(kpiData.winRate, 1)}`, 
                icon: <Target className="h-4 w-4" />, 
                color: TEXT_PRIMARY, 
                sub: `${fmtInt(kpiData.closedWins)}W / ${fmtInt(kpiData.closedLosses)}L`,
                tooltip: "Kapanan bahislerde kazanan oranı (closed_early dahil). Örn: 30W/20L = %60"
              }, 
            ].map((kpi) => (
              <Card key={kpi.label} className="py-3 gap-2 shadow-sm" style={{ borderColor: BORDER }}>
                <CardContent className="px-3 pb-0 pt-0">
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-[10px] font-medium" style={{ color: TEXT_MUTED }} title={kpi.tooltip}>{kpi.label}</p>
                    <span style={{ color: kpi.color }}>{kpi.icon}</span>
                  </div>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-lg font-bold tabular-nums" style={{ color: kpi.color }}>{kpi.value}</span>
                  </div>
                  {kpi.sub && <p className="text-[10px] mt-0.5 tabular-nums" style={{ color: kpi.color }}>{kpi.sub}</p>}
                </CardContent>
              </Card>
            ))}
          </section>

          {/* Summary row - single row with all 4 cards */}
          <section className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-2">
            <Card className="py-3 gap-2 shadow-sm" style={{ borderColor: BORDER }}>
              <CardContent className="px-3 pb-0 pt-0">
                <div className="flex items-start justify-between gap-2">
                  <p className="text-[10px] font-medium" style={{ color: TEXT_MUTED }} title="Tüm açık pozisyonların toplam stake tutarı (toplam kilitli nakit)">Açık Bet Toplam Değeri</p>
                  <span style={{ color: TEAL }}><Activity className="h-4 w-4" /></span>
                </div>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-lg font-bold tabular-nums" style={{ color: TEAL }}>{fmtUsd(kpiData.openPositionsValue)}</span>
                </div>
                <p className="text-[10px] mt-0.5 tabular-nums" style={{ color: TEXT_MUTED }}>Max: {fmtUsd(kpiData.maxOpenableUsd)}</p>
              </CardContent>
            </Card>
            {/* Total PnL — custom card with breakdown */}
            <Card className="py-3 gap-1 shadow-sm" style={{ borderColor: BORDER }}>
              <CardContent className="px-4 pb-0 pt-0">
                <div className="flex items-start justify-between gap-2">
                  <p className="text-[10px] font-medium" style={{ color: TEXT_MUTED }}>Toplam PnL</p>
                  <span style={{ color: kpiData.totalPnlValue >= 0 ? "#16A34A" : RED }}><TrendingUp className="h-4 w-4" /></span>
                </div>
                <p className="text-lg font-bold tabular-nums" style={{ color: kpiData.totalPnlValue >= 0 ? "#16A34A" : RED }}>
                  {fmtUsd(kpiData.totalPnlValue)}
                </p>
                <div className="flex flex-col gap-0.5 mt-1 text-[10px] tabular-nums">
                  <div className="flex justify-between">
                    <span style={{ color: TEXT_MUTED }}>Kapalı (Realized)</span>
                    <span style={{ color: kpiData.realizedPnl >= 0 ? TEAL : RED }}>
                      {fmtUsd(kpiData.realizedPnl)}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span style={{ color: TEXT_MUTED }}>Açık (Unrealized)</span>
                    <span style={{ color: kpiData.unrealizedPnl >= 0 ? TEAL : RED }}>
                      {fmtUsd(kpiData.unrealizedPnl)}
                    </span>
                  </div>
                </div>
              </CardContent>
            </Card>
            {[
              { 
                label: "Total ROI", 
                value: `${kpiData.totalRoi >= 0 ? "+" : ""}${fmtNum(kpiData.totalRoi)}%`, 
                icon: <TrendingUp className="h-4 w-4" />, 
                color: kpiData.totalRoi >= 0 ? TEAL : RED, 
                sub: "",
                tooltip: "Toplam yatırıma oranlı getiri. Formül: Total PnL / Toplam Stake × 100. Örn: +36.31% = her $100 için $36 kar"
              },
              { 
                label: "Kapalı Bahis", 
                value: fmtInt(kpiData.closedBets), 
                icon: <BarChart3 className="h-4 w-4" />, 
                color: TEXT_PRIMARY, 
                sub: `${fmtInt(kpiData.closedWins)}W / ${fmtInt(kpiData.closedLosses)}L`,
                tooltip: "Sonuçlanan toplam bahis (won+lost+closed_early). 50 = 30 kazanan + 20 kaybeden"
              },
            ].map((kpi) => (
              <Card key={kpi.label} className="py-3 gap-2 shadow-sm" style={{ borderColor: BORDER }}>
                <CardContent className="px-3 pb-0 pt-0">
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-[10px] font-medium" style={{ color: TEXT_MUTED }} title={kpi.tooltip}>{kpi.label}</p>
                    <span style={{ color: kpi.color }}>{kpi.icon}</span>
                  </div>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-lg font-bold tabular-nums" style={{ color: kpi.color }}>{kpi.value}</span>
                  </div>
                  {kpi.sub && <p className="text-[10px] mt-0.5 tabular-nums" style={{ color: kpi.color }}>{kpi.sub}</p>}
                </CardContent>
              </Card>
            ))}
          </section>
        </>
      )}

      {/* Open Positions + Activity Feed — ÜSTTE */}
      <section className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <Card className="lg:col-span-3 shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
          <CardHeader className="pb-0 pt-0 px-5">
            <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Açık Pozisyonlar</CardTitle>
          </CardHeader>
          <CardContent className="px-3">
            <div className="max-h-[380px] overflow-y-auto custom-scroll">
              {openPositions.length === 0 ? (
                <div className="text-center py-10 text-sm" style={{ color: TEXT_MUTED }}>Açık pozisyon yok</div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: TEXT_MUTED }}>Şehir</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-center" style={{ color: TEXT_MUTED }}>H/L</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>°C</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Açılış</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: TEXT_MUTED }}>Yön</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Giriş</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Güncel</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Edge</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Bet</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>PnL</TableHead>
                      <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Kapanış</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {openPositions.map((pos) => (
                      <TableRow key={pos.id}>
                        <TableCell className="font-medium text-sm" style={{ color: TEXT_PRIMARY }}>{pos.city}</TableCell>
                        <TableCell className="text-center">
                          {pos.metric ? (
                            <Badge className="text-[10px] font-bold px-2 py-0.5 h-5" style={{
                              backgroundColor: pos.metric === "temperature_max" ? "#FEF3C7" : "#DBEAFE",
                              color: pos.metric === "temperature_max" ? "#D97706" : "#2563EB",
                              border: `1px solid ${pos.metric === "temperature_max" ? "#F59E0B40" : "#3B82F640"}`
                            }}>{pos.metric === "temperature_max" ? "H" : "L"}</Badge>
                          ) : <span style={{ color: TEXT_MUTED }}>—</span>}
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums" style={{ color: TEXT_PRIMARY }}>
                          {pos.threshold != null ? `${pos.threshold}°` : "—"}
                        </TableCell>
                        <TableCell className="text-right text-[11px] tabular-nums whitespace-nowrap" style={{ color: TEXT_MUTED }}>{pos.openedAt}</TableCell>
                        <TableCell>
                          <Badge className="text-[10px] font-bold px-2 py-0.5 h-5" style={{ backgroundColor: pos.side === "YES" ? TEAL_LIGHT : RED_LIGHT, color: pos.side === "YES" ? TEAL : RED, border: `1px solid ${pos.side === "YES" ? TEAL : RED}33` }}>{pos.side}</Badge>
                        </TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums" style={{ color: TEXT_PRIMARY }}>{fmtPrice(pos.entryPrice)}</TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums" style={{ color: TEXT_PRIMARY }}>{fmtPrice(pos.currentPrice)}</TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums" style={{ color: TEXT_PRIMARY }}>{pos.edge}%</TableCell>
                        <TableCell className="text-right font-mono text-sm tabular-nums" style={{ color: TEXT_PRIMARY }}>{fmtUsd(pos.amount)}</TableCell>
                        <TableCell className="text-right font-mono text-sm font-semibold tabular-nums" style={{ color: pos.pnl >= 0 ? TEAL : RED }}>{fmtUsd(pos.pnl)}</TableCell>
                        <TableCell className="text-right text-[11px] tabular-nums whitespace-nowrap" style={{ color: TEXT_MUTED }}>{pos.timeLeft}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </div>
          </CardContent>
        </Card>

        <Card className="lg:col-span-2 shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
          <CardHeader className="pb-0 pt-0 px-5">
            <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Aktivite Akışı</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <div className="space-y-0 max-h-[380px] overflow-y-auto pr-1 custom-scroll">
              {activityFeed.length === 0 ? (
                <div className="text-center py-10 text-sm" style={{ color: TEXT_MUTED }}>Henüz aktivite yok</div>
              ) : (
                activityFeed.map((item) => (
                  <div key={item.id} className="flex gap-3 py-2.5 border-b last:border-0" style={{ borderColor: `${BORDER}80` }}>
                    <div className="flex flex-col items-center gap-1 pt-1 shrink-0">
                      <div className="h-2 w-2 rounded-full shrink-0" style={{ backgroundColor: dotColorMap[item.color] }} />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs leading-relaxed" style={{ color: TEXT_PRIMARY }}>{item.message}</p>
                    </div>
                    <span className="text-[10px] tabular-nums shrink-0 pt-0.5" style={{ color: TEXT_MUTED }}>{item.time}</span>
                  </div>
                ))
              )}
            </div>
          </CardContent>
        </Card>
      </section>

      {/* Portfolio Chart + Win/Loss Donut — AŞAĞIDA */}
      <section className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        <Card className="lg:col-span-3 shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
          <CardHeader className="pb-0 pt-0 px-5">
            <div className="flex items-center gap-2">
              <BarChart3 className="h-4 w-4" style={{ color: TEXT_MUTED }} />
              <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Portföy Değeri (30 Gün)</CardTitle>
            </div>
          </CardHeader>
          <CardContent className="px-4">
            <ChartWrapper height={280}>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={portfolioData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                  <defs>
                    <linearGradient id="portfolioGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={TEAL} stopOpacity={0.25} />
                      <stop offset="95%" stopColor={TEAL} stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={BORDER} vertical={false} />
                  <XAxis dataKey="date" tick={{ fontSize: 11, fill: TEXT_MUTED }} axisLine={{ stroke: BORDER }} tickLine={false} />
                  <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11, fill: TEXT_MUTED }} axisLine={false} tickLine={false} tickFormatter={(v: number) => `$${(v / 1000).toFixed(1)}k`} width={45} />
                  <Tooltip content={<PortfolioTooltip />} />
                  <Area type="monotone" dataKey="value" stroke={TEAL} strokeWidth={2} fill="url(#portfolioGradient)" dot={false} activeDot={{ r: 4, stroke: TEAL, strokeWidth: 2, fill: "#fff" }} />
                </AreaChart>
              </ResponsiveContainer>
            </ChartWrapper>
          </CardContent>
        </Card>

        {/* Win/Loss Donut */}
        <Card className="shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
          <CardHeader className="pb-0 pt-0 px-5">
            <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Win / Loss</CardTitle>
          </CardHeader>
          <CardContent className="px-4 flex flex-col items-center justify-center">
            <ChartWrapper height={180} width={180}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={winLossData} cx="50%" cy="50%" innerRadius={50} outerRadius={75} paddingAngle={3} dataKey="value" strokeWidth={0}>
                    {winLossData.map((entry, i) => <Cell key={i} fill={entry.color} />)}
                  </Pie>
                  <Tooltip />
                </PieChart>
              </ResponsiveContainer>
            </ChartWrapper>
            <div className="flex gap-6 mt-3 text-xs">
              <div className="flex items-center gap-1.5">
                <div className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: TEAL }} />
                <span style={{ color: TEXT_MUTED }}>Kazanan: <b style={{ color: TEXT_PRIMARY }}>{fmtInt(kpiData.wins)}</b></span>
              </div>
              <div className="flex items-center gap-1.5">
                <div className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: RED }} />
                <span style={{ color: TEXT_MUTED }}>Kaybeden: <b style={{ color: TEXT_PRIMARY }}>{fmtInt(kpiData.losses)}</b></span>
              </div>
            </div>
          </CardContent>
        </Card>
      </section>

      {/* Edge Distribution */}
      <Card className="shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
        <CardHeader className="pb-0 pt-0 px-5">
          <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Edge Dağılımı</CardTitle>
        </CardHeader>
        <CardContent className="px-4">
          <ChartWrapper height={220}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={edgeDistribution} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke={BORDER} vertical={false} />
                <XAxis dataKey="range" tick={{ fontSize: 11, fill: TEXT_MUTED }} axisLine={{ stroke: BORDER }} tickLine={false} />
                <YAxis tick={{ fontSize: 11, fill: TEXT_MUTED }} axisLine={false} tickLine={false} width={30} />
                <Tooltip content={<EdgeTooltip />} cursor={{ fill: "rgba(0,0,0,0.04)" }} />
                <Bar dataKey="count" fill={GREEN} radius={[4, 4, 0, 0]} barSize={40} />
              </BarChart>
            </ResponsiveContainer>
          </ChartWrapper>
        </CardContent>
      </Card>
    </div>
  );
}

// ==========================================
// TRADE HISTORY TAB
// ==========================================
function TradesTab({ tradeHistory, historyStats, totalPnl }: { tradeHistory: TradeHistoryEntry[]; historyStats: HistoryStats | null; totalPnl: number }) {
  const [filterResult, setFilterResult] = useState<"ALL" | "WIN" | "LOSS" | "PARTIAL_TP">("ALL");
  const [filterSide, setFilterSide] = useState<"ALL" | "YES" | "NO">("ALL");
  const [filterExit, setFilterExit] = useState<"ALL" | "ST" | "TP" | "SL" | "TS" | "TD">("ALL");
  const [filterDate, setFilterDate] = useState<string>("");
  const dateInputRef = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState("");
  const [sortBy, setSortBy] = useState<"date" | "pnl" | "edge">("date");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const filtered = useMemo(() => {
    let data = [...tradeHistory];
    // Varsayılan olarak son 10 günün kapananlarını göster
    const tenDaysAgo = new Date();
    tenDaysAgo.setDate(tenDaysAgo.getDate() - 10);
    data = data.filter((t) => {
      if (t.result === "PARTIAL_TP") return true; // Açık pozisyonlar her zaman göster
      if (!t.closedAtISO) return false;
      return new Date(t.closedAtISO) >= tenDaysAgo;
    });
    if (filterResult !== "ALL") data = data.filter((t) => t.result === filterResult);
    if (filterSide !== "ALL") data = data.filter((t) => t.side === filterSide);
    if (filterExit !== "ALL") data = data.filter((t) => t.exitType === filterExit);
    if (filterDate) {
      data = data.filter((t) => {
        if (!t.closedAtISO) return false;
        const iso = t.closedAtISO.slice(0, 10);
        return iso === filterDate;
      });
    }
    if (search) data = data.filter((t) => t.city.toLowerCase().includes(search.toLowerCase()) || t.strategy.toLowerCase().includes(search.toLowerCase()));
    data.sort((a, b) => {
      let cmp = 0;
      if (sortBy === "date") cmp = 0;
      else if (sortBy === "pnl") cmp = a.pnl - b.pnl;
      else if (sortBy === "edge") cmp = a.edge - b.edge;
      return sortDir === "desc" ? -cmp : cmp;
    });
    return data;
  }, [tradeHistory, filterResult, filterSide, filterExit, filterDate, search, sortBy, sortDir]);

  // Summary stats from API (all settled bets, not just the 50 displayed)
  const hs = historyStats;
  const totalSettledBets = (hs?.total_won ?? 0) + (hs?.total_lost ?? 0);
  const totalSettledWinRate = totalSettledBets > 0 ? ((hs?.total_won ?? 0) / totalSettledBets) * 100 : 0;
  // Filtered stats for the displayed subset
  const filteredPnl = filtered.reduce((s, t) => s + t.pnl, 0);
  const winCount = filtered.filter((t) => t.result === "WIN").length;

  function toggleSort(col: "date" | "pnl" | "edge") {
    if (sortBy === col) setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    else { setSortBy(col); setSortDir("desc"); }
  }

  const SortIcon = ({ col }: { col: "date" | "pnl" | "edge" }) => {
    if (sortBy !== col) return <Minus className="h-3 w-3 inline ml-1 opacity-30" />;
    return sortDir === "desc" ? <ArrowDownRight className="h-3 w-3 inline ml-1" /> : <ArrowUpRight className="h-3 w-3 inline ml-1" />;
  };

  return (
    <div className="space-y-4">
      {/* Summary cards — show ALL settled bets from API, not just filtered */}
      <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Card className="py-3 gap-1 shadow-sm" style={{ borderColor: BORDER }}>
          <CardContent className="px-4 pb-0 pt-0">
            <p className="text-[11px] font-medium" style={{ color: TEXT_MUTED }}>Toplam İşlem</p>
            <p className="text-lg font-bold tabular-nums" style={{ color: TEXT_PRIMARY }}>{fmtInt(totalSettledBets)}</p>
          </CardContent>
        </Card>
        <Card className="py-3 gap-1 shadow-sm" style={{ borderColor: BORDER }}>
          <CardContent className="px-4 pb-0 pt-0">
            <p className="text-[11px] font-medium" style={{ color: TEXT_MUTED }}>Toplam PnL</p>
            <p className="text-lg font-bold tabular-nums" style={{ color: totalPnl >= 0 ? TEAL : RED }}>{fmtUsd(totalPnl)}</p>
          </CardContent>
        </Card>
        <Card className="py-3 gap-1 shadow-sm" style={{ borderColor: BORDER }}>
          <CardContent className="px-4 pb-0 pt-0">
            <p className="text-[11px] font-medium" style={{ color: TEXT_MUTED }}>Win Oranı</p>
            <p className="text-lg font-bold tabular-nums" style={{ color: TEXT_PRIMARY }}>{totalSettledBets > 0 ? fmtNum(totalSettledWinRate, 1) : 0}%</p>
          </CardContent>
        </Card>
        <Card className="py-3 gap-1 shadow-sm" style={{ borderColor: BORDER }}>
          <CardContent className="px-4 pb-0 pt-0">
            <p className="text-[11px] font-medium" style={{ color: TEXT_MUTED }}>Ort. Edge</p>
            <p className="text-lg font-bold tabular-nums" style={{ color: TEXT_PRIMARY }}>{hs?.avg_edge != null ? fmtNum(hs.avg_edge, 1) : "—"}%</p>
          </CardContent>
        </Card>
      </section>

      {/* Filters */}
      <Card className="py-3 gap-2 shadow-sm" style={{ borderColor: BORDER }}>
        <CardContent className="px-4 pt-0">
          <div className="flex flex-wrap items-center gap-3">
            <div className="relative flex-1 min-w-[180px] max-w-xs">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-400" />
              <input
                type="text"
                placeholder="Şehir veya strateji ara..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full pl-8 pr-3 py-1.5 text-xs border rounded-md bg-white focus:outline-none focus:ring-1 focus:ring-teal-300"
                style={{ borderColor: BORDER, color: TEXT_PRIMARY }}
              />
            </div>
            <div className="flex items-center gap-1.5">
              <Filter className="h-3.5 w-3.5" style={{ color: TEXT_MUTED }} />
              <span className="text-[11px] font-medium" style={{ color: TEXT_MUTED }}>Sonuç:</span>
              {(["ALL", "WIN", "PARTIAL_TP", "LOSS"] as const).map((v) => (
                <button key={v} onClick={() => setFilterResult(v)}
                  className="px-2.5 py-1 text-[11px] font-medium rounded-md border transition-colors"
                  style={{
                    borderColor: filterResult === v ? TEAL : BORDER,
                    backgroundColor: filterResult === v ? TEAL_LIGHT : "transparent",
                    color: filterResult === v ? TEAL : TEXT_MUTED,
                  }}>
                  {v === "ALL" ? "Tümü" : v === "WIN" ? "Kazanan" : v === "PARTIAL_TP" ? "◐ PT" : "Kaybeden"}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] font-medium" style={{ color: TEXT_MUTED }}>Taraf:</span>
              {(["ALL", "YES", "NO"] as const).map((v) => (
                <button key={v} onClick={() => setFilterSide(v)}
                  className="px-2.5 py-1 text-[11px] font-medium rounded-md border transition-colors"
                  style={{
                    borderColor: filterSide === v ? TEAL : BORDER,
                    backgroundColor: filterSide === v ? TEAL_LIGHT : "transparent",
                    color: filterSide === v ? TEAL : TEXT_MUTED,
                  }}>
                  {v === "ALL" ? "Tümü" : v}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] font-medium" style={{ color: TEXT_MUTED }}>Neden:</span>
              {([
                { value: "ALL" as const, label: "Tümü" },
                { value: "ST" as const, label: "Settlement" },
                { value: "TP" as const, label: "Take Profit" },
                { value: "SL" as const, label: "Stop Loss" },
                { value: "TS" as const, label: "Trailing Stop" },
                { value: "TD" as const, label: "Time Decay" },
              ]).map((v) => (
                <button key={v.value} onClick={() => setFilterExit(v.value)}
                  className="px-2.5 py-1 text-[11px] font-medium rounded-md border transition-colors"
                  style={{
                    borderColor: filterExit === v.value ? TEAL : BORDER,
                    backgroundColor: filterExit === v.value ? TEAL_LIGHT : "transparent",
                    color: filterExit === v.value ? TEAL : TEXT_MUTED,
                  }}>
                  {v.label}
                </button>
              ))}
            </div>
            {/* Tümünü Temizle — only when any filter is active */}
            {(filterResult !== "ALL" || filterSide !== "ALL" || filterExit !== "ALL" || filterDate !== "" || search !== "") && (
              <button onClick={() => { setFilterResult("ALL"); setFilterSide("ALL"); setFilterExit("ALL"); setFilterDate(""); setSearch(""); }}
                className="px-2.5 py-1 text-[11px] font-medium rounded-md border transition-colors hover:bg-red-50"
                style={{ borderColor: "#FCA5A5", color: RED }}>
                ✕ Tümünü Temizle
              </button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Table */}
      <Card className="shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
        <CardHeader className="pb-0 pt-0 px-5">
          <div className="flex items-center justify-between gap-4">
            <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>
              İşlem Geçmişi
              <span className="ml-2 text-[11px] font-normal" style={{ color: TEXT_MUTED }}>({fmtInt(filtered.length)} kayıt)</span>
            </CardTitle>
            <div className="flex items-center gap-3">
              {/* Filtered PnL display */}
              <div className="flex items-center gap-1.5">
                <span className="text-[10px] font-medium" style={{ color: TEXT_MUTED }}>Filtre PnL:</span>
                <span className="text-sm font-bold tabular-nums" style={{ color: filteredPnl >= 0 ? TEAL : RED }}>
                  {filteredPnl >= 0 ? "+" : ""}{fmtNum(filteredPnl, 2)} USD
                </span>
              </div>
              {/* Date picker — hidden native input, clickable display */}
              <div className="flex items-center gap-1.5">
                <Calendar className="h-3.5 w-3.5" style={{ color: TEXT_MUTED }} />
                <input
                  ref={dateInputRef}
                  type="date"
                  value={filterDate}
                  onChange={(e) => setFilterDate(e.target.value)}
                  className="sr-only"
                  tabIndex={-1}
                />
                <button
                  onClick={() => dateInputRef.current?.showPicker?.()}
                  className="text-[11px] px-2 py-1 border rounded-md bg-white focus:outline-none focus:ring-1 focus:ring-teal-300 tabular-nums text-left"
                  style={{ borderColor: BORDER, color: filterDate ? TEXT_PRIMARY : TEXT_MUTED, minWidth: 110 }}
                >
                  {filterDate ? (() => {
                    const [y, m, d] = filterDate.split("-");
                    return `${d}.${m}.${y}`;
                  })() : "GG.AA.YYYY"}
                </button>
                {filterDate && (
                  <button onClick={() => setFilterDate("")}
                    className="text-[10px] px-1.5 py-0.5 rounded border transition-colors hover:bg-red-50"
                    style={{ borderColor: "#FCA5A5", color: RED }}>
                    ✕
                  </button>
                )}
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent className="px-3">
          <div className="max-h-[500px] overflow-y-auto custom-scroll">
            {filtered.length === 0 ? (
              <div className="text-center py-10 text-sm" style={{ color: TEXT_MUTED }}>İşlem geçmişi bulunamadı</div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider cursor-pointer select-none" style={{ color: TEXT_MUTED }} onClick={() => toggleSort("date")}>Tarih <SortIcon col="date" /></TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: TEXT_MUTED }}>Şehir</TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: TEXT_MUTED }}>Taraf</TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Giriş</TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Çıkış</TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right cursor-pointer select-none" style={{ color: TEXT_MUTED }} onClick={() => toggleSort("pnl")}>PnL <SortIcon col="pnl" /></TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: TEXT_MUTED }}>Sonuç</TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right cursor-pointer select-none" style={{ color: TEXT_MUTED }} onClick={() => toggleSort("edge")}>Edge <SortIcon col="edge" /></TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-center" style={{ color: TEXT_MUTED }}>Neden</TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: TEXT_MUTED }}>Kapanış</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((t) => (
                    <TableRow key={t.id}>
                      <TableCell className="text-xs tabular-nums" style={{ color: TEXT_MUTED }}>{t.timestamp}</TableCell>
                      <TableCell className="font-medium text-sm" style={{ color: TEXT_PRIMARY }}>{t.city}</TableCell>
                      <TableCell>
                        <Badge className="text-[10px] font-bold px-2 py-0.5 h-5" style={{ backgroundColor: t.side === "YES" ? TEAL_LIGHT : RED_LIGHT, color: t.side === "YES" ? TEAL : RED, border: `1px solid ${t.side === "YES" ? TEAL : RED}33` }}>{t.side}</Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono text-sm tabular-nums" style={{ color: TEXT_PRIMARY }}>{fmtPrice(t.entryPrice)}</TableCell>
                      <TableCell className="text-right font-mono text-sm tabular-nums" style={{ color: TEXT_PRIMARY }}>{fmtPrice(t.exitPrice)}</TableCell>
                      <TableCell className="text-right font-mono text-sm font-semibold tabular-nums" style={{ color: t.pnl >= 0 ? TEAL : RED }}>{fmtUsd(t.pnl)}</TableCell>
                      <TableCell>
                        <Badge className="text-[10px] font-bold px-2 py-0.5 h-5" style={{ backgroundColor: t.result === "WIN" ? GREEN_LIGHT : t.result === "PARTIAL_TP" ? "#FFF7ED" : RED_LIGHT, color: t.result === "WIN" ? "#16A34A" : t.result === "PARTIAL_TP" ? "#D97706" : RED, border: `1px solid ${t.result === "WIN" ? "#16A34A" : t.result === "PARTIAL_TP" ? "#D97706" : RED}33` }}>
                          {t.result === "WIN" ? "✓ WIN" : t.result === "PARTIAL_TP" ? "◐ PT" : "✗ LOSS"}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right font-mono text-sm tabular-nums" style={{ color: TEXT_PRIMARY }}>{t.edge}%</TableCell>
                      <TableCell className="text-center">
                        {(() => {
                          const exitLabels: Record<string, { label: string; color: string; bg: string }> = {
                            ST: { label: "ST", color: "#6B7280", bg: "#F3F4F6" },
                            TP: { label: "TP", color: "#16A34A", bg: "#DCFCE7" },
                            PT: { label: "PT", color: "#D97706", bg: "#FFF7ED" },
                            SL: { label: "SL", color: "#DC2626", bg: "#FEE2E2" },
                            TS: { label: "TS", color: "#D97706", bg: "#FEF3C7" },
                            TD: { label: "TD", color: "#7C3AED", bg: "#EDE9FE" },
                          };
                          const e = exitLabels[t.exitType] || exitLabels.ST;
                          return (
                            <Badge className="text-[10px] font-bold px-2 py-0.5 h-5" style={{ backgroundColor: e.bg, color: e.color, border: `1px solid ${e.color}33` }}>
                              {e.label}
                            </Badge>
                          );
                        })()}
                      </TableCell>
                      <TableCell className="text-xs tabular-nums whitespace-nowrap" style={{ color: TEXT_MUTED }}>{t.closedAt}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ==========================================
// MODEL PERFORMANCE TAB
// ==========================================
function ModelsTab({ modelScores }: { modelScores: ModelScore[] }) {
  return (
    <div className="space-y-6">
      {modelScores.length === 0 ? (
        <div className="text-center py-20 text-sm" style={{ color: TEXT_MUTED }}>
          <Brain className="h-8 w-8 mx-auto mb-3 opacity-40" />
          <p>Model verisi henüz yüklenmedi</p>
          <p className="text-xs mt-1">Bot başlatıldığında ağırlıklar burada görünecek</p>
        </div>
      ) : (
        <>
          {/* Model Score Cards */}
          <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            {modelScores.map((m, i) => (
              <Card key={m.name} className="py-4 gap-2 shadow-sm" style={{ borderColor: BORDER }}>
                <CardContent className="px-4 pb-0 pt-0">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <div className="h-3 w-3 rounded-full" style={{ backgroundColor: MODEL_COLORS[i % MODEL_COLORS.length] }} />
                      <p className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>{m.name}</p>
                    </div>
                    {m.trend === "up" && <ArrowUpRight className="h-4 w-4 text-green-500" />}
                    {m.trend === "down" && <ArrowDownRight className="h-4 w-4 text-red-500" />}
                    {m.trend === "stable" && <Minus className="h-4 w-4" style={{ color: TEXT_MUTED }} />}
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-2">
                    <div>
                      <p className="text-[10px]" style={{ color: TEXT_MUTED }}>Ağırlık</p>
                      <p className="text-sm font-bold font-mono tabular-nums" style={{ color: TEXT_PRIMARY }}>%{m.weight}</p>
                    </div>
                    <div>
                      <p className="text-[10px]" style={{ color: TEXT_MUTED }}>Brier Score</p>
                      <p className="text-sm font-bold font-mono tabular-nums" style={{ color: m.brierScore <= 0.16 ? TEAL : m.brierScore <= 0.19 ? TEXT_PRIMARY : RED }}>
                        {m.brierScore.toFixed(3)}
                      </p>
                    </div>
                  </div>
                  {/* Weight bar */}
                  <div className="mt-2 w-full h-1.5 bg-gray-100 rounded-full overflow-hidden">
                    <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(m.weight, 100)}%`, backgroundColor: MODEL_COLORS[i % MODEL_COLORS.length] }} />
                  </div>
                </CardContent>
              </Card>
            ))}
          </section>

          {/* Weight Distribution Chart */}
          <Card className="shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
            <CardHeader className="pb-0 pt-0 px-5">
              <div className="flex items-center gap-2">
                <Brain className="h-4 w-4" style={{ color: TEXT_MUTED }} />
                <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Model Ağırlık Dağılımı</CardTitle>
              </div>
            </CardHeader>
            <CardContent className="px-4">
              <ChartWrapper height={300}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={modelScores.map(m => ({ name: m.name, weight: m.weight }))} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={BORDER} vertical={false} />
                    <XAxis dataKey="name" tick={{ fontSize: 10, fill: TEXT_MUTED }} axisLine={{ stroke: BORDER }} tickLine={false} angle={-20} textAnchor="end" height={60} />
                    <YAxis tick={{ fontSize: 11, fill: TEXT_MUTED }} axisLine={false} tickLine={false} tickFormatter={(v: number) => `%${v}`} width={40} />
                    <Tooltip contentStyle={{ fontSize: 11, borderRadius: 8, border: `1px solid ${BORDER}` }} />
                    <Bar dataKey="weight" radius={[4, 4, 0, 0]} barSize={36}>
                      {modelScores.map((_, i) => (
                        <Cell key={i} fill={MODEL_COLORS[i % MODEL_COLORS.length]} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </ChartWrapper>
            </CardContent>
          </Card>

          {/* Detailed Comparison Table */}
          <Card className="shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
            <CardHeader className="pb-0 pt-0 px-5">
              <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Model Karşılaştırma Tablosu</CardTitle>
            </CardHeader>
            <CardContent className="px-3">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: TEXT_MUTED }}>Model</TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Brier Score</TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider text-right" style={{ color: TEXT_MUTED }}>Ağırlık</TableHead>
                    <TableHead className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: TEXT_MUTED }}>Trend</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {modelScores.map((m, idx) => (
                    <TableRow key={m.name}>
                      <TableCell className="font-medium text-sm" style={{ color: TEXT_PRIMARY }}>
                        <div className="flex items-center gap-2">
                          <span className="text-[10px] font-bold tabular-nums w-4" style={{ color: TEXT_MUTED }}>#{idx + 1}</span>
                          <div className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: MODEL_COLORS[idx % MODEL_COLORS.length] }} />
                          {m.name}
                        </div>
                      </TableCell>
                      <TableCell className="text-right font-mono text-sm font-semibold tabular-nums" style={{ color: m.brierScore <= 0.16 ? TEAL : m.brierScore <= 0.19 ? TEXT_PRIMARY : RED }}>
                        {m.brierScore.toFixed(3)}
                      </TableCell>
                      <TableCell className="text-right font-mono text-sm tabular-nums" style={{ color: TEXT_PRIMARY }}>%{m.weight}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          {m.trend === "up" && <ArrowUpRight className="h-3.5 w-3.5 text-green-500" />}
                          {m.trend === "down" && <ArrowDownRight className="h-3.5 w-3.5 text-red-500" />}
                          {m.trend === "stable" && <Minus className="h-3.5 w-3.5" style={{ color: TEXT_MUTED }} />}
                          <span className="text-xs" style={{ color: TEXT_MUTED }}>
                            {m.trend === "up" ? "Yükseliyor" : m.trend === "down" ? "Düşüyor" : "Stabil"}
                          </span>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

// ==========================================
// HEALTH TAB
// ==========================================
function HealthTab({ health, kpiData }: { health: HealthResponse | null; kpiData?: KpiData }) {
  const pnlScrollRef = useRef<HTMLDivElement>(null);
  const h = health ?? {
    verdict: "healthy" as const,
    verdict_text: "Veri bekleniyor",
    verdict_color: "#9CA3AF",
    activity_24h: { bets_opened: 0, pass_reasons: [], total_analyses: 0 },
    edge_distribution: { avg_net_edge_pct: 0, min_net_edge_pct: 0, max_net_edge_pct: 0, count: 0 },
    summary_all: { total_settled: 0, wins: 0, losses: 0, win_rate_pct: 0, total_pnl: 0, total_stake: 0, roi_pct: 0, avg_net_edge_pct: 0 },
    red_flags: [],
    daily_pnl_timeline: [],
  };

  const verdictConfig: Record<string, { bg: string; border: string; icon: React.ReactNode }> = {
    healthy: { bg: "rgba(34,197,94,0.08)", border: "rgba(34,197,94,0.3)", icon: <ShieldCheck className="h-6 w-6" style={{ color: "#22c55e" }} /> },
    degraded: { bg: "rgba(245,158,11,0.08)", border: "rgba(245,158,11,0.3)", icon: <ShieldAlert className="h-6 w-6" style={{ color: "#f59e0b" }} /> },
    critical: { bg: "rgba(239,68,68,0.08)", border: "rgba(239,68,68,0.3)", icon: <ShieldX className="h-6 w-6" style={{ color: "#ef4444" }} /> },
    error: { bg: "rgba(239,68,68,0.08)", border: "rgba(239,68,68,0.3)", icon: <XCircle className="h-6 w-6" style={{ color: "#ef4444" }} /> },
  };
  const vc = verdictConfig[h.verdict] ?? verdictConfig.healthy;
  const flagStyle: Record<string, { bg: string; color: string; icon: React.ReactNode }> = {
    critical: { bg: RED_LIGHT, color: RED, icon: <XCircle className="h-3.5 w-3.5" /> },
    warning: { bg: "rgba(245,158,11,0.12)", color: "#d97706", icon: <AlertTriangle className="h-3.5 w-3.5" /> },
    info: { bg: "rgba(59,130,246,0.1)", color: "#3b82f6", icon: <Info className="h-3.5 w-3.5" /> },
  };

  function PnlTooltip({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number; payload?: { wins?: number; losses?: number; total?: number; stake?: number; win_rate?: number; roi?: number } }>; label?: string }) {
    if (!active || !payload?.length) return null;
    const p = payload[0].payload ?? {};
    const total = p.total ?? 0;
    const wins = p.wins ?? 0;
    const losses = p.losses ?? 0;
    const winRate = p.win_rate ?? 0;
    const roi = p.roi ?? 0;
    const stake = p.stake ?? 0;
    return (
      <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-lg text-xs space-y-1">
        <p className="font-medium text-gray-500 mb-1">{label}</p>
        <p className="font-mono font-semibold" style={{ color: payload[0].value >= 0 ? TEAL : RED }}>
          {fmtUsd(payload[0].value)} PnL
        </p>
        {total > 0 && (
          <>
            <p className="text-gray-400">{fmtInt(total)} işlem ({fmtInt(wins)}W / {fmtInt(losses)}L)</p>
            <p className="text-gray-400">Win rate: %{winRate} &middot; ROI: %{roi}</p>
            {stake > 0 && <p className="text-gray-400">Stake: ${stake.toFixed(2)}</p>}
          </>
        )}
        {total === 0 && <p className="text-gray-400">İşlem yok</p>}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Verdict Banner */}
      <div className="rounded-xl border-2 p-5 flex items-center gap-4" style={{ backgroundColor: vc.bg, borderColor: vc.border }}>
        {vc.icon}
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-bold" style={{ color: h.verdict_color }}>{h.verdict_text}</h2>
            <Badge className="text-[10px] px-2 py-0.5 h-5 font-semibold" style={{ backgroundColor: `${h.verdict_color}20`, color: h.verdict_color, border: `1px solid ${h.verdict_color}40` }}>
              {h.verdict === "healthy" ? "Tüm sistemler normal" : h.verdict === "degraded" ? "Dikkat gerekiyor" : h.verdict === "critical" ? "Acil müdahale" : h.verdict === "error" ? "Sistem hatası" : "Veri bekleniyor"}
            </Badge>
          </div>
          <p className="text-xs mt-1" style={{ color: TEXT_MUTED }}>
            {h.red_flags.length === 0 ? "Aktif uyarı yok" : `${fmtInt(h.red_flags.length)} aktif uyarı`}
          </p>
        </div>
      </div>

      {/* 3-Day Summary + 24h Activity + Edge Stats */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* 3-Day Summary — narrow left */}
        <Card className="shadow-sm py-3 gap-2" style={{ borderColor: BORDER }}>
          <CardHeader className="pb-0 pt-0 px-5">
            <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>24 Saatlik Aktivite</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pt-0">
            <div className="grid grid-cols-2 gap-3">
              {[
                { label: "Sonuçlanan", value: fmtInt(h.summary_all.total_settled), color: TEXT_PRIMARY },
                { label: "Kazanan", value: fmtInt(h.summary_all.wins), color: "#16A34A" },
                { label: "Kaybeden", value: fmtInt(h.summary_all.losses), color: RED },
                { label: "Win Rate", value: `%${fmtNum(h.summary_all.win_rate_pct, 1)}`, color: TEXT_PRIMARY },
                { label: "Toplam PnL", value: fmtUsd(h.summary_all.total_pnl), color: h.summary_all.total_pnl >= 0 ? TEAL : RED },
                { label: "ROI", value: `%${fmtNum(h.summary_all.roi_pct, 1)}`, color: TEAL },
              ].map((item) => (
                <div key={item.label}>
                  <p className="text-[10px]" style={{ color: TEXT_MUTED }}>{item.label}</p>
                  <p className="text-sm font-bold tabular-nums" style={{ color: item.color }}>{item.value}</p>
                </div>
              ))}
            </div>

            {/* Kazanan/Kaybeden Exit Type Donut Charts */}
            {(() => {
              const exitColors: Record<string, string> = { TP: "#16A34A", SL: "#DC2626", TS: "#D97706", TD: "#7C3AED", ST: "#6B7280" };
              const exitLabels: Record<string, string> = { TP: "Take Profit", SL: "Stop Loss", TS: "Trailing Stop", TD: "Time Decay", ST: "Settlement" };

              function makePieData(src: Record<string, number>) {
                return Object.entries(src)
                  .filter(([, v]) => v > 0)
                  .map(([k, v]) => ({ name: exitLabels[k] || k, value: v, color: exitColors[k] || "#999" }));
              }

              const winData = makePieData(h.summary_all.wins_by_exit || {});
              const lossData = makePieData(h.summary_all.losses_by_exit || {});
              const winTotal = winData.reduce((s, d) => s + d.value, 0);
              const lossTotal = lossData.reduce((s, d) => s + d.value, 0);

              function DonutChart({ data, total, title, titleColor }: { data: { name: string; value: number; color: string }[]; total: number; title: string; titleColor: string }) {
                if (total === 0) return null;
                return (
                  <div className="mt-4 pt-3 border-t" style={{ borderColor: BORDER }}>
                    <p className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: TEXT_MUTED }}>{title}</p>
                    <div className="flex items-center gap-3">
                      <div className="shrink-0">
                        <PieChart width={110} height={110}>
                          <Pie data={data} cx="50%" cy="50%" innerRadius={30} outerRadius={48} paddingAngle={2} dataKey="value" strokeWidth={0}>
                            {data.map((entry, i) => (
                              <Cell key={i} fill={entry.color} />
                            ))}
                          </Pie>
                        </PieChart>
                      </div>
                      <div className="flex-1 space-y-1">
                        {data.map((d) => (
                          <div key={d.name} className="flex items-center justify-between text-[11px]">
                            <div className="flex items-center gap-1.5">
                              <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: d.color }} />
                              <span style={{ color: TEXT_PRIMARY }}>{d.name}</span>
                            </div>
                            <div className="flex items-center gap-2 tabular-nums" style={{ color: TEXT_MUTED }}>
                              <span className="font-semibold" style={{ color: TEXT_PRIMARY }}>{fmtInt(d.value)}</span>
                              <span className="text-[10px]">({total > 0 ? fmtNum((d.value / total) * 100, 1) : 0}%)</span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                );
              }

              return (
                <>
                  <DonutChart data={winData} total={winTotal} title="Kazanan Dağılımı" titleColor="#16A34A" />
                  <DonutChart data={lossData} total={lossTotal} title="Kaybeden Dağılımı" titleColor={RED} />
                </>
              );
            })()}
          </CardContent>
        </Card>

        {/* 24h Activity — wider center */}
        <Card className="shadow-sm py-4 gap-3 lg:col-span-2" style={{ borderColor: BORDER }}>
          <CardHeader className="pb-0 pt-0 px-5">
            <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>24 Saatlik Aktivite</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <div className="grid grid-cols-2 gap-2">
              <div>
                <p className="text-[10px]" style={{ color: TEXT_MUTED }}>Açılan Bahis</p>
                <p className="text-xl font-bold tabular-nums" style={{ color: TEXT_PRIMARY }}>{fmtInt(h.activity_24h.bets_opened)}</p>
              </div>
              <div>
                <p className="text-[10px]" style={{ color: TEXT_MUTED }}>Toplam Analiz</p>
                <p className="text-xl font-bold tabular-nums" style={{ color: TEXT_PRIMARY }}>{fmtInt(h.activity_24h.total_analyses)}</p>
              </div>
            </div>
            {/* Son Tarama */}
            {h.activity_24h.pass_reasons.length > 0 && (
              <div className="py-1 px-2 rounded" style={{ backgroundColor: `${TEAL_LIGHT}80` }}>
                <p className="text-[10px]" style={{ color: TEXT_MUTED }}>Son Tarama</p>
                <p className="text-xs font-mono tabular-nums" style={{ color: TEAL }}>
                  {h.activity_24h.pass_reasons[0]?.time ? new Date(h.activity_24h.pass_reasons[0].time).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" }) : "—"}
                </p>
              </div>
            )}
            <div className="space-y-0.5">
              <p className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: TEXT_MUTED }}>Pas Geçme Nedenleri</p>
              {h.activity_24h.pass_reasons.length === 0 ? (
                <p className="text-xs py-2" style={{ color: TEXT_MUTED }}>Veri yok</p>
              ) : (
                h.activity_24h.pass_reasons.map((pr, i) => (
                  <div key={i} className="flex items-start gap-2 text-[11px] py-0.5 border-b last:border-0" style={{ borderColor: `${BORDER}60` }}>
                    <span className="tabular-nums shrink-0 pt-0.5 font-mono" style={{ color: TEXT_MUTED, fontSize: 10 }}>{pr.time ? new Date(pr.time).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" }) : "?"}</span>
                    <span style={{ color: TEXT_PRIMARY }} className="flex-1">{pr.reason}</span>
                    <Badge className="text-[9px] px-1.5 py-0 h-4 font-mono shrink-0" style={{ backgroundColor: TEAL_LIGHT, color: TEAL }}>
                      %{fmtNum(pr.edge_pct, 1)}
                    </Badge>
                  </div>
                ))
              )}
            </div>
          </CardContent>
        </Card>

        {/* Edge Stats + Risk Metrics */}
        <Card className="shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
          <CardHeader className="pb-0 pt-0 px-5">
            <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Edge İstatistikleri & Risk Metrikleri</CardTitle>
          </CardHeader>
          <CardContent className="px-4">
            <div className="space-y-4">
              <div>
                <div className="flex justify-between items-baseline mb-1">
                  <p className="text-[10px]" style={{ color: TEXT_MUTED }}>Ort. Net Edge</p>
                  <p className="text-lg font-bold tabular-nums" style={{ color: TEAL }}>%{fmtNum(h.edge_distribution.avg_net_edge_pct, 1)}</p>
                </div>
                <div className="w-full h-2 bg-gray-100 rounded-full overflow-hidden">
                  <div className="h-full rounded-full" style={{ width: `${Math.min(h.edge_distribution.avg_net_edge_pct * 7, 100)}%`, backgroundColor: TEAL }} />
                </div>
              </div>
              {[
                { label: "Min Net Edge", value: `%${fmtNum(h.edge_distribution.min_net_edge_pct, 1)}`, color: TEXT_MUTED },
                { label: "Max Net Edge", value: `%${fmtNum(h.edge_distribution.max_net_edge_pct, 1)}`, color: TEAL },
                { label: "Toplam İşlem", value: fmtInt(h.edge_distribution.count), color: TEXT_PRIMARY },
              ].map((item) => (
                <div key={item.label} className="flex justify-between items-baseline">
                  <p className="text-[11px]" style={{ color: TEXT_MUTED }}>{item.label}</p>
                  <p className="text-sm font-bold tabular-nums" style={{ color: item.color }}>{item.value}</p>
                </div>
              ))}
            </div>
            
            {/* Risk Metrics with Targets */}
            <div className="pt-2 border-t" style={{ borderColor: BORDER }}>
              <p className="text-[10px] font-semibold uppercase tracking-wider mb-3" style={{ color: TEXT_MUTED }}>Risk Metrikleri (Hedef Değerler)</p>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {[
                  { 
                    label: "Sharpe Ratio", 
                    value: kpiData ? fmtNum(kpiData.sharpeRatio) : "—",
                    target: "> 1.0 İyi, > 2.0 Mükemmel",
                    color: kpiData ? (kpiData.sharpeRatio < 0.5 ? RED : kpiData.sharpeRatio < 1.0 ? "#eab308" : kpiData.sharpeRatio < 2.0 ? "#22c55e" : "#16a34a") : TEXT_PRIMARY,
                    tooltip: "Risk-başına getiri. <0.5 zayıf, 0.5-1 orta, >1 iyi, >2 mükemmel"
                  },
                  { 
                    label: "Max Drawdown", 
                    value: kpiData ? `%${fmtNum(kpiData.maxDrawdown)}` : "—",
                    target: "< 5% Mükemmel, < 15% Kabul",
                    color: kpiData ? (kpiData.maxDrawdown < 5 ? "#22c55e" : kpiData.maxDrawdown < 15 ? "#eab308" : RED) : TEXT_PRIMARY,
                    tooltip: "Zirveden dipine en büyük düşüş. <5% mükemmel, 5-15% kabul edilebilir, >15% riskli"
                  },
                  { 
                    label: "Expectancy", 
                    value: kpiData ? `${kpiData.expectancy >= 0 ? "+" : ""}$${Math.abs(kpiData.expectancy).toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—",
                    target: "> $0 Karlı, > $5 Güçlü",
                    color: "#16A34A",
                    tooltip: "Bahis başına beklenen kar. Formül: Total PnL / Kapalı Bahis. >0 karlı strateji, >$5 güçlü"
                  },
                  { 
                    label: "Ort. Bahis", 
                    value: kpiData ? `$${fmtPrice(kpiData.avgBetSize)}` : "—",
                    target: "$20-50 arası optimal",
                    color: TEXT_PRIMARY,
                    tooltip: "Ortalama bahis büyüklüğü. Formül: Toplam Stake / Kapalı Bahis. Çok yüksek = risk, çok düşük = verimsiz"
                  },
                  { 
                    label: "Profit Factor", 
                    value: kpiData ? fmtNum(kpiData.profitFactor) : "—",
                    target: "> 1.5 İyi, > 2.0 Mükemmel",
                    color: TEAL,
                    tooltip: "Kazanan/Kaybeden oranı. Formül: Gross Profit / Gross Loss. <1 zararlı, 1-1.5 zayıf, >1.5 iyi, >2 mükemmel"
                  },
                ].map((metric) => (
                  <div key={metric.label} className="p-3 rounded-lg bg-gray-50/50" style={{ border: `1px solid ${BORDER}` }}>
                    <div className="flex items-start justify-between gap-2">
                      <p className="text-[10px] font-medium" style={{ color: TEXT_MUTED }} title={metric.tooltip}>{metric.label}</p>
                    </div>
                    <div className="flex items-center gap-2 mt-1">
                      <span className="text-lg font-bold tabular-nums" style={{ color: metric.color }}>{metric.value}</span>
                    </div>
                    <p className="text-[9px] mt-1.5" style={{ color: TEXT_MUTED }}>{metric.target}</p>
                  </div>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>
      </section>

      {/* Daily PnL Timeline — scrollable */}
      <Card className="shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
        <CardHeader className="pb-0 pt-0 px-5">
          <div className="flex items-center gap-2">
            <TrendingUp className="h-4 w-4" style={{ color: TEXT_MUTED }} />
            <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Günlük PnL Zaman Çizelgesi</CardTitle>
          </div>
        </CardHeader>
        <CardContent className="px-4">
          <ChartWrapper height={260}>
            {h.daily_pnl_timeline.length === 0 ? (
              <div className="flex items-center justify-center h-full text-sm" style={{ color: TEXT_MUTED }}>Henüz veri yok</div>
            ) : (
              <div className="relative">
                {/* Scrollable chart container */}
                <div ref={pnlScrollRef} className="overflow-x-auto custom-scroll pb-1" style={{ scrollBehavior: "smooth" }}>
                  <div style={{ width: Math.max(h.daily_pnl_timeline.length * 64, 500) }}>
                    <ResponsiveContainer width="100%" height={260}>
                      <BarChart data={h.daily_pnl_timeline} margin={{ top: 5, right: 12, left: 10, bottom: 5 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke={BORDER} vertical={false} />
                        <XAxis dataKey="date" tick={{ fontSize: 11, fill: TEXT_MUTED }} axisLine={{ stroke: BORDER }} tickLine={false} interval={0} angle={-20} textAnchor="end" height={40} />
                        <YAxis tick={{ fontSize: 11, fill: TEXT_MUTED }} axisLine={false} tickLine={false} tickFormatter={(v: number) => `$${v}`} width={50} />
                        <Tooltip content={<PnlTooltip />} cursor={{ fill: "rgba(0,0,0,0.04)" }} />
                        <Bar dataKey="pnl" radius={[4, 4, 0, 0]} barSize={36}>
                          {h.daily_pnl_timeline.map((entry, i) => (
                            <Cell key={i} fill={entry.pnl >= 0 ? TEAL : RED} />
                          ))}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              </div>
            )}
          </ChartWrapper>
        </CardContent>
      </Card>

      {/* Red Flags */}
      <Card className="shadow-sm py-4 gap-3" style={{ borderColor: BORDER }}>
        <CardHeader className="pb-0 pt-0 px-5">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm font-semibold" style={{ color: TEXT_PRIMARY }}>Uyarılar ve Bayraklar</CardTitle>
            <Badge className="text-[10px] px-2 py-0.5 h-5" style={{
              backgroundColor: h.red_flags.length === 0 ? GREEN_LIGHT : RED_LIGHT,
              color: h.red_flags.length === 0 ? "#16A34A" : RED,
            }}>
              {h.red_flags.length === 0 ? "Temiz" : `${fmtInt(h.red_flags.length)} uyarı`}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="px-4">
          {h.red_flags.length === 0 ? (
            <div className="text-center py-6">
              <ShieldCheck className="h-8 w-8 mx-auto mb-2" style={{ color: "#22c55e" }} />
              <p className="text-sm" style={{ color: TEXT_PRIMARY }}>Tüm sistemler sağlıklı</p>
              <p className="text-xs mt-1" style={{ color: TEXT_MUTED }}>Aktif uyarı veya kırmızı bayrak bulunmuyor.</p>
            </div>
          ) : (
            <div className="space-y-3">
              {h.red_flags.map((flag, i) => {
                const fs = flagStyle[flag.severity] ?? flagStyle.info;
                return (
                  <div key={i} className="flex gap-3 p-3 rounded-lg border" style={{ backgroundColor: fs.bg, borderColor: `${fs.color}30` }}>
                    <div className="shrink-0 pt-0.5">{fs.icon}</div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <Badge className="text-[9px] px-1.5 py-0 h-4 font-bold uppercase" style={{ backgroundColor: fs.bg, color: fs.color, border: `1px solid ${fs.color}40` }}>
                          {flag.severity === "critical" ? "KRİTİK" : flag.severity === "warning" ? "UYARI" : "BİLGİ"}
                        </Badge>
                      </div>
                      <p className="text-xs" style={{ color: TEXT_PRIMARY }}>{flag.message}</p>
                      <p className="text-[11px] mt-1" style={{ color: fs.color }}>
                        <span className="font-semibold">Önerilen aksiyon:</span> {flag.action}
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ==========================================
// MAIN DASHBOARD
// ==========================================
export default function DashboardPage() {
  const [activeTab, setActiveTab] = useState<TabId>("overview");
  const [darkMode, setDarkMode] = useState<boolean>(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("darkMode") === "true";
    }
    return false;
  });
  const data = useApiData();

  useEffect(() => {
    const root = document.documentElement;
    if (darkMode) {
      root.classList.add("dark");
    } else {
      root.classList.remove("dark");
    }
    localStorage.setItem("darkMode", String(darkMode));
  }, [darkMode]);

  return (
    <div className="min-h-screen flex flex-col bg-gray-50/50 dark:bg-gray-900/50" style={{ fontFamily: "'Inter', system-ui, sans-serif" }}>
      {/* ---- HEADER ---- */}
      <header className="sticky top-0 z-50 bg-white dark:bg-gray-900 border-b" style={{ borderColor: BORDER }}>
        <div className="max-w-7xl mx-auto flex items-center justify-between px-4 sm:px-6 h-14">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-bold tracking-tight text-gray-900 dark:text-gray-100">Junbo</h1>
            <div className="flex items-center gap-1.5">
              {data.isLoading && !data.status ? (
                <>
                  <Loader2 className="h-3 w-3 animate-spin" style={{ color: TEXT_MUTED }} />
                  <span className="text-xs font-medium" style={{ color: TEXT_MUTED }}>Bağlanıyor...</span>
                </>
              ) : data.status?.is_running ? (
                <>
                  <span className="relative flex h-2 w-2">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
                    <span className="relative inline-flex rounded-full h-2 w-2 bg-green-500" />
                  </span>
                  <span className="text-xs font-medium text-green-600 dark:text-green-400">ÇALIŞIYOR</span>
                </>
              ) : (
                <>
                  <span className="relative flex h-2 w-2">
                    <span className="relative inline-flex rounded-full h-2 w-2 bg-gray-400" />
                  </span>
                  <span className="text-xs font-medium text-gray-500 dark:text-gray-400">DURDURULDU</span>
                </>
              )}
            </div>
            {data.error && (
              <Badge className="text-[10px] px-2 py-0.5 h-5" style={{ backgroundColor: RED_LIGHT, color: RED }}>
                API Hatası
              </Badge>
            )}
          </div>
          <div className="flex items-center gap-2">
            {data.lastUpdated && (
              <span className="text-[10px] tabular-nums text-gray-500 dark:text-gray-400">
                Son güncelleme: {data.lastUpdated.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </span>
            )}
            {data.health?.activity_24h?.pass_reasons?.[0]?.time && (
              <span className="text-[10px] tabular-nums" style={{ color: TEAL }}>
                Son Tarama: {new Date(data.health.activity_24h.pass_reasons[0].time).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" })}
              </span>
            )}
            <button
                className="p-2 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
                onClick={() => setDarkMode((d) => !d)}
              >
                {darkMode ? (
                  <Sun className="h-4 w-4 text-yellow-400" />
                ) : (
                  <Moon className="h-4 w-4 text-gray-500" />
                )}
              </button>
          </div>
        </div>
      </header>

      {/* ---- TAB NAVIGATION ---- */}
      <nav className="bg-white dark:bg-gray-900 border-b sticky top-14 z-40" style={{ borderColor: BORDER }}>
        <div className="max-w-7xl mx-auto px-4 sm:px-6">
          <div className="flex gap-0 overflow-x-auto">
            {TABS.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className="flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap"
                style={{
                  borderColor: activeTab === tab.id ? TEAL : "transparent",
                  color: activeTab === tab.id ? TEAL : TEXT_MUTED,
                }}
              >
                {tab.icon}
                {tab.label}
              </button>
            ))}
          </div>
        </div>
      </nav>

      {/* ---- MAIN CONTENT ---- */}
      <main className="flex-1 max-w-7xl mx-auto w-full px-4 sm:px-6 py-6">
        {activeTab === "overview" && (
          <OverviewTab
            isLoading={data.isLoading && !data.status}
            kpiData={data.kpiData}
            portfolioData={data.portfolioData}
            openPositions={data.openPositions}
            activityFeed={data.activityFeed}
            edgeDistribution={data.edgeDistribution}
          />
        )}
        {activeTab === "trades" && <TradesTab tradeHistory={data.tradeHistory} historyStats={data.historyStats} totalPnl={data.historyStats?.total_pnl ?? 0} />}
        {activeTab === "models" && <ModelsTab modelScores={data.modelScores} />}
        {activeTab === "health" && <HealthTab health={data.health} kpiData={data.kpiData} />}
      </main>

      {/* ---- FOOTER ---- */}
      <footer className="mt-auto py-4 text-center">
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Junbo — Polymarket Hava Ticaret Botu - SIA Modeli ile Otomatik İşlem
        </p>
      </footer>
    </div>
  );
}
