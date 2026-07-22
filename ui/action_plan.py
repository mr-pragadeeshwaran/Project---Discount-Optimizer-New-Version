"""
action_plan.py — the ONE master sheet: for every product x city, the single thing
to do and how much discount to set, in plain English.

The dashboard already surfaces the two "act now" slices (cut_list, reinvest_list).
This reconciles the decision engine's full verdict (plan/all_cells.csv, which buckets
EVERY cell in a confounder-aware way) with those two lists, so nothing is left
unexplained. Every cell resolves to exactly one of:

    Cut discount            trim a wasteful discount to its target      (bank the rupees)
    Reinvest — add discount discount reliably pays; room below break-even (grow volume)
    Hold — fix availability sales gated by stock, discount is not the lever
    Hold — defend share     losing category share; cutting may worsen it
    Monitor / test          no confident signal yet — watch, do not act

"How much to give" (disc_set) is therefore explicit for all cells:
    cut       -> tgt_disc   (down to the observed floor / break-even)
    reinvest  -> be_disc    (up toward the net-revenue-maximizing level)
    hold/mon  -> cur_disc   (no change — that IS the recommendation)

Faithful to scripts/analysis/discount_plan.py (bucketing + decision_reason) and
scripts/analysis/optimize_plan.py (the act-now reconciliation).
"""
import os
import pandas as pd

# plain-English label per internal action key
ACTION_LABEL = {
    "cut":         "Cut discount",
    "reinvest":    "Reinvest — add discount",
    "hold_defend": "Hold — defend share",
    "hold_stock":  "Hold — fix availability",
    "monitor":     "Monitor / test",
}
# display order: act-now first, then the holds, then monitor
ACTION_ORDER = {"cut": 0, "reinvest": 1, "hold_defend": 2, "hold_stock": 3, "monitor": 4}

# columns of the master sheet, in order (product_id + cell_id first, for lookups/joins)
COLUMNS = ["product_id", "cell_id", "title", "pack", "city", "category", "mrp", "action",
           "disc_now", "disc_set", "price_set", "gain_mo", "confidence", "why"]


def _num(v, default=0.0):
    try:
        f = float(v)
        return default if pd.isna(f) else f
    except (TypeError, ValueError):
        return default


def build_action_plan(run):
    """Return a DataFrame — one row per product x city — with the plain-English
    action, the discount to set, the resulting price, the monthly rupee impact
    (cuts only), confidence and the reason. Raw numeric values; the caller rounds
    for display or CSV as it sees fit."""
    plan = os.path.join(run, "plan")
    ac = pd.read_csv(os.path.join(plan, "all_cells.csv"))

    def _ids(fname):
        p = os.path.join(plan, fname)
        return set(pd.read_csv(p)["cell_id"].astype(str)) if os.path.exists(p) else set()
    cut_ids = _ids("cut_list.csv")
    rein_ids = _ids("reinvest_list.csv")

    rows = []
    for _, r in ac.iterrows():
        cid = str(r["cell_id"])
        cur = _num(r.get("cur_disc"))
        tgt = _num(r.get("tgt_disc"))
        be = _num(r.get("be_disc"))
        mrp = _num(r.get("mrp"))
        head = max(be - cur, 0.0)
        reason = r.get("decision_reason")

        if (r.get("bucket") == "c_waste_cut") or (cid in cut_ids):
            key, set_disc = "cut", tgt
            gain = _num(r.get("net_gain_mo"))          # validated, bankable
            why = reason
        elif cid in rein_ids:
            key, set_disc = "reinvest", (be if be > cur else cur)
            gain = None                                # all_cells net_gain ~0 here; value is volume growth
            why = (f"discount reliably lifts sales and sits ~{head:.0f}pp below its "
                   f"break-even level ({be:.0f}%) — room to invest more profitably")
        elif r.get("bucket") == "b_competitive":
            key, set_disc, gain, why = "hold_defend", cur, None, reason
        elif r.get("bucket") == "a_stock":
            key, set_disc, gain, why = "hold_stock", cur, None, reason
        else:
            key, set_disc, gain, why = "monitor", cur, None, reason

        pack = cid.split("_")[1] if cid.count("_") >= 2 else ""
        price_set = round(mrp * (1 - set_disc / 100.0)) if mrp else ""
        rows.append({
            "product_id": r.get("product_id"), "cell_id": cid,
            "title": r.get("title"), "pack": pack, "city": r.get("city"),
            "category": r.get("category"), "mrp": round(mrp) if mrp else "",
            "action": ACTION_LABEL[key],
            "disc_now": round(cur, 1), "disc_set": round(set_disc, 1),
            "price_set": price_set,
            "gain_mo": round(gain) if gain is not None else "",
            "confidence": r.get("confidence"), "why": why,
            "_order": ACTION_ORDER[key],
            "_gain_sort": gain if gain is not None else float("-inf"),
        })

    out = pd.DataFrame(rows)
    if not len(out):
        return out
    out = out.sort_values(["_order", "_gain_sort", "title", "city"],
                          ascending=[True, False, True, True])
    return out.drop(columns=["_order", "_gain_sort"]).reset_index(drop=True)


def action_counts(df):
    """Small summary: how many cells fall under each action (in display order)."""
    if not len(df):
        return []
    order = {v: ACTION_ORDER[k] for k, v in ACTION_LABEL.items()}
    vc = df["action"].value_counts()
    items = sorted(vc.items(), key=lambda kv: order.get(kv[0], 99))
    return [(a, int(n)) for a, n in items]
