// Tails Constellation workflow results and keeps a live table of forecast passes and their actions.
import { actionState, Client } from "@constellationspace/client";

const client = new Client();
const rows = new Map();
const label = { approved: "APPROVED: act", pending: "held", rejected: "rejected", advice: "advice" };

function render(cursor) {
  console.clear();
  console.log(`Constellation workflow results at ${client.baseUrl}, cursor ${cursor}`);
  console.table([...rows.values()].slice(-30));
}

render("the start");
for await (const entry of client.results({ pollMs: Number(process.env.POLL_SECONDS ?? 5) * 1000 })) {
  for (const { items, run } of entry.parts) {
    for (const item of items) {
      const state = actionState(item);
      rows.set(item.entity.id, {
        workflow: run.workflow,
        entity: item.entity.id,
        satellite: item.entity.satellite_id ?? "",
        antenna: item.entity.antenna_id ?? "",
        at: item.target_at ?? "",
        "p50 GB": item.value?.p50 ?? "",
        action: state ? label[state] : "",
      });
    }
  }
  render(entry.cursor);
}
