(() => {
  const source = document.getElementById("profile-data");
  if (!source || !window.Chart) return;
  const payload = JSON.parse(source.textContent);
  const cssValue = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const colorTokens = {
    "#1d5a48": "--chart-pace",
    "#6757a8": "--chart-violet",
    "#68756f": "--chart-neutral",
    "#d8f36a": "--chart-highlight",
    "#e24b3b": "--chart-heart-rate",
    "#ff5c35": "--chart-accent",
  };
  const datasetColor = (color) => {
    const token = colorTokens[color.toLowerCase()];
    return token ? cssValue(token) : color;
  };
  const datasetBackground = (dataset, chartType) => {
    if (dataset.colors) return dataset.colors.map(datasetColor);
    const color = datasetColor(dataset.color);
    if (dataset.type === "bar" || chartType === "bar") return `${color}cc`;
    return dataset.fill ? `${color}18` : color;
  };
  Chart.defaults.color = cssValue("--muted-foreground");

  const number = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
  const tooltipValue = (value, unit) => `${number.format(value)} ${unit}`;

  payload.charts.forEach((config) => {
    const canvas = document.getElementById(config.id);
    if (!canvas) return;
    const doughnut = config.type === "doughnut";
    const datasets = config.datasets.map((dataset) => ({
      label: dataset.label,
      data: dataset.data,
      type: dataset.type || config.type,
      yAxisID: dataset.axis || "y",
      borderColor: datasetColor(dataset.color),
      backgroundColor: datasetBackground(dataset, config.type),
      borderWidth: doughnut ? 0 : 2,
      borderDash: dataset.dashed ? [6, 5] : undefined,
      fill: Boolean(dataset.fill),
      tension: .25,
      pointRadius: config.labels.length === 1 ? 4 : 1.5,
      pointHoverRadius: 5,
      spanGaps: false,
      borderRadius: dataset.type === "bar" || config.type === "bar" ? 5 : undefined,
      maxBarThickness: 44,
    }));

    const chart = new Chart(canvas, {
      type: config.type,
      data: { labels: config.labels, datasets },
      options: {
        animation: false,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: doughnut ? "nearest" : "index" },
        onHover: (event, elements) => {
          event.native.target.style.cursor = elements.length && config.links.length ? "pointer" : "default";
        },
        onClick: (_event, elements) => {
          if (!elements.length || !config.links.length) return;
          const target = config.links[elements[0].index];
          if (target) window.location.assign(target);
        },
        plugins: {
          legend: {
            display: datasets.length > 1 || doughnut,
            position: "bottom",
            labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true, padding: 16 },
          },
          tooltip: {
            displayColors: datasets.length > 1 || doughnut,
            callbacks: {
              label: (item) => `${item.dataset.label}: ${tooltipValue(item.parsed.y ?? item.parsed, config.unit)}`,
            },
          },
        },
        scales: doughnut ? {} : {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 8, maxRotation: 0 } },
          y: {
            beginAtZero: config.type === "bar",
            border: { display: false },
            grid: { color: cssValue("--chart-grid") },
            ticks: { maxTicksLimit: 6, callback: (value) => number.format(value) },
          },
        },
        cutout: doughnut ? "64%" : undefined,
      },
    });
    canvas.profileChart = chart;
    canvas.profileChartConfig = config;
  });

  document.addEventListener("pacepilot:themechange", () => {
    Chart.defaults.color = cssValue("--muted-foreground");
    document.querySelectorAll("canvas").forEach((canvas) => {
      const chart = canvas.profileChart;
      if (!chart) return;
      canvas.profileChartConfig.datasets.forEach((dataset, index) => {
        chart.data.datasets[index].borderColor = datasetColor(dataset.color);
        chart.data.datasets[index].backgroundColor = datasetBackground(
          dataset,
          canvas.profileChartConfig.type,
        );
      });
      if (chart.options.scales?.y?.grid) chart.options.scales.y.grid.color = cssValue("--chart-grid");
      chart.update("none");
    });
  });
})();
