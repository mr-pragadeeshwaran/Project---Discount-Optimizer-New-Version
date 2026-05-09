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

def generate_dashboard(rec_df: pd.DataFrame, output_dir: str, context: dict) -> str:
    """Generates the 4-view HTML dashboard."""
    print("  [Dashboard] Generating 4-view HTML dashboard...")
    
    # ── Prep data ──────────────────────────────────────────
    tier_counts = rec_df["tier"].value_counts()
    tier_savings = rec_df.groupby("tier")["rec_monthly_savings"].sum()
    
    # Current vs Target (mocking target for now)
    current_disc_avg = rec_df["current_discount_pct"].mean()
    
    # Last week mock performance 
    last_week = {
        "acted": 38,
        "pred_vol": -3.1, "act_vol": -3.4,
        "pred_rev": 0.8, "act_rev": 0.6,
        "drift": 2
    }

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
                <div class="subtext trend-good">↓ from last quarter</div>
            </div>
            <div class="card">
                <div class="label">Target Discount %</div>
                <div class="value">{cfg.TARGET_DISCOUNT_PCT:.1f}%</div>
                <div class="subtext">by {cfg.TARGET_QUARTER}</div>
            </div>
            <div class="card">
                <div class="label">Glide-path Status</div>
                <div class="value trend-good">On track ✓</div>
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
        <div class="card">
            <p>Cells acted on: <strong>{last_week['acted']}</strong></p>
            <p>Predicted vs actual volume: <strong>{last_week['pred_vol']}% vs {last_week['act_vol']}%</strong> <span class="trend-good">(within tolerance ✓)</span></p>
            <p>Predicted vs actual revenue: <strong>+{last_week['pred_rev']}% vs +{last_week['act_rev']}%</strong> <span class="trend-good">(within tolerance ✓)</span></p>
            <p>Drift alerts: <strong class="trend-bad">{last_week['drift']} cells</strong> <a href="#">(click to review)</a></p>
        </div>
    </div>
    """

    # ── View 2: Action Queue HTML ──────────────────────────
    v2_html = """<div id="view2" class="view" style="display:none;"><h2>Action Queue</h2>"""
    
    for tier in ["Strong Cut", "Trade-off", "Increase"]:
        tier_df = rec_df[rec_df["tier"] == tier]
        if tier_df.empty: continue
        
        header_actions = "[Approve All] [Review Each]" if tier != "Trade-off" else "[Review Each]"
        v2_html += f"""
        <div class="tier-section tier-{tier.lower().replace(' ', '')}">
            <div class="tier-header flex justify-between">
                <h3>TIER — {tier.upper()} ({len(tier_df)} cells)</h3>
                <span>{header_actions}</span>
            </div>
            <table class="w-100">
                <thead>
                    <tr><th><input type="checkbox"></th><th>SKU</th><th>City</th><th>Now</th><th>→</th><th>Rec</th><th>Vol Δ</th><th>Rev Δ</th><th>Save/mo</th><th>Conf</th></tr>
                </thead>
                <tbody>
        """
        for _, r in tier_df.iterrows():
            row_json = json.dumps(r.drop(labels=["ladder", "curve_points", "curve_params"]).to_dict()).replace('"', '&quot;')
            v2_html += f"""
            <tr onclick="openCellDetail('{r['cell_id']}', '{row_json}')">
                <td><input type="checkbox" onclick="event.stopPropagation()"></td>
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
                v2_html += f"""<tr class="warning-row"><td colspan="10">⚠ Volume drop above 5% — needs your call</td></tr>"""
        v2_html += "</tbody></table></div>"
    
    v2_html += "</div>"

    # ── View 3: Cell Detail HTML (Template) ────────────────
    v3_html = """
    <div id="view3" class="view" style="display:none;">
        <div class="flex justify-between mt-2 mb-2">
            <h2 id="detail-title">SKU — City</h2>
            <div><button onclick="switchView('view2')">Back</button> <button class="btn-primary">Approve</button></div>
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
            <div id="chart-container" style="height: 250px; background: #f8fafc; border: 1px solid #e2e8f0; display:flex; align-items:center; justify-content:center;">
                [Chart renders here]
            </div>
        </div>
        
        <div class="card mt-2">
            <h3>WHY THIS RECOMMENDATION</h3>
            <p>At <span id="w-c-disc"></span>% discount, marginal ROI is low.</p>
            <p>At <span id="w-r-disc"></span>% discount, marginal ROI = <strong id="w-r-roi"></strong> (just above threshold 1.0)</p>
            <p>Cells with similar profile: 6 — average actual outcome matched prediction within 4%</p>
        </div>
        
        <div class="flex gap-2 mt-2">
            <div class="card flex-1">
                <h3>GUARDRAILS APPLIED</h3>
                <p>Floor price check: <span id="g-floor"></span></p>
                <p>Max change rate: <span id="g-change"></span></p>
                <p id="g-throttle" class="trend-bad font-bold"></p>
                <p id="g-phase"></p>
            </div>
            <div class="card flex-1">
                <h3>OVERRIDE OPTIONS</h3>
                <div><input type="radio" name="override" checked> Accept recommendation</div>
                <div><input type="radio" name="override"> Modify discount: <input type="number" style="width:60px">%</div>
                <div><input type="radio" name="override"> Defer to next cycle</div>
                <div><input type="checkbox"> Mark "do not auto-act" (strategic)</div>
                <div class="mt-2"><input type="text" placeholder="Notes..." class="w-100"></div>
            </div>
        </div>
    </div>
    """

    # ── View 4: Export HTML ────────────────────────────────
    v4_html = """
    <div id="view4" class="view" style="display:none;">
        <h2>Export to Blinkit</h2>
        <div class="card mt-4 text-center">
            <h3>EXPORT APPROVED CHANGES</h3>
            <p class="mt-2">Cells approved: <strong>58</strong></p>
            <p>Total impact/mo: <strong>+₹24.8L margin, -₹19.6L discount spend</strong></p>
            <p class="mt-2">Effective date: <input type="date"></p>
            
            <div class="flex justify-center gap-2 mt-4">
                <button class="btn-primary">Download CSV — Blinkit Format</button>
                <button>Download CSV — Internal Review</button>
                <button>Generate Audit Log</button>
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
    <div class="notifications">
        <strong>🔔 Notifications:</strong> 
        <span class="mr-4">Competitor X cut price on Jaggery in Bangalore.</span>
        <span><a href="#">Only Organic Day</a> on Friday — 23 cells need event pricing.</span>
    </div>
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
