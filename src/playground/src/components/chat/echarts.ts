// The ONLY file permitted to import from `echarts/*`.
//
// Tree-shaken registration. Importing `echarts` wholesale instead pulls ~1MB and
// every chart type we do not use — and it fails *silently*: the chart works, the
// bundle triples. Hence one chokepoint plus a grep and a measured bundle budget,
// rather than vigilance.
//
// The registration list IS the feature list. A kind that is not registered here
// cannot be drawn, which is why `charts.py`'s CHART_KINDS and this file have to
// be changed together.
import { BarChart, LineChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

// CanvasRenderer, NOT SVGRenderer: getDataURL('png') does not work under the SVG
// renderer, and PNG export is a requirement. The cost — chart text is pixels, so
// it is neither selectable nor screen-readable — is paid by the mandatory Table
// view, which is why that view is load-bearing rather than merely correct.
//
// LegendComponent is here for the stacked kinds ONLY; an unstacked chart still
// sets `legend: {show: false}`, because one measure has nothing to key. There is
// no stacked *chart* module to add: stacking is a `stack` property on a bar
// series, so `BarChart` already covers it.
//
// Deliberately unregistered: PieChart (part-to-whole rides the stacked bar),
// ToolboxComponent (the download button is ours, matching the app's chrome),
// TitleComponent (the card header renders the title as real DOM text —
// selectable and searchable, a small win back against the canvas trade-off),
// DatasetComponent, dataZoom, MarkLineComponent.
echarts.use([
  BarChart,
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer,
]);

export default echarts;
