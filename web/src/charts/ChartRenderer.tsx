import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import {
  DataZoomComponent, GridComponent, LegendComponent, TooltipComponent,
} from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import { useEffect, useMemo, useRef } from "react";
import type { ChartSpec } from "../api/types";

echarts.use([
  BarChart, LineChart, DataZoomComponent, GridComponent, LegendComponent,
  TooltipComponent, SVGRenderer,
]);

export function buildChartOption(spec: ChartSpec, rows: Array<Record<string, unknown>>) {
  const categories = spec.x ? rows.map((row) => row[spec.x!]) : rows.map((_row, index) => index + 1);
  return {
    backgroundColor: "transparent",
    animation: false,
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#9da7b3" } },
    grid: { left: 56, right: 24, top: 42, bottom: 52 },
    xAxis: { type: "category", data: categories, axisLabel: { color: "#8d98a5" }, axisLine: { lineStyle: { color: "#303944" } } },
    yAxis: { type: "value", name: spec.unit ?? "", nameTextStyle: { color: "#8d98a5" }, axisLabel: { color: "#8d98a5" }, splitLine: { lineStyle: { color: "#202832" } } },
    dataZoom: rows.length > 20 ? [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }] : [],
    series: spec.y.map((field) => ({ name: field, type: spec.chart_type === "bar" ? "bar" : "line", smooth: false, showSymbol: rows.length < 40, data: rows.map((row) => row[field]), lineStyle: { width: 2 }, areaStyle: spec.chart_type === "line" ? { opacity: 0.08 } : undefined })),
  };
}

export function ChartRenderer({ spec, rows }: { spec: ChartSpec; rows: Array<Record<string, unknown>> }) {
  const node = useRef<HTMLDivElement>(null);
  const option = useMemo(() => buildChartOption(spec, rows), [spec, rows]);
  useEffect(() => {
    if (!node.current || spec.chart_type === "table") return;
    const chart = echarts.init(node.current, undefined, { renderer: "svg" });
    chart.setOption(option);
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); chart.dispose(); };
  }, [option, spec.chart_type]);
  if (spec.chart_type === "table") return <div className="state-panel">ChartSpec selected the audited table view.</div>;
  return <div ref={node} className="chart" role="img" aria-label={`${spec.title} ${spec.chart_type} chart`} />;
}
