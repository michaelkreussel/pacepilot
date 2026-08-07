(() => {
  const dataElement = document.getElementById("activity-detail-data");
  if (!dataElement) return;

  const data = JSON.parse(dataElement.textContent);
  const formatElapsed = (seconds) => {
    const total = Math.max(0, Math.round(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = total % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
      : `${minutes}:${String(remainder).padStart(2, "0")}`;
  };
  const formatPace = (seconds) => {
    if (!Number.isFinite(seconds)) return "–";
    const total = Math.max(0, Math.round(seconds));
    const minutes = Math.floor(total / 60);
    return `${minutes}:${String(total % 60).padStart(2, "0")}`;
  };

  const createChart = (id, values, config) => {
    const canvas = document.getElementById(id);
    if (!canvas || !window.Chart || !values.length) return;
    const paceAxis = config.unit === "min/km";
    new Chart(canvas, {
      type: "line",
      data: {
        datasets: [{
          data: values.map(([x, y]) => ({ x, y })),
          borderColor: config.color,
          backgroundColor: config.fill,
          borderWidth: 2,
          fill: true,
          pointRadius: 0,
          pointHitRadius: 12,
          tension: .22,
        }],
      },
      options: {
        animation: false,
        interaction: { intersect: false, mode: "index" },
        maintainAspectRatio: false,
        parsing: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              title: (items) => items.length ? formatElapsed(items[0].parsed.x) : "",
              label: (item) => `${paceAxis ? formatPace(item.parsed.y) : Math.round(item.parsed.y)} ${config.unit}`,
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            grid: { display: false },
            ticks: { callback: formatElapsed, maxTicksLimit: 7 },
            title: { display: true, text: data.time_axis_label },
          },
          y: {
            reverse: paceAxis,
            border: { display: false },
            grid: { color: "rgba(19, 36, 31, .08)" },
            ticks: { callback: paceAxis ? formatPace : undefined, maxTicksLimit: 6 },
            title: { display: true, text: config.unit },
          },
        },
      },
    });
  };

  createChart("heart-rate-chart", data.series.heart_rate, {
    color: "#e24b3b",
    fill: "rgba(226, 75, 59, .10)",
    unit: "bpm",
  });
  createChart("pace-chart", data.series.pace, {
    color: "#1d5a48",
    fill: "rgba(29, 90, 72, .10)",
    unit: "min/km",
  });
  createChart("speed-chart", data.series.speed, {
    color: "#1d5a48",
    fill: "rgba(29, 90, 72, .10)",
    unit: "km/h",
  });
  createChart("cadence-chart", data.series.cadence, {
    color: "#7a5ad8",
    fill: "rgba(122, 90, 216, .09)",
    unit: data.is_running ? "spm" : "rpm",
  });

  const mapElement = document.getElementById("activity-map");
  if (mapElement && window.L && data.route.length) {
    const map = L.map(mapElement, { preferCanvas: true, scrollWheelZoom: false });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    }).addTo(map);
    const route = L.polyline(data.route, {
      color: "#ff5c35",
      lineCap: "round",
      lineJoin: "round",
      opacity: .95,
      weight: 5,
    }).addTo(map);
    L.circleMarker(data.route[0], {
      color: "#fffefa",
      fillColor: "#1d5a48",
      fillOpacity: 1,
      radius: 7,
      weight: 3,
    }).addTo(map).bindTooltip("Start");
    L.circleMarker(data.route[data.route.length - 1], {
      color: "#fffefa",
      fillColor: "#ff5c35",
      fillOpacity: 1,
      radius: 7,
      weight: 3,
    }).addTo(map).bindTooltip("Ziel");
    map.fitBounds(route.getBounds(), { padding: [28, 28] });
  }
})();
