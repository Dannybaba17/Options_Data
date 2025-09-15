import requests
import pandas as pd
import argparse
from urllib.parse import quote

def get_access_token(client_id, client_secret, redirect_uri, code):
    """
    Get the access token from the Upstox API.
    """
    url = "https://api-v2.upstox.com/login/authorization/token"
    headers = {
        'accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'code': code,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code'
    }
    response = requests.post(url, headers=headers, data=data)
    if response.status_code == 200:
        return response.json().get('access_token')
    else:
        print(f"Failed to get access token: {response.text}")
        return None

def get_expiry_dates(instrument_key, access_token):
    """
    Get the expiry dates for a given instrument.
    """
    # The API documentation is ambiguous here. The code review suggests the response is a list of dicts.
    # Let's try to get the full instrument list to be sure.
    # The documentation for "Get expired future contracts" might give a hint.
    # It says: "This API retrieves a list of all expired future contracts for a given underlying instrument key."
    # The response contains "expiry_date" and "instrument_key". This "instrument_key" is likely the expired_instrument_key.
    # So, let's assume the review is right.
    # I will use the "Get Expired Future Contracts" endpoint instead of "Get Expiries".
    # This seems more direct for getting the expired instrument keys.
    # The endpoint is /expired-instruments/futures/{underlying_key}

    encoded_instrument_key = quote(instrument_key)
    url = f"https://api-v2.upstox.com/expired-instruments/futures/{encoded_instrument_key}"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json'
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        # The response for this endpoint is a list of dicts, each with "instrument_key" and "expiry_date".
        # The "instrument_key" here is the expired_instrument_key we need.
        # Let's rename it in our code for clarity.
        data = response.json().get('data', [])
        return [{'expiry_date': item['expiry_date'], 'expired_instrument_key': item['instrument_key']} for item in data]
    else:
        print(f"Failed to get expiry dates: {response.text}")
        return None

def get_historical_data(expired_instrument_key, from_date, to_date, access_token):
    """
    Get historical OHLC data for an expired instrument.
    """
    interval = "day"
    # The expired_instrument_key needs to be URL-encoded.
    encoded_expired_instrument_key = quote(expired_instrument_key)
    url = f"https://api-v2.upstox.com/expired-instruments/historical/{encoded_expired_instrument_key}/{interval}/{to_date}/{from_date}"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json'
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('data', {}).get('candles', [])
    else:
        print(f"Failed to get historical data for {expired_instrument_key}: {response.text}")
        return None

def main():
    """
    Main function to run the script.
    """
    parser = argparse.ArgumentParser(description="Fetch Nifty expired contract data from Upstox API.")
    parser.add_argument("--client_id", required=True, help="Upstox API client_id")
    parser.add_argument("--client_secret", required=True, help="Upstox API client_secret")
    parser.add_argument("--redirect_uri", required=True, help="Upstox API redirect_uri")
    parser.add_argument("--code", help="Authorization code from the redirect URL. If not provided, the script will print the auth URL and exit.")
    parser.add_argument("--expiry_start_date", help="Start date (YYYY-MM-DD) to filter expiries.")
    parser.add_argument("--expiry_end_date", help="End date (YYYY-MM-DD) to filter expiries.")
    parser.add_argument("--data_start_date", help="Start date (YYYY-MM-DD) for fetching historical data for each expiry.")

    args = parser.parse_args()

    client_id = args.client_id
    client_secret = args.client_secret
    redirect_uri = args.redirect_uri
    code = args.code

    if not code:
        auth_url = f"https://api-v2.upstox.com/login/authorization/dialog?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code"
        print(f"\nPlease open the following URL in your browser to authorize the application:\n{auth_url}")
        print("\nAfter authorization, you will be redirected. The URL will look like: <your_redirect_uri>?code=<authorization_code>")
        print("Please rerun the script with the --code argument, providing the received authorization_code.")
        return

    # Get access token
    access_token = get_access_token(client_id, client_secret, redirect_uri, code)

    if not access_token:
        print("Failed to get access token. Exiting.")
        return

    # Get expiry dates
    nifty_instrument_key = "NSE_INDEX|Nifty 50"
    expiry_data = get_expiry_dates(nifty_instrument_key, access_token)

    if not expiry_data:
        print("Failed to get expiry dates. Exiting.")
        return

    if not args.expiry_start_date or not args.expiry_end_date or not args.data_start_date:
        print("Please provide --expiry_start_date, --expiry_end_date, and --data_start_date to fetch the data.")
        print("Available expiry dates:")
        for d in expiry_data:
            print(d['expiry_date'])
        return

    # Filter expiry dates
    filtered_expiry_data = [d for d in expiry_data if args.expiry_start_date <= d['expiry_date'] <= args.expiry_end_date]

    if not filtered_expiry_data:
        print("No expiry dates found in the specified range.")
        return

    # Fetch and save historical data
    for expiry_info in filtered_expiry_data:
        expiry_date = expiry_info['expiry_date']
        expired_instrument_key = expiry_info['expired_instrument_key']

        print(f"Fetching data for expiry: {expiry_date}")

        ohlc_data = get_historical_data(expired_instrument_key, args.data_start_date, expiry_date, access_token)

        if ohlc_data:
            df = pd.DataFrame(ohlc_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'open_interest'])
            filename = f"nifty_ohlc_{expiry_date}.csv"
            df.to_csv(filename, index=False)
            print(f"Data saved to {filename}")

if __name__ == "__main__":
    main()
