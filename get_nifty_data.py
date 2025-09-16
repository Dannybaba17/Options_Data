"""
get_nifty_data.py

This script is a backtesting engine for a delta-neutral options trading strategy
on the Nifty 50 index, using the Upstox API.
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
import csv
import os
import numpy as np
import calendar

# ======= CONFIGURE THESE =======
CLIENT_ID = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
CLIENT_SECRET = "xxxxxxxxxxx"
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8080
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/"
NIFTY_UNDERLYING = "NSE_INDEX|Nifty 50"
# ===============================

# --- API URLs ---
AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROFILE_URL = "https://api.upstox.com/v2/user/profile"
HISTORICAL_URL = "https://api.upstox.com/v2/historical-candle"
OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"

_received_auth_code = {"code": None, "error": None}

# ---------- Auth flow helpers ----------
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse.urlparse(self.path); qs = urlparse.parse_qs(parsed.query)
        if "code" in qs: _received_auth_code["code"] = qs["code"][0]
        self.send_response(200); self.send_header("Content-type", "text/html"); self.end_headers()
        self.wfile.write(b"<html><body><h2>Login successful. You can close this window.</h2></body></html>")
    def log_message(self, format, *args): return

def start_local_server(host, port):
    server = socketserver.TCPServer((host, port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

def exchange_code_for_token(code, client_id, client_secret, redirect_uri):
    headers = {"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"code": code, "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
    resp = requests.post(TOKEN_URL, headers=headers, data=data, timeout=15); resp.raise_for_status()
    return resp.json()

def verify_access_token(access_token):
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(PROFILE_URL, headers=headers, timeout=15); resp.raise_for_status()
    print("✅ Successfully authorized.")
    return True

# ---------- Strategy & Data Helpers ----------
class TradeLogger:
    def __init__(self, filename="trade_log.csv"):
        self.filename = filename
        is_new = not os.path.exists(filename)
        self.file = open(self.filename, 'a', newline='')
        self.writer = csv.writer(self.file)
        if is_new: self.writer.writerow(["timestamp", "instrument_key", "action", "price", "reason"])
    def log(self, timestamp, instrument, action, price, reason=""):
        self.writer.writerow([timestamp, instrument, action, price, reason]); self.file.flush()
    def close(self):
        if self.file: self.file.close()

def calculate_std_dev(prices: list, period: int = 20) -> float:
    if len(prices) < period: return 0.0
    return np.std(prices[-period:])

def get_historical_candles(access_token, instrument_key, interval, from_date, to_date):
    # CORRECTED URL FORMAT: to_date comes before from_date
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"{HISTORICAL_URL}/{urlparse.quote(instrument_key)}/{interval}/{to_date}/{from_date}"
        resp = requests.get(url, headers=headers, timeout=15); resp.raise_for_status()
        return resp.json().get("data", {}).get("candles", [])
    except requests.HTTPError as e:
        if e.response.status_code == 404: return []
        else: print(f"HTTP Error fetching candles for {instrument_key} from {from_date} to {to_date}: {e}"); return []
    except Exception as e: print(f"Error fetching candles for {instrument_key}: {e}"); return []

def find_closest_strikes(chain, spot_price):
    target_call_strike, target_put_strike = spot_price * 1.05, spot_price * 0.95
    closest_call, closest_put = None, None
    min_call_diff, min_put_diff = float('inf'), float('inf')
    for option in chain:
        try:
            strike, o_type = float(option.get('strike_price')), option.get('option_type')
            diff = abs(strike - (target_call_strike if o_type == 'CE' else target_put_strike))
            if o_type == 'CE' and diff < min_call_diff: min_call_diff, closest_call = diff, option
            elif o_type == 'PE' and diff < min_put_diff: min_put_diff, closest_put = diff, option
        except (ValueError, TypeError): continue
    return closest_call, closest_put

def find_option_by_premium(chain, target_premium, option_type_to_find):
    closest_option, min_premium_diff = None, float('inf')
    for option in chain:
        if option.get('option_type') == option_type_to_find:
            try:
                ltp = float(option.get('last_price'))
                diff = abs(ltp - target_premium)
                if diff < min_premium_diff: min_premium_diff, closest_option = diff, option
            except (ValueError, TypeError): continue
    return closest_option

def get_option_chain(access_token, expiry_date):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    params = {"instrument_key": NIFTY_UNDERLYING, "expiry_date": expiry_date}
    resp = requests.get(OPTION_CHAIN_URL, headers=headers, params=params, timeout=30); resp.raise_for_status()
    return resp.json().get("data", [])

def generate_monthly_expiries(start_date, end_date):
    expiries = []
    year, month = start_date.year, start_date.month
    while date(year, month, 1) <= end_date:
        _, last_day = calendar.monthrange(year, month)
        for day in range(last_day, 0, -1):
            expiry_candidate = date(year, month, day)
            if expiry_candidate.weekday() == 3: # Thursday
                if expiry_candidate >= start_date: expiries.append(expiry_candidate)
                break
        month += 1
        if month > 12: month, year = 1, year + 1
    return sorted(expiries)

def get_daily_close(access_token, instrument_key, date_obj):
    from_date_str = to_date_str = date_obj.strftime("%Y-%m-%d")
    candles = get_historical_candles(access_token, instrument_key, "day", from_date_str, to_date_str)
    return candles[0][4] if candles else None

def enter_strangle(access_token, trade_logger, entry_date, expiry_date_str, historical_prices):
    print(f"  Attempting to enter new strangle on {entry_date.strftime('%Y-%m-%d')} for {expiry_date_str} expiry...")
    spot_price = get_daily_close(access_token, NIFTY_UNDERLYING, entry_date)
    if not spot_price: print("  > Could not get spot EOD price."); return None

    # CORRECTED: Calculate SD on prices BEFORE the current entry day to avoid lookahead bias
    sd = calculate_std_dev(historical_prices)

    chain = get_option_chain(access_token, expiry_date_str)
    if not chain: print("  > Could not fetch option chain."); return None
    call_to_sell, put_to_sell = find_closest_strikes(chain, spot_price)
    if not call_to_sell or not put_to_sell: print("  > Could not find suitable strikes."); return None

    call_price = get_daily_close(access_token, call_to_sell['instrument_key'], entry_date)
    put_price = get_daily_close(access_token, put_to_sell['instrument_key'], entry_date)

    if call_price and put_price:
        position = {'call': call_to_sell, 'put': put_to_sell, 'entry_spot': spot_price, 'sd_upper': spot_price + (2*sd), 'sd_lower': spot_price - (2*sd)}
        trade_logger.log(entry_date, position['call']['instrument_key'], "SELL", call_price, "Entry")
        trade_logger.log(entry_date, position['put']['instrument_key'], "SELL", put_price, "Entry")
        print(f"  > Entered strangle. SD band: {position['sd_lower']:.2f} - {position['sd_upper']:.2f}")
        return position
    print("  > Could not get option EOD prices.")
    return None

# ---------- Main Backtesting Engine ----------
def main():
    if CLIENT_ID.startswith("X"): sys.exit("Please edit the script and set CLIENT_ID and CLIENT_SECRET.")

    server = start_local_server(REDIRECT_HOST, REDIRECT_PORT)
    access_token = None
    try:
        webbrowser.open(f"{AUTH_URL.format(client_id=urlparse.quote(CLIENT_ID), redirect_uri=urlparse.quote(REDIRECT_URI))}")
        start_time = time.time()
        while time.time() - start_time < 300:
            if _received_auth_code.get("code"):
                token_resp = exchange_code_for_token(_received_auth_code["code"], CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)
                access_token = token_resp.get("access_token"); break
            time.sleep(0.5)

        if not access_token or not verify_access_token(access_token): raise ConnectionError("Failed to get a valid access token.")

        trade_logger = TradeLogger()
        end_date = date.today()
        start_date = end_date - timedelta(days=5*365)

        print(f"\n--- Initializing Backtest from {start_date} to {end_date} ---")
        monthly_expiries = generate_monthly_expiries(start_date, end_date)

        for i in range(len(monthly_expiries) - 1):
            entry_date = monthly_expiries[i]
            next_expiry_date = monthly_expiries[i+1]
            exit_date = next_expiry_date - timedelta(days=1)
            print(f"\n--- Cycle: {entry_date} -> {exit_date} ---")

            # CORRECTED: The order of dates for the API call is to_date, then from_date.
            to_date_hist = entry_date.strftime('%Y-%m-%d')
            from_date_hist = (entry_date - timedelta(days=40)).strftime('%Y-%m-%d')
            hist_candles = get_historical_candles(access_token, NIFTY_UNDERLYING, "day", to_date_hist, from_date_hist)
            historical_nifty_prices = [c[4] for c in hist_candles][-40:] # Trim to last 40 for efficiency

            position = enter_strangle(access_token, trade_logger, entry_date, next_expiry_date.strftime('%Y-%m-%d'), historical_nifty_prices)
            if not position: print("  > Failed to enter position. Skipping cycle."); continue

            current_day = entry_date + timedelta(days=1)
            while current_day <= exit_date:
                if current_day.weekday() >= 5: # Skip weekends
                    current_day += timedelta(days=1); continue

                nifty_close = get_daily_close(access_token, NIFTY_UNDERLYING, current_day)
                if not nifty_close: current_day += timedelta(days=1); continue # Skip holidays

                if not (position['sd_lower'] < nifty_close < position['sd_upper']):
                    print(f"  ! STOP-LOSS on {current_day} at Nifty close {nifty_close}")
                    call_close = get_daily_close(access_token, position['call']['instrument_key'], current_day) or 0
                    put_close = get_daily_close(access_token, position['put']['instrument_key'], current_day) or 0
                    trade_logger.log(current_day, position['call']['instrument_key'], "BUY", call_close, "Stop-Loss")
                    trade_logger.log(current_day, position['put']['instrument_key'], "BUY", put_close, "Stop-Loss")

                    historical_nifty_prices.append(nifty_close)
                    historical_nifty_prices = historical_nifty_prices[-40:] # Trim list
                    position = enter_strangle(access_token, trade_logger, current_day, next_expiry_date.strftime('%Y-%m-%d'), historical_nifty_prices)
                    if not position: print("  > Failed to re-enter position. Ending cycle."); break
                else:
                    call_price = get_daily_close(access_token, position['call']['instrument_key'], current_day)
                    put_price = get_daily_close(access_token, position['put']['instrument_key'], current_day)
                    if call_price and put_price:
                        if call_price > 3 * put_price or put_price > 3 * call_price:
                            print(f"  ! ADJUSTMENT on {current_day} (C:{call_price}, P:{put_price})")
                            chain = get_option_chain(access_token, next_expiry_date.strftime('%Y-%m-%d'))
                            if not chain: print("  > Could not get option chain for adjustment. Skipping adjustment."); continue

                            if call_price < put_price:
                                trade_logger.log(current_day, position['call']['instrument_key'], "BUY", call_price, "Adjustment")
                                new_call = find_option_by_premium(chain, put_price, 'CE')
                                if new_call:
                                    new_price = get_daily_close(access_token, new_call['instrument_key'], current_day) or new_call.get('last_price', 0)
                                    trade_logger.log(current_day, new_call['instrument_key'], "SELL", new_price, "Adjustment")
                                    position['call'] = new_call
                            else:
                                trade_logger.log(current_day, position['put']['instrument_key'], "BUY", put_price, "Adjustment")
                                new_put = find_option_by_premium(chain, call_price, 'PE')
                                if new_put:
                                    new_price = get_daily_close(access_token, new_put['instrument_key'], current_day) or new_put.get('last_price', 0)
                                    trade_logger.log(current_day, new_put['instrument_key'], "SELL", new_price, "Adjustment")
                                    position['put'] = new_put
                current_day += timedelta(days=1)

            if position:
                print(f"  > Exiting position on {exit_date}")
                call_exit = get_daily_close(access_token, position['call']['instrument_key'], exit_date) or 0
                put_exit = get_daily_close(access_token, position['put']['instrument_key'], exit_date) or 0
                trade_logger.log(exit_date, position['call']['instrument_key'], "BUY", call_exit, "Cycle End")
                trade_logger.log(exit_date, position['put']['instrument_key'], "BUY", put_exit, "Cycle End")

    except Exception as e:
        print(f"\nAn error occurred: {e}", file=sys.stderr)
    finally:
        if 'server' in locals() and server: server.shutdown()
        if 'trade_logger' in locals() and trade_logger: trade_logger.close()
        print("\nScript finished.")

if __name__ == "__main__":
    main()
