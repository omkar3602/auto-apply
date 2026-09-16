(function () {
  const table = document.getElementById("history-table");
  if (!table) return;

  const selectAll = document.getElementById("select-all-jobs");
  const exportBtn = document.getElementById("export-links-btn");

  function rowCheckboxes() {
    return Array.from(table.querySelectorAll(".job-select"));
  }

  function refreshExportState() {
    const boxes = rowCheckboxes();
    const checkedCount = boxes.filter((b) => b.checked).length;
    exportBtn.disabled = checkedCount === 0;
    if (selectAll) {
      selectAll.checked = boxes.length > 0 && checkedCount === boxes.length;
      selectAll.indeterminate = checkedCount > 0 && checkedCount < boxes.length;
    }
  }

  table.addEventListener("change", (e) => {
    if (e.target.classList.contains("job-select")) refreshExportState();
  });

  if (selectAll) {
    selectAll.addEventListener("change", () => {
      rowCheckboxes().forEach((b) => (b.checked = selectAll.checked));
      refreshExportState();
    });
  }

  // Quotes a CSV field only when it actually needs it (contains a comma,
  // quote, or newline), doubling up any internal quotes per RFC 4180.
  function csvField(value) {
    const s = String(value ?? "");
    if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  }

  exportBtn.addEventListener("click", () => {
    const rows = rowCheckboxes()
      .filter((b) => b.checked)
      .map((b) => [b.dataset.company || "", b.value]);
    if (!rows.length) return;

    const csv = ["Company,Opening link", ...rows.map((r) => r.map(csvField).join(","))].join("\n") + "\n";
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `job-links-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  });

  refreshExportState();
})();
