"""
make_settings_template.py — write the brand settings template into config/.

    python -X utf8 scripts/make_settings_template.py            # both formats
    python -X utf8 scripts/make_settings_template.py --live     # + make it live

Templates are generated from the registry in settings_loader.py and pre-filled
with the values currently in force, so a template can never list a knob the
code doesn't support (or miss one it does).

Without --live this only writes *_TEMPLATE files; your live config/settings.*
is never touched. With --live it also installs config/settings.csv (creating it
only if absent — an existing live file is left alone).
"""
import os
import sys
import argparse

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

import settings_loader as sl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="also create config/settings.csv if it does not exist yet")
    a = ap.parse_args()

    os.makedirs(sl.CONFIG_DIR, exist_ok=True)
    written = []

    def _write_text(name, text):
        p = os.path.join(sl.CONFIG_DIR, name)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        written.append(p)

    _write_text("SETTINGS_TEMPLATE.csv", sl.template_csv())
    _write_text("FESTIVALS_TEMPLATE.csv", sl.template_festivals_csv())
    _write_text("PLATFORM_EVENTS_TEMPLATE.csv", sl.template_events_csv())

    try:
        p = os.path.join(sl.CONFIG_DIR, "SETTINGS_TEMPLATE.xlsx")
        with open(p, "wb") as fh:
            fh.write(sl.template_xlsx_bytes())
        written.append(p)
    except ImportError:
        print("[template] openpyxl missing — wrote the CSV templates only.")

    if a.live:
        live = sl.CSV_PATH
        if os.path.exists(live) or os.path.exists(sl.XLSX_PATH):
            print(f"[template] live settings already exist "
                  f"({os.path.basename(live if os.path.exists(live) else sl.XLSX_PATH)}) "
                  f"— left untouched.")
        else:
            _write_text("settings.csv", sl.template_csv())
            print("[template] created config/settings.csv (edit the value column).")

    for p in written:
        print(f"[template] wrote {os.path.relpath(p, ROOT)}")
    print(f"[template] {len(sl.REGISTRY)} settings across {len(sl.SECTIONS)} sections: "
          f"{', '.join(sl.SECTIONS)}")


if __name__ == "__main__":
    main()
