"""Evaluate the latest WASTE_REINVEST_REPORT.xlsx formulas to confirm correctness."""
import formulas, glob

p = sorted(glob.glob('v4_outputs/*/WASTE_REINVEST_REPORT.xlsx'))[-1]
print(f"Evaluating: {p}")
xl = formulas.ExcelModel().loads(p).finish()
sol = xl.calculate()

def fetch(sheet, coord):
    key = f"'[WASTE_REINVEST_REPORT.xlsx]{sheet.upper()}'!{coord}"
    if key not in sol:
        return "(no formula)"
    v = sol[key].value
    try:
        v = v[0][0] if hasattr(v, '__len__') and len(v) and hasattr(v[0], '__len__') else v
    except Exception:
        pass
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)

print("\n=== Summary sheet (formulas evaluated) ===")
for coord, label in [
    ('B8',  'Today gross sales'),
    ('C8',  'After-cuts gross sales'),
    ('D8',  'After-both gross sales'),
    ('B9',  'Today discount spend'),
    ('C9',  'After-cuts discount spend'),
    ('D9',  'After-both discount spend'),
    ('B10', 'Today net revenue'),
    ('B11', 'Today units / mo'),
    ('C11', 'After-cuts units / mo'),
    ('D11', 'After-both units / mo'),
    ('B12', 'Today weighted discount %'),
    ('C12', 'After-cuts weighted discount %'),
    ('D12', 'After-both weighted discount %'),
    ('B15', 'Gap today (ppt)'),
    ('C22', 'Cut: spend delta'),
    ('D22', 'Cut: units delta'),
    ('C23', 'Reinvest: spend delta'),
    ('D23', 'Reinvest: units delta'),
    ('B34', 'OVERALL ACCURACY TIER'),
]:
    print(f"  Summary!{coord:5s} {label:40s} = {fetch('Summary', coord)}")

# By Product sheet was redesigned as a city × week glide matrix —
# values are pre-computed in Python, not formulas. Skip the formula check.
