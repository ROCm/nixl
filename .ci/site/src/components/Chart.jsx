// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef } from 'react';
import { Box } from '@mui/material';
import { useTheme } from '@mui/material/styles';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import { chartInk } from '../theme';

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

// series: [{name, slot, points: [{x, value, note}]}]; categories: the x values
// in order. One y axis, a crosshair tooltip, and a legend when there is more
// than one series.
export default function TrendChart({ categories, labels, series, format, height = 380, ariaLabel }) {
  const node = useRef(null);
  const chart = useRef(null);
  const mode = useTheme().palette.mode;

  useEffect(() => {
    const instance = echarts.init(node.current);
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(node.current);
    return () => {
      observer.disconnect();
      instance.dispose();
    };
  }, []);

  useEffect(() => {
    const ink = chartInk(mode);
    const text = { color: ink.secondary, fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif' };
    chart.current.setOption(
      {
        animation: false,
        textStyle: text,
        grid: { left: 8, right: 16, top: 16, bottom: series.length > 1 ? 56 : 8, containLabel: true },
        legend: {
          show: series.length > 1,
          type: 'scroll',
          bottom: 0,
          textStyle: { color: ink.secondary },
          pageTextStyle: { color: ink.secondary },
          pageIconColor: ink.secondary,
          pageIconInactiveColor: ink.axis,
        },
        tooltip: {
          trigger: 'axis',
          confine: true,
          backgroundColor: ink.surface,
          borderColor: ink.axis,
          textStyle: { color: ink.primary, fontSize: 12 },
          axisPointer: { type: 'line', lineStyle: { color: ink.axis } },
          valueFormatter: (value) => (value === undefined || value === null ? 'not run' : format(value)),
        },
        xAxis: {
          type: 'category',
          data: categories,
          boundaryGap: false,
          axisLine: { lineStyle: { color: ink.axis } },
          axisTick: { show: false },
          axisLabel: { color: ink.muted, formatter: (value) => (labels && labels[value]) || value },
          axisPointer: { label: { formatter: ({ value }) => (labels && labels[value]) || value } },
        },
        yAxis: {
          type: 'value',
          scale: true,
          axisLabel: { color: ink.muted, formatter: (value) => format(value) },
          splitLine: { lineStyle: { color: ink.grid } },
        },
        series: series.map((entry) => {
          const byX = new Map(entry.points.map((point) => [point.x, point.value]));
          return {
            name: entry.name,
            type: 'line',
            data: categories.map((x) => (byX.has(x) ? byX.get(x) : null)),
            connectNulls: false,
            showSymbol: true,
            symbol: 'circle',
            symbolSize: 8,
            lineStyle: { width: 2, color: ink.series[entry.slot] },
            itemStyle: { color: ink.series[entry.slot], borderColor: ink.surface, borderWidth: 2 },
            emphasis: { focus: 'series' },
          };
        }),
      },
      true,
    );
  }, [categories, labels, series, format, mode]);

  return <Box ref={node} role="img" aria-label={ariaLabel} sx={{ width: '100%', height }} />;
}
