"""
pipeline.py — Master orchestrator for the 8-Stage Pricing Optimization System.

Usage:
    python pipeline.py                 # Full pipeline (all stages)
    python pipeline.py --stages 1 2 3  # Run specific stages only
"""
import os
import sys
import time
import json
import argparse
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import v4_config as cfg


def run_pipeline(stages=None):
    """Run the full 8-stage pipeline (or specific stages)."""
    all_stages = [1, 2, 3, 4, 5, 6, 7, 8]
    if stages is None:
        stages = all_stages

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(cfg.OUTPUT_DIR, timestamp)
    os.makedirs(run_dir, exist_ok=True)

    print("=" * 70)
    print(f"  PRICING OPTIMIZATION PIPELINE — {timestamp}")
    print(f"  Stages: {stages}")
    print(f"  Output: {run_dir}")
    print("=" * 70)

    context = {"run_dir": run_dir, "timestamp": timestamp}

    # ── STAGE 1: DATA INGESTION ─────────────────────────────────────
    if 1 in stages:
        print("\n" + "─" * 70)
        print("  STAGE 1 — DATA INGESTION")
        print("─" * 70)
        from stage1_ingestion.ingest import ingest_all_sales, load_event_calendar, load_master_costs
        raw_df = ingest_all_sales()
        calendar_df = load_event_calendar()
        master_costs = load_master_costs()
        context["raw_df"] = raw_df
        context["calendar_df"] = calendar_df
        context["master_costs"] = master_costs

    # ── STAGE 2: DATA PREPARATION ───────────────────────────────────
    if 2 in stages:
        print("\n" + "─" * 70)
        print("  STAGE 2 — DATA PREPARATION")
        print("─" * 70)
        from stage2_preparation.prepare import prepare_fact_table
        fact_df = prepare_fact_table(context["raw_df"], context["calendar_df"], run_dir=run_dir)
        fact_df.to_csv(os.path.join(run_dir, "fact_table.csv"), index=False)
        context["fact_df"] = fact_df

    # ── STAGE 3: FEATURE ENGINEERING ────────────────────────────────
    if 3 in stages:
        print("\n" + "─" * 70)
        print("  STAGE 3 — FEATURE ENGINEERING")
        print("─" * 70)
        from stage3_features.features import engineer_features
        feat_df = engineer_features(context["fact_df"])
        feat_df.to_csv(os.path.join(run_dir, "features.csv"), index=False)
        context["feat_df"] = feat_df

    # ── STAGE 4: HIERARCHICAL ELASTICITY MODEL ──────────────────────
    if 4 in stages:
        print("\n" + "─" * 70)
        print("  STAGE 4 — HIERARCHICAL ELASTICITY MODEL")
        print("─" * 70)
        from stage4_model.elasticity import train_hierarchical_model
        model_output = train_hierarchical_model(context["feat_df"])
        model_output["elasticities"].to_csv(
            os.path.join(run_dir, "elasticity_estimates.csv"), index=False
        )
        context["model_output"] = model_output

    # ── STAGE 5: SATURATION CURVES ──────────────────────────────────
    if 5 in stages:
        print("\n" + "─" * 70)
        print("  STAGE 5 — SATURATION CURVES")
        print("─" * 70)
        from stage5_curves.curves import generate_saturation_curves
        curves_df = generate_saturation_curves(
            context["model_output"]["elasticities"],
            context["model_output"]["model"],
            context["feat_df"],
        )
        context["curves_df"] = curves_df

    # ── STAGE 6: ECONOMICS + ELBOW DETECTION ────────────────────────
    if 6 in stages:
        print("\n" + "─" * 70)
        print("  STAGE 6 — ECONOMICS + ELBOW DETECTION")
        print("─" * 70)
        from stage6_economics.economics import compute_economics
        economics_df = compute_economics(
            context["curves_df"],
            context.get("master_costs"),
        )
        context["economics_df"] = economics_df

    # ── STAGE 7: GUARDRAILS + TIERING ───────────────────────────────
    if 7 in stages:
        print("\n" + "─" * 70)
        print("  STAGE 7 — GUARDRAILS + TIERING")
        print("─" * 70)
        from stage7_guardrails.guardrails import apply_guardrails_and_tier
        recommendations_df = apply_guardrails_and_tier(context["economics_df"])

        # Save recommendations (exclude nested columns for CSV).
        # Lead with price-first columns — that's what the brand team thinks in.
        nested = {"ladder", "curve_points", "curve_params"}
        price_first = [
            "product_id", "city", "category", "title", "mrp", "cell_id",
            "tier", "confidence", "quality_note",
            "current_price", "rec_price", "price_change_inr", "price_change_pct",
            "current_discount_pct", "rec_discount_pct",
            "current_units_day", "rec_units_day", "rec_vol_change_pct",
            "current_revenue_day", "rec_revenue_day", "rec_rev_change_pct",
            "rec_monthly_savings",
            "elasticity", "badge_sensitivity",
        ]
        ordered = [c for c in price_first if c in recommendations_df.columns]
        rest    = [c for c in recommendations_df.columns
                   if c not in ordered and c not in nested]
        save_cols = ordered + rest
        recommendations_df[save_cols].to_csv(
            os.path.join(run_dir, "recommendations.csv"), index=False
        )
        context["recommendations_df"] = recommendations_df

        # ── GENERATE DASHBOARD ──────────────────────────────────────
        print("\n" + "─" * 70)
        print("  DASHBOARD GENERATION")
        print("─" * 70)
        from dashboard.dashboard_generator import generate_dashboard
        dashboard_path = generate_dashboard(recommendations_df, run_dir, context)
        context["dashboard_path"] = dashboard_path

    # ── STAGE 8: WASTE & REINVESTMENT REPORT ─────────────────────────
    if 8 in stages:
        print("\n" + "─" * 70)
        print("  STAGE 8 — WASTE & REINVESTMENT REPORT")
        print("─" * 70)
        from stage8_output.waste_reinvest import generate_waste_reinvest_report
        report_paths = generate_waste_reinvest_report(
            context["recommendations_df"],
            context["feat_df"],
            context.get("model_output"),
            run_dir,
        )
        context["report_paths"] = report_paths

    # ── FINAL SUMMARY ───────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  PIPELINE COMPLETE")
    print("=" * 70)
    print(f"  Output directory: {run_dir}")
    if "dashboard_path" in context:
        print(f"  Dashboard: {context['dashboard_path']}")
    if "report_paths" in context:
        print(f"  W&R Report (MD):    {context['report_paths']['markdown']}")
        if context['report_paths'].get('excel'):
            print(f"  W&R Report (Excel): {context['report_paths']['excel']}")
    print("=" * 70)

    return context


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="8-Stage Pricing Pipeline")
    parser.add_argument("--stages", nargs="+", type=int, default=None,
                        help="Specific stages to run (e.g., --stages 1 2 3)")
    args = parser.parse_args()
    run_pipeline(stages=args.stages)
