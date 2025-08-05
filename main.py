#he
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import asyncio
import websockets
from typing import Dict, Optional, List
from dotenv import load_dotenv
from datetime import datetime, timedelta

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import AzureChatOpenAI

# === Load Environment Variables ===
load_dotenv(dotenv_path=os.path.join("config", ".env"))

api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

# === Setup FastAPI app ===
app = FastAPI()

# Enable CORS for frontend access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Qubit WebSocket Configuration ===
QUBIT_WS_URL = "wss://api.tgx.finance/v1/ws/"
ORIGIN_HEADER = "https://test.qb.finance"

market_data: Dict[str, Dict] = {}
last_updated: Dict[str, datetime] = {}

class QubitWebSocketClient:
    def __init__(self):
        self.websocket = None
        self.is_connected = False
        self.ping_interval = 15
        self.reconnect_delay = 5
        self.active_subscriptions: List[Dict] = []

    async def connect(self):
        while True:
            try:
                self.websocket = await websockets.connect(
                    QUBIT_WS_URL,
                    ping_interval=self.ping_interval,
                    extra_headers={"Origin": ORIGIN_HEADER}
                )
                self.is_connected = True
                print("✅ Connected to Qubit WebSocket")

                await self.resubscribe()

                asyncio.create_task(self.listen_for_messages())
                asyncio.create_task(self.send_pings())
                return

            except Exception as e:
                print(f"❌ Connection failed: {e}. Retrying in {self.reconnect_delay}s...")
                self.is_connected = False
                await asyncio.sleep(self.reconnect_delay)

    async def subscribe(self, contract_code: str, topics: List[str]):
        for topic in topics:
            subscription = {
                "action": "sub",
                "data": {"contract_code": contract_code},
                "topic": topic
            }
            await self.websocket.send(json.dumps(subscription))
            self.active_subscriptions.append(subscription)
            print(f"🔔 Subscribed to {topic} for {contract_code}")

    async def resubscribe(self):
        for sub in self.active_subscriptions:
            await self.websocket.send(json.dumps(sub))
            print(f"🔄 Resubscribed to {sub['topic']}")

    async def send_pings(self):
        while self.is_connected:
            try:
                await self.websocket.send("ping")
                await asyncio.sleep(self.ping_interval)
            except Exception as e:
                print(f"⚠️ Ping failed: {e}")
                self.is_connected = False
                break

    async def listen_for_messages(self):
        global market_data, last_updated

        while self.is_connected:
            try:
                message = await self.websocket.recv()
                data = json.loads(message)

                print(f"📩 Received message: {data.get('topic')}")

                if data.get("action") == "notify":
                    topic = data.get("topic")
                    contract_code = data.get("data", {}).get("contract_code")

                    if not contract_code:
                        continue

                    market_data[contract_code] = data.get("data", {})
                    last_updated[contract_code] = datetime.now()

                    if topic == "market.ticker":
                        print(f"📊 Updated ticker for {contract_code}")

            except websockets.exceptions.ConnectionClosed:
                print("🔌 WebSocket connection closed")
                self.is_connected = False
            except json.JSONDecodeError:
                print(f"⚠️ Non-JSON message: {message}")
            except Exception as e:
                print(f"⚠️ Error processing message: {e}")
                self.is_connected = False

    async def disconnect(self):
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            print("🔌 Disconnected from WebSocket")

qubit_client = QubitWebSocketClient()

@app.on_event("startup")
async def startup_event():
    await qubit_client.connect()
    await qubit_client.subscribe("BTCUSDT", ["market.ticker", "contracts.market"])

@app.on_event("shutdown")
async def shutdown_event():
    await qubit_client.disconnect()

def normalize_symbol(symbol: str) -> str:
    return symbol.upper().replace("/", "").replace("-", "")

def is_data_fresh(symbol: str, max_age: int = 30) -> bool:
    normalized = normalize_symbol(symbol)
    if normalized not in last_updated:
        return False
    return (datetime.now() - last_updated[normalized]) < timedelta(seconds=max_age)

@tool
def get_crypto_price(symbol: str) -> str:
    try:
        normalized = normalize_symbol(symbol)

        if not is_data_fresh(normalized):
            return f"⚠️ Data for {symbol} is stale (>30 seconds old)"

        if normalized not in market_data:
            return f"❌ No data for {symbol}. Available: {list(market_data.keys())}"

        data = market_data[normalized]
        price_fields = ["last_price", "price", "mark_price", "close"]

        for field in price_fields:
            if field in data:
                return f"💵 {symbol}: {data[field]} USDT"

        return f"⚠️ No price field found for {symbol}. Data: {data}"

    except Exception as e:
        return f"❌ Error fetching price: {str(e)}"

@tool
def get_crypto_info(symbol: str) -> str:
    try:
        normalized = normalize_symbol(symbol)

        if not is_data_fresh(normalized):
            return f"⚠️ Data for {symbol} is stale (>30 seconds old)"

        if normalized not in market_data:
            return f"❌ No data for {symbol}"

        data = market_data[normalized]
        info = [f"📊 {symbol.upper()} Market Data"]

        fields = {
            "last_price": "Price",
            "high_price": "24h High",
            "low_price": "24h Low",
            "volume": "Volume",
            "change": "Change",
            "change_ratio": "Change %"
        }

        for field, label in fields.items():
            if field in data:
                value = f"{data[field]}%" if field == "change_ratio" else data[field]
                info.append(f"{label}: {value}")

        if normalized in last_updated:
            info.append(f"\n⏰ Updated: {last_updated[normalized].strftime('%Y-%m-%d %H:%M:%S')}")

        return "\n".join(info)

    except Exception as e:
        return f"❌ Error fetching info: {str(e)}"

@app.get("/subscribe/{contract_code}")
async def subscribe_to_contract(contract_code: str):
    if not qubit_client.is_connected:
        raise HTTPException(status_code=503, detail="WebSocket not connected")

    await qubit_client.subscribe(contract_code, ["market.ticker", "contracts.market"])
    return {"status": "subscribed", "contract": contract_code}

@app.get("/ask")
async def ask_stream(user_input: str):
    async def generate_response():
        try:
            client = AzureChatOpenAI(
                azure_endpoint=endpoint,
                azure_deployment=deployment,
                openai_api_version=api_version,
                api_key=azure_api_key,
            )
            client_with_tools = client.bind_tools([get_crypto_info, get_crypto_price])

            messages = [
                SystemMessage("You're a crypto assistant with real-time Qubit exchange data."),
                HumanMessage(user_input),
            ]

            result = await client_with_tools.ainvoke(messages)
            messages.append(result)

            if result.tool_calls:
                for tool_call in result.tool_calls:
                    yield json.dumps({
                        "type": "tool_call",
                        "name": tool_call["name"],
                        "args": tool_call["args"]
                    }) + "\n"

                    tool_fn = globals()[tool_call["name"]]
                    tool_result = tool_fn(**tool_call["args"])

                    messages.append(ToolMessage(
                        content=tool_result,
                        tool_call_id=tool_call["id"]
                    ))

                    yield json.dumps({
                        "type": "tool_result",
                        "content": tool_result
                    }) + "\n"

            async for chunk in client_with_tools.astream(messages):
                if chunk.content:
                    yield json.dumps({
                        "type": "text",
                        "content": chunk.content
                    }) + "\n"

        except Exception as e:
            yield json.dumps({
                "type": "error",
                "content": f"Error: {str(e)}"
            }) + "\n"

    return StreamingResponse(
        generate_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"}
    )

@app.get("/market_data")
async def get_market_data():
    return {
        "connected": qubit_client.is_connected,
        "subscriptions": qubit_client.active_subscriptions,
        "available_contracts": list(market_data.keys()),
        "last_updated": {k: v.isoformat() for k, v in last_updated.items()}
    }
