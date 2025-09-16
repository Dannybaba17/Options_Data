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
        parsed = urlparse.urlparse(self.path)
        qs = urlparse.parse_qs(parsed.query)
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

def get_historical_candles(access_token, instrument_key, interval, to_date, from_date):
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"{HISTORICAL_URL}/{urlparse.quote(instrument_key)}/{interval}/{to_date}/{from_date}"
        resp = requests.get(url, headers=headers, timeout=15); resp.raise_for_status()
        return resp.json().get("data", {}).get("candles", [])
    except requests.HTTPError as e:
        if e.response.status_code == 404: return []
        else: print(f"HTTP Error fetching candles for {instrument_key}: {e}"); return []
    except Exception as e: print(f"Error fetching candles for {instrument_key}: {e}"); return []

def get_price_at_time(access_token, instrument_key, date_str, target_time_str):
    candles = get_historical_candles(access_token, instrument_key, "1minute", date_str, date_str)
    for candle in reversed(candles):
        if datetime.fromisoformat(candle[0]).strftime("%H:%M") <= target_time_str:
            return candle[4]
    return None

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

def enter_strangle(access_token, trade_logger, entry_date, expiry_date_str, historical_prices):
    print(f"  Attempting to enter new strangle on {entry_date.strftime('%Y-%m-%d')} for {expiry_date_str} expiry...")
    spot_price = get_price_at_time(access_token, NIFTY_UNDERLYING, entry_date.strftime('%Y-%m-%d'), "15:15")
    if not spot_price: print("  > Could not get spot price at 3:15 PM."); return None, historical_prices

    chain = get_option_chain(access_token, expiry_date_str)
    if not chain: print("  > Could not fetch option chain."); return None, historical_prices
    call_to_sell, put_to_sell = find_closest_strikes(chain, spot_price)
    if not call_to_sell or not put_to_sell: print("  > Could not find suitable strikes."); return None, historical_prices

    call_price = get_price_at_time(access_token, call_to_sell['instrument_key'], entry_date.strftime('%Y-%m-%d'), "15:15")
    put_price = get_price_at_time(access_token, put_to_sell['instrument_key'], entry_date.strftime('%Y-%m-%d'), "15:15")

    if call_price and put_price:
        historical_prices.append(spot_price)
        sd = calculate_std_dev(historical_prices)
        position = {'call': call_to_sell, 'put': put_to_sell, 'entry_spot': spot_price, 'sd_upper': spot_price + (2*sd), 'sd_lower': spot_price - (2*sd)}
        trade_logger.log(entry_date, position['call']['instrument_key'], "SELL", call_price, "Entry")
        trade_logger.log(entry_date, position['put']['instrument_key'], "SELL", put_price, "Entry")
        print(f"  > Entered strangle: SOLD {position['call']['instrument_key']} @ {call_price}, SOLD {position['put']['instrument_key']} @ {put_price}")
        return position, historical_prices
    print("  > Could not get option prices at 3:15 PM.")
    return None, historical_prices

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

            hist_candles = get_historical_candles(access_token, NIFTY_UNDERLYING, "day", entry_date.strftime('%Y-%m-%d'), (entry_date - timedelta(days=40)).strftime('%Y-%m-%d'))
            historical_nifty_prices = [c[4] for c in hist_candles]

            position, historical_nifty_prices = enter_strangle(access_token, trade_logger, entry_date, next_expiry_date.strftime('%Y-%m-%d'), historical_nifty_prices)
            if not position: print("  > Failed to enter position. Skipping cycle."); continue

            current_day = entry_date + timedelta(days=1)
            while current_day <= exit_date:
                day_str = current_day.strftime("%Y-%m-%d")
                daily_nifty_candles = get_historical_candles(access_token, NIFTY_UNDERLYING, "day", day_str, day_str)
                if not daily_nifty_candles: current_day += timedelta(days=1); continue

                nifty_close = daily_nifty_candles[0][4]

                if not (position['sd_lower'] < nifty_close < position['sd_upper']):
                    print(f"  ! STOP-LOSS on {day_str} at Nifty close {nifty_close}")
                    call_close = get_price_at_time(access_token, position['call']['instrument_key'], day_str, "15:15") or 0
                    put_close = get_price_at_time(access_token, position['put']['instrument_key'], day_str, "15:15") or 0
                    trade_logger.log(current_day, position['call']['instrument_key'], "BUY", call_close, "Stop-Loss")
                    trade_logger.log(current_day, position['put']['instrument_key'], "BUY", put_close, "Stop-Loss")

                    historical_nifty_prices.append(nifty_close)
                    position, historical_nifty_prices = enter_strangle(access_token, trade_logger, current_day, next_expiry_date.strftime('%Y-%m-%d'), historical_nifty_prices)
                    if not position: print("  > Failed to re-enter position. Ending cycle."); break
                else:
                    call_candles = get_historical_candles(access_token, position['call']['instrument_key'], "day", day_str, day_str)
                    put_candles = get_historical_candles(access_token, position['put']['instrument_key'], "day", day_str, day_str)
                    if call_candles and put_candles:
                        call_price, put_price = call_candles[0][4], put_candles[0][4]
                        if call_price > 3 * put_price or put_price > 3 * call_price:
                            print(f"  ! ADJUSTMENT on {day_str} (C:{call_price}, P:{put_price})")
                            chain = get_option_chain(access_token, next_expiry_date.strftime('%Y-%m-%d'))
                            if call_price < put_price:
                                trade_logger.log(current_day, position['call']['instrument_key'], "BUY", call_price, "Adjustment")
                                new_call = find_option_by_premium(chain, put_price, 'CE')
                                if new_call:
                                    new_price = get_price_at_time(access_token, new_call['instrument_key'], day_str, "15:15") or new_call.get('last_price', 0)
                                    trade_logger.log(current_day, new_call['instrument_key'], "SELL", new_price, "Adjustment")
                                    position['call'] = new_call
                            else:
                                trade_logger.log(current_day, position['put']['instrument_key'], "BUY", put_price, "Adjustment")
                                new_put = find_option_by_premium(chain, call_price, 'PE')
                                if new_put:
                                    new_price = get_price_at_time(access_token, new_put['instrument_key'], day_str, "15:15") or new_put.get('last_price', 0)
                                    trade_logger.log(current_day, new_put['instrument_key'], "SELL", new_price, "Adjustment")
                                    position['put'] = new_put
                current_day += timedelta(days=1)

            if position:
                print(f"  > Exiting position on {exit_date}")
                call_exit = get_price_at_time(access_token, position['call']['instrument_key'], exit_date.strftime('%Y-%m-%d'), "15:15") or 0
                put_exit = get_price_at_time(access_token, position['put']['instrument_key'], exit_date.strftime('%Y-%m-%d'), "15:15") or 0
                trade_logger.log(exit_date, position['call']['instrument_key'], "BUY", call_exit, "Cycle End")
                trade_logger.log(exit_date, position['put']['instrument_key'], "BUY", put_exit, "Cycle End")

    except Exception as e:
        print(f"\nAn error occurred: {e}", file=sys.stderr)
    finally:
        if server: server.shutdown()
        if 'trade_logger' in locals(): trade_logger.close()
        print("\nScript finished.")

if __name__ == "__main__":
    main()
