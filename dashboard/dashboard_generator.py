import os
import json
import datetime
import pandas as pd
import v4_config as cfg

import numpy as np

class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer): return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        return super(NpEncoder, self).default(obj)


def _load_last_week_performance():
    """Real tracked performance from the weekly scorecard — never mock data.
    Returns {has_data, acted, hit_rate, r2, realized} or {has_data: False} if no actuals yet."""
    try:
        import sys
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.join(_root, "scripts", "tracker"))
        import scorecard as _sc
        hist_path = os.path.join(_root, "output", "DISCOUNT_PLAN", "tracker_history.csv")
        if not os.path.exists(hist_path):
            return {"has_data": False}
        hist = pd.read_csv(hist_path)
        scored = hist[hist["actual_net_rev_delta"].notna()] if "actual_net_rev_delta" in hist else hist.iloc[:0]
        s = _sc.score_history(scored)
        if int(s.get("n_weeks_scored", 0) or 0) == 0:
            return {"has_data": False}
        return {"has_data": True, "acted": int(s.get("n_obs", 0)),
                "hit_rate": float(s.get("hit_rate") or 0), "r2": float(s.get("pred_vs_actual_r2") or 0),
                "realized": float(s.get("cumulative_realized_saving_inr") or 0)}
    except Exception:
        return {"has_data": False}

def _upcoming_events(days_ahead: int = 14) -> list:
    """Real entries from the config event calendar within the next days_ahead
    days. Empty list (bar hidden) when the calendar has nothing upcoming —
    never invented notifications."""
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=days_ahead)
    out = []
    for d, name in sorted(getattr(cfg, "FESTIVAL_DATES", {}).items()):
        try:
            dt = datetime.date.fromisoformat(d)
        except ValueError:
            continue
        if today <= dt <= horizon:
            out.append(f"{name} on {dt.strftime('%d %b')}")
    for (start, end), name in getattr(cfg, "PLATFORM_EVENT_WINDOWS", {}).items():
        try:
            s = datetime.date.fromisoformat(start)
            e = datetime.date.fromisoformat(end)
        except ValueError:
            continue
        if s <= horizon and e >= today:
            out.append(f"{name} ({s.strftime('%d %b')}–{e.strftime('%d %b')})")
    return out


def generate_dashboard(rec_df: pd.DataFrame, output_dir: str, context: dict) -> str:
    """Generates the 4-view HTML dashboard."""
    print("  [Dashboard] Generating 4-view HTML dashboard...")

    # ── Prep data ──────────────────────────────────────────
    tier_counts = rec_df["tier"].value_counts()
    tier_savings = rec_df.groupby("tier")["rec_monthly_savings"].sum()

    current_disc_avg = rec_df["current_discount_pct"].mean()

    # Glide-path status computed from this run's data vs the config target —
    # no hardcoded status claims.
    gap_ppt = current_disc_avg - cfg.TARGET_DISCOUNT_PCT
    if gap_ppt <= 0.5:
        glide_value, glide_cls = "At target ✓", "trend-good"
    else:
        glide_value, glide_cls = f"{gap_ppt:.1f} ppt above target", ""

    # Real upcoming events from the config calendar (bar hidden when empty).
    events = _upcoming_events()
    if events:
        notifications_html = (
            '<div class="notifications"><strong>📅 Event calendar:</strong> '
            + " · ".join(events)
            + "</div>"
        )
    else:
        notifications_html = ""

    # Last week's REAL tracked performance from the weekly scorecard (never mock).
    # Honest "no results yet" state until actuals exist.
    last_week = _load_last_week_performance()
    if last_week and last_week.get("has_data"):
        last_week_html = f"""<div class="card">
            <p>Cells scored: <strong>{last_week['acted']}</strong></p>
            <p>Prediction hit rate: <strong>{last_week['hit_rate']:.0%}</strong></p>
            <p>Predicted vs actual (net-rev) R²: <strong>{last_week['r2']:.2f}</strong></p>
            <p>Realized saving to date: <strong>₹{last_week['realized']:,.0f}</strong></p>
        </div>"""
    else:
        last_week_html = ('<div class="card"><p><em>No tracked results yet — this fills in once the '
                          'weekly tracker has real actuals (from week 2). No mock data is shown.</em></p></div>')

    # Serialize curve data for JS
    curves_data = {}
    for _, row in rec_df.iterrows():
        cid = row["cell_id"]
        curves_data[cid] = {
            "points": row.get("curve_points", []),
            "current_disc": row["current_discount_pct"],
            "elbow_disc": row["elbow_discount_pct"],
            "params": row.get("curve_params", {})
        }

    curves_json_str = json.dumps(curves_data, cls=NpEncoder)

    # ── View 1: Portfolio Summary HTML ─────────────────────
    v1_html = f"""
    <div id="view1" class="view active">
        <h2>Portfolio Summary — Week of {datetime.date.today()}</h2>
        
        <div class="metrics-grid">
            <div class="card">
                <div class="label">Current Discount %</div>
                <div class="value">{current_disc_avg:.1f}%</div>
                <div class="subtext">simple average across {len(rec_df)} cells, this run</div>
            </div>
            <div class="card">
                <div class="label">Target Discount %</div>
                <div class="value">{cfg.TARGET_DISCOUNT_PCT:.1f}%</div>
                <div class="subtext">by {cfg.TARGET_QUARTER}</div>
            </div>
            <div class="card">
                <div class="label">Glide-path Status</div>
                <div class="value {glide_cls}">{glide_value}</div>
                <div class="subtext">recomputed from this run's data</div>
            </div>
        </div>

        <h3 class="mt-4">This Week's Recommendations</h3>
        <div class="metrics-grid">
            <div class="card tier-green">
                <div class="label">Strong Cuts Ready</div>
                <div class="value">{tier_counts.get('Strong Cut', 0)} cells</div>
                <div class="subtext">→ ₹{tier_savings.get('Strong Cut', 0)/100000:.1f}L savings/mo</div>
            </div>
            <div class="card tier-amber">
                <div class="label">Trade-offs (Review)</div>
                <div class="value">{tier_counts.get('Trade-off', 0)} cells</div>
                <div class="subtext">→ ₹{tier_savings.get('Trade-off', 0)/100000:.1f}L savings/mo</div>
            </div>
            <div class="card tier-blue">
                <div class="label">Increase Recommended</div>
                <div class="value">{tier_counts.get('Increase', 0)} cells</div>
                <div class="subtext">→ ₹{tier_savings.get('Increase', 0)/100000:.1f}L margin</div>
            </div>
            <div class="card tier-gray">
                <div class="label">Hold Steady</div>
                <div class="value">{tier_counts.get('Hold', 0)} cells</div>
            </div>
            <div class="card tier-purple">
                <div class="label">Needs Experiment</div>
                <div class="value">{tier_counts.get('Do Not Act', 0)} cells</div>
            </div>
        </div>
        
        <h3 class="mt-4">Last Week's Actions — Performance</h3>
        {last_week_html}
    </div>
    """

    # ── View 2: Action Queue HTML ──────────────────────────
    v2_html = """<div id="view2" class="view" style="display:none;"><h2>Action Queue</h2>"""
    
    for tier in ["Strong Cut", "Trade-off", "Increase"]:
        tier_df = rec_df[rec_df["tier"] == tier]
        if tier_df.empty: continue
        
        v2_html += f"""
        <div class="tier-section tier-{tier.lower().replace(' ', '')}">
            <div class="tier-header flex justify-between">
                <h3>TIER — {tier.upper()} ({len(tier_df)} cells)</h3>
                <span class="subtext">click a row for detail</span>
            </div>
            <table class="w-100">
                <thead>
                    <tr><th>SKU</th><th>City</th><th>Now</th><th>→</th><th>Rec</th><th>Vol Δ</th><th>Rev Δ</th><th>Save/mo</th><th>Conf</th></tr>
                </thead>
                <tbody>
        """
        for _, r in tier_df.iterrows():
            row_json = json.dumps(r.drop(labels=["ladder", "curve_points", "curve_params"]).to_dict()).replace('"', '&quot;')
            v2_html += f"""
            <tr onclick="openCellDetail('{r['cell_id']}', '{row_json}')">
                <td>{r['title'][:30]}...</td>
                <td>{r['city']}</td>
                <td>{r['current_discount_pct']:.0f}%</td>
                <td>→</td>
                <td><strong>{r['rec_discount_pct']:.0f}%</strong></td>
                <td>{r['rec_vol_change_pct']}%</td>
                <td>{r['rec_rev_change_pct']}%</td>
                <td>₹{(r['rec_monthly_savings']/1000):.1f}K</td>
                <td>{r['confidence']}</td>
            </tr>
            """
            # Add warning for trade-offs
            if tier == "Trade-off" and r['rec_vol_change_pct'] < -5:
                v2_html += f"""<tr class="warning-row"><td colspan="9">⚠ Volume drop above 5% — needs your call</td></tr>"""
        v2_html += "</tbody></table></div>"
    
    v2_html += "</div>"

    # ── View 3: Cell Detail HTML (Template) ────────────────
    v3_html = """
    <div id="view3" class="view" style="display:none;">
        <div class="flex justify-between mt-2 mb-2">
            <h2 id="detail-title">SKU — City</h2>
            <div><button onclick="switchView('view2')">Back</button></div>
        </div>
        
        <div class="flex gap-2">
            <div class="card flex-1">
                <h3>CURRENT STATE</h3>
                <p>Price: <strong id="d-c-price"></strong></p>
                <p>Discount: <strong id="d-c-disc"></strong></p>
                <p>Avg daily units: <strong id="d-c-units"></strong></p>
                <p>Avg daily revenue: <strong id="d-c-rev"></strong></p>
                <p>Margin/day: <strong id="d-c-margin"></strong></p>
            </div>
            <div class="card flex-1">
                <h3>RECOMMENDATION</h3>
                <p>Price: <strong id="d-r-price"></strong></p>
                <p>Discount: <strong id="d-r-disc"></strong></p>
                <p>Expected units: <strong id="d-r-units"></strong></p>
                <p>Expected revenue: <strong id="d-r-rev"></strong></p>
                <p>Expected margin: <strong id="d-r-margin"></strong></p>
            </div>
        </div>
        
        <div class="card mt-2">
            <h3>SATURATION CURVE</h3>
            <div id="chart-container" style="height: 250px; background: #f8fafc; border: 1px solid #e2e8f0;">
            </div>
            <p class="subtext">Predicted daily units at each discount level (model curve). Dashed lines mark the current and recommended discount.</p>
        </div>
        
        <div class="card mt-2">
            <h3>WHY THIS RECOMMENDATION</h3>
            <p>At <span id="w-c-disc"></span>% discount, marginal ROI is low.</p>
            <p>At <span id="w-r-disc"></span>% discount, marginal ROI = <strong id="w-r-roi"></strong> (just above threshold 1.0)</p>
            <p>Model confidence for this cell: <strong id="w-conf"></strong></p>
        </div>
        
        <div class="card mt-2">
            <h3>GUARDRAILS APPLIED</h3>
            <p>Floor price check: <span id="g-floor"></span></p>
            <p>Max change rate: <span id="g-change"></span></p>
            <p id="g-throttle" class="trend-bad font-bold"></p>
            <p id="g-phase"></p>
        </div>
    </div>
    """

    # ── View 4: Export HTML ────────────────────────────────
    # Real numbers computed from this run's recommendations — the download
    # button produces an actual CSV of the actionable rows, client-side.
    actionable = rec_df[rec_df["tier"].isin(["Strong Cut", "Trade-off", "Increase"])]
    export_cols = [c for c in ["cell_id", "title", "city", "category",
                               "current_price", "rec_price",
                               "current_discount_pct", "rec_discount_pct",
                               "tier", "confidence", "rec_monthly_savings",
                               "phasing_plan"] if c in actionable.columns]
    export_json_str = json.dumps(
        actionable[export_cols].to_dict(orient="records"), cls=NpEncoder
    )
    act_savings_l = actionable["rec_monthly_savings"].sum() / 100000

    v4_html = f"""
    <div id="view4" class="view" style="display:none;">
        <h2>Export Recommended Changes</h2>
        <div class="card mt-4 text-center">
            <h3>THIS RUN'S ACTIONABLE RECOMMENDATIONS</h3>
            <p class="mt-2">Actionable cells (Strong Cut / Trade-off / Increase): <strong>{len(actionable)}</strong></p>
            <p>Modeled net-revenue impact: <strong>₹{act_savings_l:.1f}L / month</strong></p>
            <p class="subtext">Model-estimated, not yet register-proven — the weekly tracker's
            predicted-vs-actual scorecard is what converts this into evidence.</p>

            <div class="flex justify-center gap-2 mt-4">
                <button class="btn-primary" onclick="downloadExportCsv()">Download CSV — recommended changes</button>
            </div>
        </div>
    </div>
    """

    # ── Assemble Full HTML ─────────────────────────────────
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Brand Team Pricing Dashboard</title>
    <style>
        body {{ font-family: 'Inter', -apple-system, sans-serif; background: #f8fafc; color: #0f172a; margin: 0; }}
        .nav {{ background: #1e293b; color: white; padding: 10px 20px; display: flex; gap: 20px; }}
        .nav a {{ color: #cbd5e1; text-decoration: none; cursor: pointer; padding: 8px 12px; border-radius: 4px; }}
        .nav a.active {{ background: #334155; color: white; font-weight: bold; }}
        .notifications {{ background: #fffbeb; border-bottom: 1px solid #fde68a; padding: 10px 20px; font-size: 0.9em; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
        .card {{ background: white; padding: 16px; border-radius: 8px; border: 1px solid #e2e8f0; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
        .label {{ font-size: 0.85em; color: #64748b; text-transform: uppercase; }}
        .value {{ font-size: 1.8em; font-weight: bold; margin: 4px 0; }}
        .subtext {{ font-size: 0.85em; }}
        .trend-good {{ color: #16a34a; }} .trend-bad {{ color: #dc2626; }}
        .tier-green {{ border-left: 4px solid #22c55e; }}
        .tier-amber {{ border-left: 4px solid #f59e0b; }}
        .tier-blue {{ border-left: 4px solid #3b82f6; }}
        .tier-gray {{ border-left: 4px solid #94a3b8; }}
        .tier-purple {{ border-left: 4px solid #8b5cf6; }}
        .tier-header {{ background: #f1f5f9; padding: 10px; margin-top: 20px; border-radius: 4px 4px 0 0; border: 1px solid #e2e8f0; border-bottom: none; }}
        .tier-strongcut .tier-header {{ background: #dcfce7; }}
        .tier-trade-off .tier-header {{ background: #fef3c7; }}
        .tier-increase .tier-header {{ background: #dbeafe; }}
        table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #e2e8f0; }}
        th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #e2e8f0; font-size: 0.9em; }}
        th {{ background: #f8fafc; font-weight: 600; color: #475569; }}
        tr:hover {{ background: #f1f5f9; cursor: pointer; }}
        .warning-row td {{ background: #fffbeb; color: #b45309; font-size: 0.85em; padding: 6px 10px; font-weight: 500; }}
        .flex {{ display: flex; }} .flex-1 {{ flex: 1; }} .justify-between {{ justify-content: space-between; }} .gap-2 {{ gap: 16px; }}
        .mt-2 {{ margin-top: 16px; }} .mt-4 {{ margin-top: 32px; }} .mb-2 {{ margin-bottom: 16px; }} .w-100 {{ width: 100%; }}
        button {{ padding: 8px 16px; border-radius: 4px; border: 1px solid #cbd5e1; background: white; cursor: pointer; }}
        .btn-primary {{ background: #2563eb; color: white; border: none; font-weight: bold; }}
    </style>
</head>
<body>
    {notifications_html}
    <div class="nav">
        <a id="tab1" class="active" onclick="switchView('view1')">Portfolio Summary</a>
        <a id="tab2" onclick="switchView('view2')">Action Queue</a>
        <a id="tab3" onclick="switchView('view3')">Cell Detail</a>
        <a id="tab4" onclick="switchView('view4')">Export</a>
    </div>
    
    <div class="container">
        {v1_html}
        {v2_html}
        {v3_html}
        {v4_html}
    </div>

    <script>
        const curveData = {curves_json_str};
        const exportData = {export_json_str};

        function downloadExportCsv() {{
            if (!exportData.length) {{ alert('No actionable recommendations in this run.'); return; }}
            const cols = Object.keys(exportData[0]);
            const esc = v => {{
                const s = (v === null || v === undefined) ? '' : String(v);
                return /[",\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
            }};
            const lines = [cols.join(',')].concat(
                exportData.map(r => cols.map(c => esc(r[c])).join(','))
            );
            const blob = new Blob([lines.join('\\n')], {{ type: 'text/csv' }});
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'recommended_changes.csv';
            a.click();
            URL.revokeObjectURL(a.href);
        }}

        function renderCurve(cellId, r) {{
            const el = document.getElementById('chart-container');
            const c = curveData[cellId];
            if (!c || !c.points || !c.points.length) {{
                el.innerHTML = '<div style="display:flex;height:100%;align-items:center;justify-content:center;color:#64748b;"><em>No curve available for this cell</em></div>';
                return;
            }}
            const pts = c.points.slice().sort((a, b) => a.discount_pct - b.discount_pct);
            const W = 600, H = 250, padL = 50, padR = 20, padT = 15, padB = 30;
            const xs = pts.map(p => p.discount_pct), ys = pts.map(p => p.predicted_units);
            const xMin = Math.min(...xs), xMax = Math.max(...xs);
            const yMax = Math.max(...ys) * 1.05 || 1;
            const X = v => padL + (v - xMin) / Math.max(xMax - xMin, 1e-9) * (W - padL - padR);
            const Y = v => H - padB - (v / yMax) * (H - padT - padB);
            const poly = pts.map(p => X(p.discount_pct).toFixed(1) + ',' + Y(p.predicted_units).toFixed(1)).join(' ');
            const marker = (disc, color, label) => {{
                if (disc === null || disc === undefined || disc < xMin || disc > xMax) return '';
                const x = X(disc).toFixed(1);
                return `<line x1="${{x}}" y1="${{padT}}" x2="${{x}}" y2="${{H - padB}}" stroke="${{color}}" stroke-dasharray="4 3"/>` +
                       `<text x="${{x}}" y="${{padT + 10}}" fill="${{color}}" font-size="10" text-anchor="middle">${{label}}</text>`;
            }};
            el.innerHTML =
                `<svg viewBox="0 0 ${{W}} ${{H}}" width="100%" height="100%" preserveAspectRatio="xMidYMid meet">` +
                `<line x1="${{padL}}" y1="${{H - padB}}" x2="${{W - padR}}" y2="${{H - padB}}" stroke="#cbd5e1"/>` +
                `<line x1="${{padL}}" y1="${{padT}}" x2="${{padL}}" y2="${{H - padB}}" stroke="#cbd5e1"/>` +
                `<text x="${{(W) / 2}}" y="${{H - 6}}" fill="#64748b" font-size="11" text-anchor="middle">Discount %</text>` +
                `<text x="12" y="${{H / 2}}" fill="#64748b" font-size="11" text-anchor="middle" transform="rotate(-90 12 ${{H / 2}})">Units/day</text>` +
                `<text x="${{padL - 6}}" y="${{Y(0) + 4}}" fill="#64748b" font-size="10" text-anchor="end">0</text>` +
                `<text x="${{padL - 6}}" y="${{Y(yMax / 1.05) + 4}}" fill="#64748b" font-size="10" text-anchor="end">${{(yMax / 1.05).toFixed(0)}}</text>` +
                `<text x="${{X(xMin)}}" y="${{H - padB + 14}}" fill="#64748b" font-size="10" text-anchor="middle">${{xMin.toFixed(0)}}%</text>` +
                `<text x="${{X(xMax)}}" y="${{H - padB + 14}}" fill="#64748b" font-size="10" text-anchor="middle">${{xMax.toFixed(0)}}%</text>` +
                `<polyline points="${{poly}}" fill="none" stroke="#2563eb" stroke-width="2"/>` +
                marker(c.current_disc, '#64748b', 'now') +
                marker(c.elbow_disc, '#16a34a', 'rec') +
                `</svg>`;
        }}

        function switchView(viewId) {{
            document.querySelectorAll('.view').forEach(e => e.style.display = 'none');
            document.querySelectorAll('.nav a').forEach(e => e.classList.remove('active'));
            document.getElementById(viewId).style.display = 'block';
            document.getElementById('tab' + viewId.replace('view', '')).classList.add('active');
        }}

        function openCellDetail(cellId, rowJsonStr) {{
            const r = JSON.parse(rowJsonStr.replace(/&quot;/g, '"'));
            
            document.getElementById('detail-title').textContent = r.title + " — " + r.city;
            document.getElementById('d-c-price').textContent = "₹" + r.current_price;
            document.getElementById('d-c-disc').textContent = r.current_discount_pct + "%";
            document.getElementById('d-c-units').textContent = r.current_units_day;
            document.getElementById('d-c-rev').textContent = "₹" + r.current_revenue_day;
            document.getElementById('d-c-margin').textContent = "₹" + r.current_margin_day;
            
            document.getElementById('d-r-price').textContent = "₹" + r.rec_price;
            document.getElementById('d-r-disc').textContent = r.rec_discount_pct + "%";
            document.getElementById('d-r-units').textContent = r.rec_units_day + " (" + r.rec_vol_change_pct + "%)";
            document.getElementById('d-r-rev').textContent = "₹" + r.rec_revenue_day + " (" + r.rec_rev_change_pct + "%)";
            document.getElementById('d-r-margin').textContent = "₹" + (r.current_margin_day + r.margin_change_monthly/30).toFixed(0);
            
            document.getElementById('w-c-disc').textContent = r.current_discount_pct;
            document.getElementById('w-r-disc').textContent = r.elbow_discount_pct;
            document.getElementById('w-r-roi').textContent = r.elbow_marginal_roi;
            document.getElementById('w-conf').textContent =
                r.confidence + (r.quality_note ? " — " + r.quality_note : "");
            renderCurve(cellId, r);
            
            document.getElementById('g-floor').textContent = r.guardrail_floor_ok ? "✓ Passed" : "✗ Hit floor limit";
            document.getElementById('g-change').textContent = r.guardrail_change_ok ? "✓ Passed" : "⚠ Hit max change rate";
            
            if (r.is_throttled) {{
                document.getElementById('g-throttle').textContent = "Recommendation throttled to respect max change rules.";
                document.getElementById('g-phase').textContent = "Suggested phasing: " + r.phasing_plan;
            }} else {{
                document.getElementById('g-throttle').textContent = "";
                document.getElementById('g-phase').textContent = "";
            }}
            
            switchView('view3');
        }}
    </script>
</body>
</html>
"""
    
    out_path = os.path.join(output_dir, "BRAND_DASHBOARD.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
        
    print(f"  [Dashboard] Saved: {out_path}")
    return out_path
