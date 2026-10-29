/* machine — the node as a picture: CPU and memory bars, one block per pod, pods waiting outside. */
LK.register({
  id: "machine", title: "The machine",
  render(el, S) {
    const t = LK.cursor();
    const cap = S.node.cpu, memCap = S.node.memory;
    if (S.mode === "preview") {
      el.innerHTML = `<div class="muted">Nothing is running. When you press Start, the kit creates the pods of every load on one
        machine of the cluster (${cap} cores, ${LK.fmtMem(memCap)} needed) and you will see them here.</div>`;
      return;
    }
    const cpu = {}, mem = {};
    S.loads.forEach((l) => { cpu[l] = LK.at(l, "cpu", t) || 0; mem[l] = LK.at(l, "memory", t) || 0; });
    const nodeCap = LK.at("_node", "cpu_capacity", t) || cap;
    const nodeMem = LK.at("_node", "memory_capacity", t) || memCap;
    const totC = Object.values(cpu).reduce((a, b) => a + b, 0), totM = Object.values(mem).reduce((a, b) => a + b, 0);
    const seg = (vals, max) => S.loads.map((l) => `<div style="width:${Math.min(100, (vals[l] / max) * 100)}%;background:${LK.color(l)}" title="${LK.esc(l)}"></div>`).join("");
    const full = totC >= 0.9 * nodeCap;
    let html = `<div class="bar-row"><span>CPU</span><div class="bar">${seg(cpu, nodeCap)}</div>
        <span class="v" style="color:${full ? "var(--danger)" : "var(--text-2)"}">${LK.num(totC)} of ${LK.num(nodeCap, 0)} cores${full ? " · full" : ""}</span></div>
      <div class="bar-row"><span>Memory</span><div class="bar">${seg(mem, nodeMem)}</div>
        <span class="v">${LK.fmtMem(totM)} of ${LK.fmtMem(nodeMem)}</span></div>`;
    let waiting = "";
    S.loads.forEach((l) => {
      const type = S.types[l] || "batch";
      const n = Math.round(LK.at(l, "pods", t) || 0), w = Math.round(LK.at(l, "pending", t) || 0);
      const pods = Array.from({ length: Math.min(n, 24) }, () =>
        `<span class="pod" style="background:${LK.color(l)}" title="${LK.esc(l)} pod">${LK.icon[type]}</span>`).join("");
      const extra = n > 24 ? `<span class="muted"> +${n - 24}</span>` : "";
      const what = type === "batch" ? (n ? "" : "no run right now") : "";
      html += `<div class="lane"><span class="ln">${LK.esc(l)} <span class="muted" style="font-size:11px">${S.prio[l] || ""}</span></span>
        <div>${pods}${extra}${!n ? `<span class="muted" style="font-size:12px">${what || "no pods"}</span>` : ""}</div></div>`;
      if (w) waiting += Array.from({ length: Math.min(w, 12) }, () => `<span class="pod wait" title="${LK.esc(l)} pod waiting">${LK.icon.wait}</span>`).join("") +
        `<span style="font-size:12px;color:var(--danger);margin:0 12px 0 4px">${LK.esc(l)}</span>`;
    });
    html += `<div class="waiting"><span class="muted" style="font-size:12px;margin-right:8px">Waiting outside the machine</span>${waiting || '<span class="muted" style="font-size:12px">nobody</span>'}</div>`;
    el.innerHTML = html;
  },
});
