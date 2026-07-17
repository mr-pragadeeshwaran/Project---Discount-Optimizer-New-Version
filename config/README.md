# config/ — every knob, in a spreadsheet

The system's settings live **here**, not in code. Edit a file, run the system;
nothing else to touch.

## The short version

1. Open the dashboard (`launch_ui.bat`) → **Inputs & Settings**
2. **Download template** (Excel or CSV)
3. Edit the **value** column
4. **Upload filled file** — it is checked before it is saved
5. Run as usual; the new values are in force

Prefer files to buttons? Edit `config/settings.csv` directly — same thing.

## What's in here

| File | What it is |
|---|---|
| `settings.csv` | **The live settings.** Edit the `value` column. |
| `settings.xlsx` | Live settings, Excel form. One of the two — if both exist, the `.xlsx` wins and says so. |
| `SETTINGS_TEMPLATE.xlsx` | A fresh template: **Settings**, **Festivals** and **Platform Events** sheets, pre-filled with the values currently in force. |
| `SETTINGS_TEMPLATE.csv` | Same, text form. |
| `FESTIVALS_TEMPLATE.csv` | Festival calendar (`date,event`) → copy to `festivals.csv` to use. |
| `PLATFORM_EVENTS_TEMPLATE.csv` | Sale windows (`start,end,event`) → copy to `platform_events.csv` to use. |

Regenerate the templates any time (they are built from the code, so they can
never list a knob that doesn't exist):

```
python -X utf8 scripts/make_settings_template.py
```

## The rules

- **Blank value = keep the built-in default.** It is not an empty string.
- **`none` empties a list** (e.g. no hero SKUs).
- **Lists separate with `|`** — `496799 | 521140`.
- **Unknown keys are rejected**, with a suggestion. A typo can't silently do nothing.
- **Fractions vs percents are checked.** `DEFAULT_BUDGET_PCT_CAP` is a fraction:
  `0.12` = 12%. Type `12` and it's rejected with "did you mean 0.12?" — that
  mistake would otherwise mean a 1200% cap and no cuts, ever.
- **A festivals/events sheet with rows REPLACES the calendar** (the template
  ships pre-filled, so what you see is what runs).
- **A bad file stops the run**, loudly, naming the cell. That is on purpose: a
  wrong number that runs quietly is far more expensive than a run that stops.

## Defaults still live in code

`v4_config.py` holds every default and stays the reference for what a knob
means. This folder only *overrides* it. Delete `settings.csv` and you're back
to the code defaults — nothing else changes.

Which knobs are overridable, their types and their validation rules are
declared in one place: the `REGISTRY` in `settings_loader.py`.
