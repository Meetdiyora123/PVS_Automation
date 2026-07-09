# PVS Automation

Daily script that queries MySQL for packs scheduled for vision system analysis, retrieves pack details via the getpackdetails API, builds quadrant-grouped payloads per drop, and POSTs each to the PVS analysis API.

## Workflow

```
config.json
     │
     ▼
 ┌──────────────┐    ┌──────────────────┐
 │  Auth Login   │───▶│  token_cache.json│
 │  (API POST)   │    │  (reused on next │
 └──────────────┘    │       run)        │
                     └──────────────────┘
     │
     ▼
 ┌──────────────┐
 │  MySQL Query  │───▶  Rows grouped by pack_id
 │  (connector)  │
 └──────────────┘
     │
     ▼
 ┌──────────────────┐
 │  getpackdetails   │───▶  Response with SlotList + drop_data
 │  API (GET)        │
 └──────────────────┘
     │
     ▼
 ┌──────────────────────┐
 │  Build payloads       │───▶  One payload per drop_number
 │  (merge slots by drop │      Quadrant-grouped drug data
 │   into 4 quadrants)   │
 └──────────────────────┘
     │
     ▼
 ┌──────────────────────────┐
 │  POST to PVS Analysis API │───▶  x-www-form-urlencoded
 │  (one call per drop)      │     args=<JSON payload>
 └──────────────────────────┘
     │
     ▼
  Log results to console + file
```

## Requirements

- Python 3.8+
- `requests`
- `mysql-connector-python`

Install:

```
pip install -r requirements.txt
```

## Configuration

All settings are in `config.json`:

### `auth`

Credentials for the login API. The script POSTs `application/x-www-form-urlencoded` data to obtain a bearer token.

```json
{
  "auth": {
    "url": "https://example.com/api/login",
    "data": {
      "username": "...",
      "password": "...",
      "client_id": "dp-web-app",
      "scope": "dp-full-scope"
    }
  }
}
```

Login response must contain `data.access_token` (or `access_token` at the top level). The token is cached to `token_cache.json` and reused on subsequent runs until it expires.

### `database`

MySQL connection details and the SQL query to fetch packs.

| Field     | Description                                 |
|-----------|---------------------------------------------|
| `host`    | MySQL host                                  |
| `port`    | MySQL port (default 3306)                   |
| `database`| Database name                               |
| `user`    | Database user                               |
| `password`| Database password                           |
| `date`    | Date override (see Date Configuration below)|
| `query`   | SQL query with `{date}` / `{date_start}` / `{date_end}` placeholders |

The query must alias the following columns: `pack_id`, `company_id`, `system_id`, `device_id`.

### `api`

| Field                               | Description                          |
|-------------------------------------|--------------------------------------|
| `getpackdetails_base_url`           | Base URL for getpackdetails API      |
| `perform_vision_system_analysis_url`| URL for PVS analysis POST endpoint   |

### `request_id` / `msg_id`

Defaults (`54` / `124`) are included in each payload. Override in config if needed.

### `test_pack`

Device/system/company IDs used when running with `--test-pack`.

## Date Configuration

The SQL query supports three placeholders:

| Placeholder    | Single Date Mode     | Range Mode (`last_N_days`) |
|----------------|----------------------|----------------------------|
| `{date}`       | `2026-05-20`         | `2026-05-20` (end date)    |
| `{date_start}` | `2026-05-20`         | `2026-05-18` (N days ago)  |
| `{date_end}`   | `2026-05-20`         | `2026-05-20` (yesterday)   |

Resolution priority (lowest to highest):

1. **Default** → yesterday (`today - 1`)
2. **Config `database.date`** in `config.json`
3. **`--date` CLI flag**

### Examples

| Config / CLI value    | Result                          |
|-----------------------|---------------------------------|
| *(empty / omitted)*   | Yesterday                       |
| `"2026-05-15"`        | Single date: May 15             |
| `"last_2_days"`       | Range: today-2 to yesterday     |
| `"last_4_days"`       | Range: today-4 to yesterday     |
| `--date 2026-05-15`   | Single date (overrides config)  |
| `--date last_2_days`  | Range (overrides config)        |

### SQL examples

For a single date:

```sql
WHERE DATE(created_date) = '{date}'
```

For a date range:da

```sql
WHERE DATE(created_date) BETWEEN '{date_start}' AND '{date_end}'
```

## Usage

```
python pvs_automation.py [options]
```

| Argument                      | Description                                        |
|-------------------------------|----------------------------------------------------|
| `--test-pack <id>`            | Process a single pack (uses `test_pack` in config) |
| `--no-post`                   | Print generated payloads to stdout, skip POST      |
| `-s, --save-payloads <dir>`   | Save payload JSON files to the given directory     |
| `--date <value>`              | Override query date (see Date Configuration above) |
| `--slot-number <slot_number>  | Process a single slot                              |

### Examples

```powershell
# Full daily run (default: yesterday's packs)
python pvs_automation.py

# Test a specific pack
python pvs_automation.py --test-pack 153084

# Inspect payloads without posting
python pvs_automation.py --test-pack 153084 --no-post

# Save payloads for inspection
python pvs_automation.py --test-pack 153084 --no-post -s payloads_output

# Run for a date range
python pvs_automation.py --date last_3_days

# Run for a specific date
python pvs_automation.py --date 2026-05-20
```

## Payload Structure

One POST per `drop_number`. Each payload contains 4 quadrant entries (`"1"` through `"4"`) with drug data grouped by their `quadrant` field (not `current_quadrant`).
    
```json
{
  "data": {
    "1": {
      "pack_id": 153084,
      "slot_number": "22",
      "slot_id": 3392867,
      "drug_data": {
        "009047280_3009": {
          "ndc": "00904728080",
          "drug_name": "DOCUSATE SODIUM 100 MG",
          "quantity": 1,
          "quadrant": 1,
          "total_quantity": 1,
          "dropped_quantity": 1,
          "pill_for_slot_transaction": 1,
          "skipped": false,
          "timeout_occurred": false,
          "motor_or_canister_jammed": false
        }
      },
      "drug_volume": 423.18,
      "dummy_pack": false,
      "redrop": false,
      "request_id": 54
    },
    "2": { "slot_number": "13", "drug_data": { ... } },
    "3": { "slot_number": null, "drug_data": {} },
    "4": { "slot_number": null, "drug_data": {} }
  },
  "drop_number": "1",
  "type": "pack",
  "use_start_stop_flow": 1,
  "callback_type": 1,
  "msg_id": 124,
  "station_type": 21,
  "station_id": 21000
}
```

## Token Caching

- On first run, the script authenticates and saves the token + expiry to `token_cache.json`.
- Subsequent runs reuse the cached token until it expires.
- If the PVS API returns `401`, the token is refreshed automatically and the request is retried once.

## Logging

Logs are written to both `stdout` and a dated file (`pvs_automation_YYYY-MM-DD.log`).

## Troubleshooting

| Symptom                    | Likely Cause                                   |
|----------------------------|-------------------------------------------------|
| Script hangs on MySQL      | `use_pure=True` missing; mysql-connector C extension hangs on handshake with some MySQL versions |
| "Found 0 pack(s)"          | No packs found for the configured date range; check `database.date` or `--date` |
| `401` on API calls         | Token expired; script refreshes automatically   |
| "slot None" in old logs    | Fixed — log now shows actual slot numbers       |

## Files

| File                  | Purpose                                   |
|-----------------------|-------------------------------------------|
| `pvs_automation.py`   | Main script                               |
| `config.json`         | Configuration (auth, DB, API, test_pack)  |
| `requirements.txt`    | Python package dependencies               |
| `token_cache.json`    | Cached auth token (auto-created)          |
| `pvs_automation_*.log`| Run logs (auto-created)                   |
