"""
PVS Automation - Daily script to process packs per drop and perform vision system analysis.

Workflow:
  1) Read config.json
  2) Load cached token or login to auth API
  3) Connect to MySQL, fetch today's pack rows (SQL query from config,
     {date} placeholder replaced automatically)
  4) Group rows by pack_id. For each pack: call getpackdetails API once,
     then build one quadrant-grouped PVS payload per drop.
     Init vision system, then POST each payload to PVS analysis API.
  5) If 401 received, refresh token and retry once.
  6) Log results
"""

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
import requests
import mysql.connector

TOKEN_CACHE_FILE = "token_cache.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"pvs_automation_{date.today().isoformat()}.log"),
    ],
)
logger = logging.getLogger(__name__)


def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)


# ── Token caching ──────────────────────────────────────────────

def load_cached_token():
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None
    try:
        with open(TOKEN_CACHE_FILE) as f:
            cached = json.load(f)
        expires_at = cached.get("expires_at")
        if expires_at:
            exp_dt = datetime.fromisoformat(expires_at)
            if datetime.now() < exp_dt:
                logger.info("Using cached token")
                return cached["access_token"]
        logger.info("Cached token expired")
    except Exception:
        pass
    return None


def save_token_cache(token, expires_in, expires_at_str):
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump({
            "access_token": token,
            "expires_in": expires_in,
            "expires_at": expires_at_str,
            "cached_at": datetime.now().isoformat(),
        }, f)
    logger.info("Token cached")


def get_auth_token(config):
    cached = load_cached_token()
    if cached:
        return cached

    auth_cfg = config["auth"]
    logger.info(f"Logging in to {auth_cfg['url']}")
    resp = requests.post(
        auth_cfg["url"],
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=auth_cfg["data"],
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data", {})
    token = data.get("access_token") or body.get("access_token") or body.get("token") or body.get("id_token")
    if not token:
        logger.error(f"No token found in auth response: {body}")
        raise ValueError("Could not extract token from auth response")
    logger.info("Auth token obtained")

    expires_in = data.get("expires_in") or body.get("expires_in", 84600)
    expires_at = data.get("expires_at") or body.get("expires_at", "")
    save_token_cache(token, expires_in, expires_at)
    return token


def refresh_token(config):
    logger.info("Refreshing token...")
    if os.path.exists(TOKEN_CACHE_FILE):
        os.remove(TOKEN_CACHE_FILE)
    return get_auth_token(config)


# ── HTTP helpers with 401 auto-retry ───────────────────────────

class TokenRefresher:
    def __init__(self, config):
        self.config = config
        self.token = None

    def ensure_token(self):
        if not self.token:
            self.token = get_auth_token(self.config)
        return self.token

    def request_with_retry(self, method, url, **kwargs):
        token = self.ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        kwargs["headers"] = headers

        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 401:
            logger.warning("Got 401, refreshing token and retrying...")
            self.token = refresh_token(self.config)
            headers["Authorization"] = f"Bearer {self.token}"
            resp = requests.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

    def get(self, url, **kwargs):
        return self.request_with_retry("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request_with_retry("POST", url, **kwargs)


# ── Database ───────────────────────────────────────────────────

def get_db_connection(config):
    db = config["database"]
    logger.info(f"Connecting to MySQL {db['host']}:{db.get('port', 3306)}/{db['database']}")
    conn = mysql.connector.connect(
        host=db["host"],
        port=db.get("port", 3306),
        database=db["database"],
        user=db["user"],
        password=db["password"],
        connection_timeout=25,
        use_pure=True,
    )
    return conn


def resolve_query(config):
    """Replace {date}, {date_start}, {date_end} placeholders in SQL.

    Date resolution (lowest to highest priority):
      1) Default: yesterday (today - 1)
      2) Config database.date field
      3) --date CLI override (already applied to config)

    Config date values:
      - Omitted / empty string  → yesterday
      - 'YYYY-MM-DD'            → that single date
      - 'last_2_days'           → range from today-2 to yesterday
      - 'last_4_days'           → range from today-4 to yesterday
    """
    db_cfg = config["database"]
    query = db_cfg["query"]
    raw = db_cfg.get("date", "").strip()

    today = date.today()
    yesterday = today - timedelta(days=1)

    if not raw:
        date_start = date_end = yesterday
    elif raw.startswith("last_") and raw.endswith("_days"):
        try:
            n = int(raw.split("_")[1])
            date_start = today - timedelta(days=n)
            date_end = yesterday
        except (IndexError, ValueError):
            logger.warning(f"Invalid date range '{raw}', falling back to yesterday")
            date_start = date_end = yesterday
    else:
        try:
            parsed = date.fromisoformat(raw)
            date_start = date_end = parsed
        except ValueError:
            logger.warning(f"Invalid date '{raw}', falling back to yesterday")
            date_start = date_end = yesterday

    query = query.replace("{date}", date_end.isoformat())
    query = query.replace("{date_start}", date_start.isoformat())
    query = query.replace("{date_end}", date_end.isoformat())
    return query


def fetch_packs(conn, query):
    cursor = conn.cursor(dictionary=True)
    logger.info("Fetching packs...")
    cursor.execute(query)
    rows = cursor.fetchall()
    cursor.close()
    logger.info(f"Found {len(rows)} pack(s)")
    return rows


# ── getpackdetails API ─────────────────────────────────────────

def get_pack_details(http, pack_id, device_id, system_id, company_id, base_url):
    url = f"{base_url}/v2/api/getpackdetails"
    params = {
        "device_id": device_id,
        "non_fractional": 1,
        "exclude_pack_ids": "[]",
        "system_id": system_id,
        "mft_slots": 1,
        "company_id": company_id,
        "pack_id": pack_id,
    }
    logger.info(f"  Fetching details for pack {pack_id}")
    resp = http.get(url, params=params, timeout=30)
    return resp.json()


# ── Payload building ───────────────────────────────────────────

def build_all_drop_payloads(getpack_data, system_id, request_id=54, msg_id=124):
    """Build one payload per drop, merging all slots into the 4 quadrant entries."""
    data = getpack_data.get("data", {})
    drop_data = data.get("drop_data", {})
    slot_list = data.get("SlotList", {})
    pack_id = data.get("id")
    pack_display_id = data.get("pack_display_id")
    dummy_pack = data.get("dummy_pack", False)

    payloads = []
    for drop_number in sorted(drop_data.keys(), key=int):
        dn = str(drop_number)
        runs = drop_data[dn]

        # Collect all distinct slot keys for this drop
        slot_keys = set()
        for run_key in sorted(runs.keys(), key=int):
            slots = runs[str(run_key)]
            for slot_key in sorted(slots.keys(), key=int):
                sk = str(slot_key)
                if sk in slot_list:
                    slot_keys.add(sk)

        if not slot_keys:
            continue

        # Collect drugs from all slots, grouped by quadrant
        quadrants = {"1": {}, "2": {}, "3": {}, "4": {}}
        quadrant_first_drug = {}
        quadrant_slot_key = {}

        for sk in sorted(slot_keys, key=int):
            slot_drugs = slot_list.get(sk, {})
            for fndc_txr, drug in slot_drugs.items():
                if "total_quantity" not in drug:
                    filled_qty = drug.get("filled_quantity") or drug.get("quantity") or 0
                    drug["total_quantity"] = filled_qty
                    drug["dropped_quantity"] = filled_qty
                    drug["pill_for_slot_transaction"] = filled_qty
                    drug["skipped"] = bool(drug.get("skip_status", False))
                    drug["timeout_occurred"] = False
                    drug["motor_or_canister_jammed"] = False

                quad = drug.get("quadrant")
                if quad is not None:
                    q_key = str(int(quad))
                    if q_key in quadrants:
                        if not quadrants[q_key]:
                            quadrant_first_drug[q_key] = drug
                            quadrant_slot_key[q_key] = sk
                        quadrants[q_key][fndc_txr] = drug

        # Determine drop_number from first drug
        first_drug = None
        for qk in ("1", "2", "3", "4"):
            if quadrants[qk]:
                first_drug = next(iter(quadrants[qk].values()))
                break
        if first_drug is None:
            continue
        drop_number_val = str(first_drug.get("drop_number", dn))

        # Build 4 quadrant entries
        data_entries = {}
        for q_key in ("1", "2", "3", "4"):
            q_drugs = quadrants[q_key]
            if q_drugs:
                fd = quadrant_first_drug[q_key]
                entry = {
                    "pack_id": pack_id,
                    "pack_display_id": pack_display_id,
                    "system_id": system_id,
                    "device_id": fd.get("current_device_id") or fd.get("device_id"),
                    "slot_id": fd.get("slot_id"),
                    "slot_number": quadrant_slot_key[q_key],
                    "slot_row": fd.get("slot_row"),
                    "slot_column": fd.get("slot_column"),
                    "slot_header_id": fd.get("slot_header_id"),
                    "config_id": fd.get("config_id"),
                    "drug_data": q_drugs,
                    "mfd_data": {},
                    "pvs_response": 0,
                    "pack_mft_status": False,
                    "slot_mft_status": False,
                    "drug_volume": fd.get("drug_volume", 0) * (fd.get("quantity") or 1),
                    "dummy_pack": dummy_pack,
                    "redrop": False,
                    "request_id": request_id,
                }
            else:
                entry = {
                    "pack_id": pack_id,
                    "pack_display_id": pack_display_id,
                    "system_id": system_id,
                    "device_id": None,
                    "slot_id": None,
                    "slot_number": None,
                    "slot_row": None,
                    "slot_column": None,
                    "slot_header_id": None,
                    "config_id": None,
                    "drug_data": {},
                    "mfd_data": {},
                    "pvs_response": 0,
                    "pack_mft_status": False,
                    "slot_mft_status": False,
                    "drug_volume": 0,
                    "dummy_pack": dummy_pack,
                    "redrop": False,
                }
            data_entries[q_key] = entry

        payload = {
            "data": data_entries,
            "drop_number": drop_number_val,
            "type": "pack",
            "use_start_stop_flow": 1,
            "callback_type": 1,
            "msg_id": msg_id,
            "station_type": 21,
            "station_id": 21000,
        }
        payloads.append((dn, payload))

    return payloads


def save_payloads(payloads, output_dir, pack_id):
    os.makedirs(output_dir, exist_ok=True)
    for drop_number, payload in payloads:
        filename = f"pack{pack_id}_drop{drop_number}.txt"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w") as f:
            f.write(json.dumps(payload, indent=2))
        logger.info(f"  Saved: {filepath}")


# ── Vision system init ─────────────────────────────────────────

def init_vision_system(http, pvs_base_url, station_type, station_id, msg_id=2):
    url = f"{pvs_base_url}/api/initiate_vision_system"

    params = {
        "station_type": station_type,
        "station_id": station_id,
        "msg_id": msg_id,
        "cmd": "init_camera",
    }

    logger.info(
        f"  Initialising vision system "
        f"(station_type={station_type}, station_id={station_id})..."
    )

    resp = http.get(url, params=params, timeout=30)

    logger.info(f"  Init HTTP status: {resp.status_code}")

    body = resp.json()

    logger.info(f"  Init response: {json.dumps(body, indent=2)}")

    logger.info("  Waiting 5 seconds for vision system to become ready...")
    time.sleep(2)

    return body


def stop_vision_system(http, pvs_base_url, station_type, station_id, msg_id=2):
    """Stop the PVS camera after analysis is complete."""
    url = f"{pvs_base_url}/api/stop_vision_system"
    params = {
        "station_type": station_type,
        "station_id": station_id,
        "msg_id": msg_id,
        "cmd": "stop_camera",
    }
    logger.info(
        f"  Stopping vision system "
        f"(station_type={station_type}, station_id={station_id})..."
    )
    resp = http.get(url, params=params, timeout=30)
    body = resp.json()
    logger.info(f"  Stop response: {body}")
    return body

def post_to_pvs(http, payload, api_url):
    logger.info("=" * 80)
    logger.info(
        f"PVS REQUEST: drop={payload.get('drop_number')}, "
        f"station_type={payload.get('station_type')}, "
        f"station_id={payload.get('station_id')}"
    )

    for q in ("1", "2", "3", "4"):
        qdata = payload["data"].get(q, {})

        logger.info(
            f"Quadrant {q}: "
            f"slot={qdata.get('slot_number')} "
            f"device={qdata.get('device_id')} "
            f"system={qdata.get('system_id')} "
            f"drug_count={len(qdata.get('drug_data', {}))}"
        )

    resp = http.post(
        api_url,
        headers={
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"args": json.dumps(payload)},
        timeout=60,
    )

    logger.info(f"PVS HTTP Status: {resp.status_code}")

    time.sleep(2)

    result = resp.json()

    logger.info(
        f"PVS RESPONSE: {json.dumps(result, indent=2)}"
    )

    logger.info("=" * 80)

    return result


def process_pack(http, pack_row, config, filter_drops=None, save_dir=None):
    pack_id = pack_row["pack_id"]
    device_id = pack_row["device_id"]
    system_id = pack_row["system_id"]
    company_id = pack_row["company_id"]

    getpack_base = config["api"]["getpackdetails_base_url"]
    pvs_url = config["api"]["perform_vision_system_analysis_url"]
    # Base URL for the PVS host (scheme + host + port, no path).
    # Derived from the analysis URL if not set separately in config.
    pvs_base_url = config["api"].get(
        "pvs_base_url",
        "/".join(pvs_url.split("/")[:3]),  # e.g. http://172.29.0.68:13000
    )
    request_id = config.get("request_id", 54)
    msg_id = config.get("msg_id", 124)

    logger.info(f"--- Processing pack {pack_id} (device={device_id}, system={system_id}) ---")

    try:
        getpack_data = get_pack_details(http, pack_id, device_id, system_id, company_id, getpack_base)
    except requests.RequestException as e:
        logger.error(f"  FAILED getpackdetails: {e}")
        return False

    payloads = build_all_drop_payloads(getpack_data, system_id, request_id=request_id, msg_id=msg_id)

    if filter_drops:
        payloads = [(dn, p) for dn, p in payloads if dn in filter_drops]

    logger.info(f"  Generated {len(payloads)} drop payload(s)")

    if save_dir:
        save_payloads(payloads, save_dir, pack_id)

    all_ok = True
    for drop_number, payload in payloads:
        station_type = payload.get("station_type", 21)
        station_id = payload.get("station_id", 21000)
        slots_info = ", ".join(filter(None, (payload["data"][q]["slot_number"] for q in ("1", "2", "3", "4"))))

        # ── Init vision system before each drop analysis ──────────
        try:
            init_vision_system(http, pvs_base_url, station_type, station_id)
        except requests.RequestException as e:
            logger.error(f"  FAILED vision system init for drop {drop_number}: {e}")
            all_ok = False
            continue

        for q in ("1", "2", "3", "4"):
            qdata = payload["data"].get(q, {})

            logger.info(
                f"Drop={drop_number} "
                f"Q={q} "
                f"Slot={qdata.get('slot_number')} "
                f"Device={qdata.get('device_id')} "
                f"System={qdata.get('system_id')} "
                f"DrugCount={len(qdata.get('drug_data', {}))}"
            )

        logger.info(f"  POSTing drop {drop_number} (slots: {slots_info or '?'})...")
        try:
            pvs_resp = post_to_pvs(http, payload, pvs_url)

            resp_code = pvs_resp.get("resp_code")
            description = pvs_resp.get("description")
            data_code = pvs_resp.get("data")

            if data_code == 21209:
                logger.error(
                    f"DROP {drop_number}: "
                    f"PVS NOT INITIALIZED "
                    f"(resp_code={resp_code}, "
                    f"description={description}, "
                    f"data={data_code})"
                )

            logger.info(f"    PVS response: {pvs_resp}")
        except requests.RequestException as e:
            logger.error(f"    FAILED PVS POST: {e}")
            all_ok = False
        finally:
            pass

    return all_ok


def main():
    import argparse

    parser = argparse.ArgumentParser(description="PVS Automation")
    parser.add_argument("--test-pack", type=int, help="Test mode: process a single pack_id (device/system/company from config.json test_pack)")
    parser.add_argument("--no-post", action="store_true", help="Print payloads without POSTing")
    parser.add_argument("--save-payloads", "-s", metavar="DIR", help="Save payload JSON files to the given directory")
    parser.add_argument("--date", help="Override date for query: YYYY-MM-DD, last_N_days (e.g. last_2_days). Default: yesterday")
    args = parser.parse_args()

    config = load_config()

    # Apply date override to config (for query {date} replacement)
    if args.date:
        config.setdefault("database", {})["date"] = args.date

    # Initialise HTTP helper with auto token refresh
    http = TokenRefresher(config)
    http.ensure_token()

    if args.test_pack:
        tp = config.get("test_pack", {})

        device_ids = tp.get("device_ids", [])
        system_id = tp.get("system_id")
        company_id = tp.get("company_id")

        if not device_ids:
            logger.error("No device_ids configured in test_pack")
            sys.exit(1)

        pack_row = None

        for device_id in device_ids:
            try:
                logger.info(
                    f"Trying pack {args.test_pack} "
                    f"with device_id={device_id}, "
                    f"system_id={system_id}, "
                    f"company_id={company_id}"
                )

                getpack_data = get_pack_details(
                    http,
                    args.test_pack,
                    device_id,
                    system_id,
                    company_id,
                    config["api"]["getpackdetails_base_url"]
                )

                payloads = build_all_drop_payloads(
                    getpack_data,
                    system_id
                )

                if payloads:
                    logger.info(
                        f"Pack {args.test_pack} valid using device_id={device_id} "
                        f"({len(payloads)} payloads generated)"
                    )

                    pack_row = {
                        "pack_id": args.test_pack,
                        "device_id": device_id,
                        "system_id": system_id,
                        "company_id": company_id,
                    }
                    break

                logger.warning(
                    f"device_id={device_id} generated 0 payloads, trying next device"
                )

            except Exception as e:
                logger.warning(
                    f"device_id={device_id} failed: {e}"
                )

        if not pack_row:
            logger.error(
                f"Could not find pack {args.test_pack} "
                f"using any configured device_id"
            )
            sys.exit(1)
        if args.no_post:
            getpack_base = config["api"]["getpackdetails_base_url"]
            try:
                getpack_data = get_pack_details(http, pack_row["pack_id"],
                    pack_row["device_id"], pack_row["system_id"],
                    pack_row["company_id"], getpack_base)
            except requests.RequestException as e:
                logger.error(f"FAILED getpackdetails: {e}")
                sys.exit(1)
            payloads = build_all_drop_payloads(getpack_data, pack_row["system_id"])
            print(json.dumps([{"drop_number": dn, "payload": p} for dn, p in payloads], indent=2))
            if args.save_payloads:
                save_payloads(payloads, args.save_payloads, pack_row["pack_id"])
        else:
            process_pack(http, pack_row, config, save_dir=args.save_payloads)
        return

    # ── Full run ──
    try:
        conn = get_db_connection(config)
    except Exception as e:
        logger.error(f"DB connection failed: {e}")
        sys.exit(1)

    sql_query = resolve_query(config)
    raw = config["database"].get("date", "").strip() or "yesterday (default)"
    logger.info(f"Query date config: {raw}")

    try:
        rows = fetch_packs(conn, sql_query)
    except Exception as e:
        logger.error(f"Query failed: {e}")
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    if not rows:
        logger.info("No packs to process. Exiting.")
        return

    pack_drops = {}
    for row in rows:
        pid = row["pack_id"]
        pack_drops.setdefault(pid, []).append(row)

    ok_count = 0
    fail_count = 0
    for pack_id, pack_rows in pack_drops.items():
        pack_row = pack_rows[0]
        filter_drops = None
        if "drop_number" in pack_rows[0] and pack_rows[0]["drop_number"] is not None:
            filter_drops = {str(r["drop_number"]) for r in pack_rows}

        if process_pack(http, pack_row, config, filter_drops=filter_drops, save_dir=args.save_payloads):
            ok_count += 1
        else:
            fail_count += 1

    logger.info(f"=== Done. Successful: {ok_count}, Failed: {fail_count} ===")


if __name__ == "__main__":
    main()