"""
upstox_oauth_login.py

Requirements:
    pip install requests

How it works:
  1. Start a tiny HTTP server on localhost to receive the redirect with ?code=...
  2. Open the Upstox authorization URL in the default browser.
  3. After you log in and approve, Upstox redirects to your redirect_uri with code.
  4. The script exchanges that code for an access_token.
  5. It verifies the access_token by calling the /user/profile API.
  6. It fetches expired expiries for Nifty and sample expired contracts.
"""

from datetime import datetime, date, timedelta
import http.server
import socketserver
import threading
import urllib.parse as urlparse
import webbrowser
import requests
import sys
import time
from collections import defaultdict

# ======= CONFIGURE THESE =======
CLIENT_ID = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"   # client_id / API Key
CLIENT_SECRET = "xxxxxxxxxxx"                         # client_secret
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8080
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/"  # must match app settings

# Nifty underlying instrument key (index)
NIFTY_UNDERLYING = "NSE_INDEX|Nifty 50"
# ===============================

AUTH_URL = (
    "https://api.upstox.com/v2/login/authorization/dialog"
    "?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
)
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROFILE_URL = "https://api.upstox.com/v2/user/profile"
HISTORICAL_URL = "https://api.upstox.com/v2/historical-candle"

EXPIRED_EXPIRIES_URL = "https://api.upstox.com/v2/expired-instruments/expiries"
EXPIRED_OPTIONS_URL = "https://api.upstox.com/v2/expired-instruments/option/contract"

_received_auth_code = {"code": None, "error": None}

# ---------- Auth flow helpers ----------
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse.urlparse(self.path)
        qs = urlparse.parse_qs(parsed.query)
        if "code" in qs:
            code = qs["code"][0]
            _received_auth_code["code"] = code
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Login successful. You can close this window.</h2></body></html>")
        elif "error" in qs:
            _received_auth_code["error"] = qs.get("error_description", ["Authorization error"])[0]
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<html><body><h2>Authorization failed or denied.</h2></body></html>")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return  # suppress logs

def start_local_server(host, port):
    server = socketserver.TCPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread

def build_auth_url(client_id, redirect_uri):
    return AUTH_URL.format(
        client_id=urlparse.quote(client_id, safe=""),
        redirect_uri=urlparse.quote(redirect_uri, safe="")
    )

def exchange_code_for_token(code, client_id, client_secret, redirect_uri):
    headers = {"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15)
    resp.raise_for_status()
    return resp.json()

def verify_access_token(access_token):
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(PROFILE_URL, headers=headers, timeout=15)
    if resp.status_code == 200:
        print("✅ Successfully authorized. Profile data:")
        print(resp.json())
        return True
    else:
        print("❌ Authorization failed:", resp.status_code, resp.text)
        return False

# ---------- New/Modified helpers ----------
def filter_monthly_expiries(expiry_dates: list[str]) -> list[str]:
    """From a list of YYYY-MM-DD strings, return only the latest date of each month."""
    monthly_expiries_by_month = defaultdict(str)
    for date_str in expiry_dates:
        # Assuming date_str is 'YYYY-MM-DD'
        year_month = date_str[:7]
        if date_str > monthly_expiries_by_month[year_month]:
            monthly_expiries_by_month[year_month] = date_str
    return sorted(list(monthly_expiries_by_month.values()), reverse=True)

def get_historical_price(access_token, instrument_key, date_str):
    """Fetch historical candle data for a single day to get the opening price."""
    headers = {"Authorization": f"Bearer {access_token}"}
    instrument_key_encoded = urlparse.quote(instrument_key)
    interval = "day"
    url = f"{HISTORICAL_URL}/{instrument_key_encoded}/{interval}/{date_str}/{date_str}"

    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    candles = data.get("data", {}).get("candles", [])
    if candles:
        # candle format: [timestamp, open, high, low, close, volume, OI]
        return candles[0][1]  # return the opening price
    return None

def get_price_at_month_start(access_token, instrument_key, year, month):
    """Get the opening price of the instrument on the first trading day of the month."""
    # Try fetching for the first 5 days of the month to find the first trading day
    for day in range(1, 6):
        try:
            date_str = f"{year}-{month:02d}-{day:02d}"
            price = get_historical_price(access_token, instrument_key, date_str)
            if price:
                print(f"Found Nifty opening price for {date_str}: {price}")
                return price
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                continue # It's a holiday/weekend, try next day
            else:
                print(f"HTTP error fetching price for {date_str}: {e}", file=sys.stderr)
                return None # Or re-raise
        except Exception as e:
            print(f"Error fetching price for {date_str}: {e}", file=sys.stderr)
            return None
    print(f"Could not find Nifty price for start of {year}-{month:02d}", file=sys.stderr)
    return None

# ---------- Expired instruments helpers ----------
def get_expiries(access_token, instrument_key):
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"instrument_key": instrument_key}
    resp = requests.get(EXPIRED_EXPIRIES_URL, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    print("DEBUG /expired-instruments/expiries response:", data)
    if isinstance(data, dict) and "data" in data:
        return data["data"]  # directly the list
    return []


def get_expired_option_contracts(access_token, instrument_key, expiry_date):
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"instrument_key": instrument_key, "expiry_date": expiry_date}
    resp = requests.get(EXPIRED_OPTIONS_URL, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    print(f"DEBUG /expired-instruments/option/contract response for {expiry_date}:", data)
    if isinstance(data, dict):
        return data.get("data", {}).get("contracts", [])
    elif isinstance(data, list):
        return data
    else:
        return []


# ---------- Main ----------
def main():
    if CLIENT_ID.startswith("X") or CLIENT_SECRET.startswith("x"):
        print("Please edit the script and set CLIENT_ID and CLIENT_SECRET.", file=sys.stderr)
        sys.exit(1)

    print("Starting local server to receive redirect (Ctrl+C to quit)...")
    server, thread = start_local_server(REDIRECT_HOST, REDIRECT_PORT)

    try:
        auth_url = build_auth_url(CLIENT_ID, REDIRECT_URI)
        print("Opening browser for Upstox login. If browser does not open, visit this URL manually:\n")
        print(auth_url + "\n")
        webbrowser.open(auth_url)

        print("Waiting for authorization code... (complete login in the opened browser window)")
        start = time.time()
        TIMEOUT = 300  # seconds
        while time.time() - start < TIMEOUT:
            if _received_auth_code.get("code"):
                code = _received_auth_code["code"]
                print("Received authorization code:", code)
                break
            if _received_auth_code.get("error"):
                raise RuntimeError("Authorization error: " + str(_received_auth_code["error"]))
            time.sleep(0.5)
        else:
            raise TimeoutError("Didn't receive auth code within timeout. Check redirect_uri and browser login.")

        print("Exchanging code for tokens...")
        token_resp = exchange_code_for_token(code, CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)
        print("Token response (JSON):\n", token_resp)

        access_token = token_resp.get("access_token")
        if access_token and verify_access_token(access_token):
            # Step 1: Fetch all expired expiries for Nifty
            print("\n📌 Fetching all expired expiries for Nifty...")
            all_expiries = get_expiries(access_token, NIFTY_UNDERLYING)

            # Step 2: Filter for monthly expiries
            monthly_expiries = filter_monthly_expiries(all_expiries)
            print(f"\nFound {len(monthly_expiries)} monthly expiries (out of {len(all_expiries)} total):", monthly_expiries)

            # Step 3 & 4: For each monthly expiry, get Nifty price and filter contracts
            for expiry_date in monthly_expiries:
                print(f"\n\nProcessing monthly expiry: {expiry_date}")

                # Get Nifty price at the start of the month
                dt_expiry = datetime.strptime(expiry_date, "%Y-%m-%d")
                nifty_price_at_start = get_price_at_month_start(access_token, NIFTY_UNDERLYING, dt_expiry.year, dt_expiry.month)

                if nifty_price_at_start:
                    lower_bound = nifty_price_at_start * 0.90
                    upper_bound = nifty_price_at_start * 1.10
                    print(f"Nifty price range (+/- 10%): {lower_bound:.2f} - {upper_bound:.2f}")

                    # Fetch all contracts for this expiry
                    all_contracts = get_expired_option_contracts(access_token, NIFTY_UNDERLYING, expiry_date)

                    # Filter contracts by strike price
                    filtered_contracts = []
                    for contract in all_contracts:
                        try:
                            strike_price = float(contract.get("strike_price"))
                            if lower_bound <= strike_price <= upper_bound:
                                filtered_contracts.append(contract)
                        except (ValueError, TypeError):
                            # Handle cases where strike_price is missing or not a number
                            continue

                    print(f"Found {len(filtered_contracts)} contracts (out of {len(all_contracts)}) within the strike price range for expiry {expiry_date}.")
                    print("First few filtered contracts:")
                    for c in filtered_contracts[:5]:
                        print(c)
                else:
                    print(f"Skipping expiry {expiry_date} as Nifty start-of-month price could not be determined.")
        else:
            print("No access_token found in response!")

        print("\nReminder: Upstox tokens expire at 03:30 AM IST the following day.")

    except Exception as e:
        print("Error:", e, file=sys.stderr)
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
