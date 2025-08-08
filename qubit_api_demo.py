import requests, time, hashlib, urllib.parse, json
import pandas as pd

BASE_URL = "https://api.tgx.finance"
TOKEN = "TGX_TOKEN"
SECRET_KEY = "TGX_SECRET"
ENCRYPTED_FUND_PASSWORD = "your_encrypted_fund_password_here"
CONTRACT_CODE = "BTCUSDT"

def get_sig(data: dict, public: dict):
    params = {**data, **public}
    params = {k: v for k, v in params.items() if v not in (None, "", [], {}) and not isinstance(v, (list, dict, bytes))}
    sorted_items = sorted(params.items())
    encoded = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted_items)
    string_to_sign = encoded + SECRET_KEY
    return hashlib.sha256(hashlib.md5(hashlib.md5(string_to_sign.encode()).hexdigest().encode()).hexdigest().encode()).hexdigest()

def signed_request(path, data={}):
    ts = int(time.time())
    public = {
        "api_version": "V2",
        "device": "Browser",
        "device_id": "bot-bogget",
        "version": "1.0.0",
        "req_os": 3,
        "req_lang": 1,
        "ts": ts,
    }
    sig = get_sig(data, public)
    payload = {**public, "data": data, "sig": sig}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    res = requests.post(BASE_URL + path, json=payload, headers=headers)
    try:
        return res.json()
    except:
        print("Raw:", res.text)
        raise

def get_kline(contract, duration="1m", limit=20):
    now = int(time.time())
    res = signed_request("/v1/contract/kline", {
        "contract_code": contract,
        "duration": duration,
        "start_time": now - limit * 60,
    })
    df = pd.DataFrame(res["data"])
    df.columns = ["timestamp", "open", "high", "low", "close", "volume"]
    df["close"] = df["close"].astype(float)
    return df

def place_order(side, price):
    print(f"Placing {'BUY' if side == 1 else 'SELL'} at {price}")
    res = signed_request("/v1/contract/order", {
        "contract_code": CONTRACT_CODE,
        "price": str(price),
        "volume": 1,
        "side": side,
        "type": 1,  # Limit
        "lever": 50,
        "open_type": 1,
        "order_from": 1,
        "fund_password": ENCRYPTED_FUND_PASSWORD
    })
    print("Order Response:", json.dumps(res, indent=2))

# Main loop
df = get_kline(CONTRACT_CODE)
df["ma5"] = df["close"].rolling(5).mean()
df["ma15"] = df["close"].rolling(15).mean()

prev = df.iloc[-2]
curr = df.iloc[-1]

if prev["ma5"] < prev["ma15"] and curr["ma5"] > curr["ma15"]:
    # Golden Cross → BUY
    place_order(1, curr["close"])
elif prev["ma5"] > prev["ma15"] and curr["ma5"] < curr["ma15"]:
    # Death Cross → SELL
    place_order(2, curr["close"])
else:
    print("No signal, sit tight 😴")
