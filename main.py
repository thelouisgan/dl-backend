#CRISPY2FISH

import os
import json
import asyncio
import websockets
from typing import Dict, Optional, List, Any
from datetime import datetime, timedelta
import logging
from logging.handlers import RotatingFileHandler
import traceback
import signal
import sys

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.exception_handlers import http_exception_handler
from dotenv import load_dotenv

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import AzureChatOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# === Configure Logging ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(
            'app.log',
            maxBytes=5*1024*1024,  # 5MB
            backupCount=3
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# === Load Environment Variables ===
try:
    load_dotenv(dotenv_path=os.path.join("config", ".env"))
    required_env_vars = ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"]
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]
    if missing_vars:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")
except Exception as e:
    logger.critical(f"Failed to load environment variables: {e}")
    sys.exit(1)

api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

# === Setup FastAPI app ===
app = FastAPI(
    title="Crypto Assistant API",
    description="Real-time cryptocurrency data assistant with Qubit integration",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Security middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# === Qubit WebSocket Configuration ===
QUBIT_WS_URL = "wss://api.tgx.finance/v1/ws/"
ORIGIN_HEADER = "https://test.qb.finance"
MAX_RECONNECT_ATTEMPTS = 10
PING_INTERVAL = 15  # seconds
RECONNECT_DELAY = 5  # seconds

# Thread-safe data storage
class MarketDataStore:
    def __init__(self):
        self._data: Dict[str, Dict] = {}
        self._timestamps: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()
    
    async def update(self, contract_code: str, data: Dict):
        async with self._lock:
            self._data[contract_code] = data
            self._timestamps[contract_code] = datetime.now()
    
    async def get(self, contract_code: str) -> Optional[Dict]:
        async with self._lock:
            return self._data.get(contract_code)
    
    async def get_timestamp(self, contract_code: str) -> Optional[datetime]:
        async with self._lock:
            return self._timestamps.get(contract_code)
    
    async def get_all_contracts(self) -> List[str]:
        async with self._lock:
            return list(self._data.keys())

market_store = MarketDataStore()

class QubitWebSocketClient:
    def __init__(self):
        self.websocket = None
        self.is_connected = False
        self._should_reconnect = True
        self.reconnect_attempts = 0
        self.active_subscriptions: List[Dict] = []
        self._connection_lock = asyncio.Lock()
        self._subscription_lock = asyncio.Lock()
        self._ping_task = None
        self._listen_task = None

    async def _ensure_connection(self):
        """Ensure we have an active connection"""
        async with self._connection_lock:
            if not self.is_connected or self.websocket is None or self.websocket.closed:
                await self.connect()

    @retry(
        stop=stop_after_attempt(MAX_RECONNECT_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=4, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True
    )
    async def connect(self):
        """Establish WebSocket connection with auto-reconnect"""
        try:
            logger.info("Attempting to connect to Qubit WebSocket...")
            self.websocket = await websockets.connect(
                QUBIT_WS_URL,
                ping_interval=PING_INTERVAL,
                ping_timeout=PING_INTERVAL + 5,
                close_timeout=0,
                extra_headers={"Origin": ORIGIN_HEADER}
            )
            self.is_connected = True
            self.reconnect_attempts = 0
            logger.info("✅ Successfully connected to Qubit WebSocket")
            
            # Start background tasks
            self._ping_task = asyncio.create_task(self._send_pings())
            self._listen_task = asyncio.create_task(self._listen_for_messages())
            
            # Resubscribe to any active subscriptions
            await self._resubscribe()
            
        except Exception as e:
            self.is_connected = False
            self.reconnect_attempts += 1
            logger.error(f"❌ Connection attempt {self.reconnect_attempts} failed: {e}")
            if self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                logger.critical("Max reconnection attempts reached. Giving up.")
                raise
            raise e

    async def _resubscribe(self):
        """Resubscribe to all active subscriptions"""
        if not self.active_subscriptions:
            return
            
        async with self._subscription_lock:
            for sub in self.active_subscriptions:
                try:
                    await self.websocket.send(json.dumps(sub))
                    logger.info(f"🔄 Resubscribed to {sub['topic']}")
                except Exception as e:
                    logger.error(f"Failed to resubscribe to {sub['topic']}: {e}")

    async def subscribe(self, contract_code: str, topics: List[str]):
        """Subscribe to specific market topics"""
        await self._ensure_connection()
        
        async with self._subscription_lock:
            for topic in topics:
                subscription = {
                    "action": "sub",
                    "data": {"contract_code": contract_code},
                    "topic": topic
                }
                try:
                    await self.websocket.send(json.dumps(subscription))
                    self.active_subscriptions.append(subscription)
                    logger.info(f"🔔 Subscribed to {topic} for {contract_code}")
                except Exception as e:
                    logger.error(f"Failed to subscribe to {topic}: {e}")
                    raise

    async def _send_pings(self):
        """Send regular pings to maintain connection"""
        while self.is_connected and self._should_reconnect:
            try:
                await asyncio.sleep(PING_INTERVAL)
                if self.websocket and not self.websocket.closed:
                    await self.websocket.send("ping")
                    logger.debug("Sent ping to server")
            except Exception as e:
                logger.error(f"⚠️ Ping failed: {e}")
                self.is_connected = False
                break

    async def _listen_for_messages(self):
        """Process incoming WebSocket messages"""
        while self.is_connected and self._should_reconnect:
            try:
                message = await self.websocket.recv()
                logger.debug(f"Received message: {message[:100]}...")  # Log first 100 chars
                
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(f"Received non-JSON message: {message}")
                    continue
                
                if data.get("action") == "notify":
                    topic = data.get("topic")
                    contract_code = data.get("data", {}).get("contract_code")
                    
                    if not contract_code:
                        logger.warning(f"No contract code in message: {data}")
                        continue
                        
                    try:
                        await market_store.update(contract_code, data.get("data", {}))
                        if topic == "market.ticker":
                            logger.debug(f"Updated ticker for {contract_code}")
                    except Exception as e:
                        logger.error(f"Failed to update market data: {e}")
                        
            except websockets.exceptions.ConnectionClosed as e:
                logger.error(f"WebSocket connection closed: {e}")
                self.is_connected = False
                await self._handle_disconnect()
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                self.is_connected = False
                await self._handle_disconnect()

    async def _handle_disconnect(self):
        """Handle disconnection and attempt reconnect"""
        if not self._should_reconnect:
            return
            
        logger.info("Attempting to reconnect...")
        try:
            await self.connect()
        except Exception as e:
            logger.error(f"Reconnect failed: {e}")
            # Schedule another reconnect attempt
            asyncio.create_task(self._delayed_reconnect())

    async def _delayed_reconnect(self):
        """Wait before attempting to reconnect"""
        await asyncio.sleep(RECONNECT_DELAY)
        await self._handle_disconnect()

    async def disconnect(self):
        """Cleanly disconnect from WebSocket"""
        self._should_reconnect = False
        
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
                
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
                
        if self.websocket and not self.websocket.closed:
            await self.websocket.close()
            logger.info("🔌 Disconnected from WebSocket")
        
        self.is_connected = False

# Initialize WebSocket client
qubit_client = QubitWebSocketClient()

# === Signal Handlers ===
async def shutdown_handler():
    """Handle application shutdown"""
    logger.info("Shutting down gracefully...")
    await qubit_client.disconnect()

def handle_signal(signum, frame):
    """Handle OS signals"""
    logger.info(f"Received signal {signum}, initiating shutdown...")
    asyncio.create_task(shutdown_handler())

# Register signal handlers
signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# === FastAPI Event Handlers ===
@app.on_event("startup")
async def startup_event():
    """Initialize WebSocket connection and subscribe to BTC/USDT by default"""
    logger.info("Starting up application...")
    try:
        await qubit_client.connect()
        await qubit_client.subscribe("BTCUSDT", ["market.ticker", "contracts.market"])
    except Exception as e:
        logger.critical(f"Failed to initialize WebSocket: {e}")
        raise

@app.on_event("shutdown")
async def shutdown_event():
    """Clean up WebSocket connection"""
    await shutdown_handler()

# === Utility Functions ===
def normalize_symbol(symbol: str) -> str:
    """Convert symbol formats (BTC/USDT -> BTCUSDT)"""
    return symbol.upper().replace("/", "").replace("-", "")

async def is_data_fresh(symbol: str, max_age: int = 30) -> bool:
    """Check if market data is recent enough"""
    normalized = normalize_symbol(symbol)
    timestamp = await market_store.get_timestamp(normalized)
    if not timestamp:
        return False
    return (datetime.now() - timestamp) < timedelta(seconds=max_age)

# === Tool Functions ===
@tool
async def get_crypto_price(symbol: str) -> str:
    """Get current cryptocurrency price from Qubit. Input should be a trading pair like 'BTC/USDT'."""
    try:
        normalized = normalize_symbol(symbol)
        
        if not await is_data_fresh(normalized):
            return f"⚠️ Data for {symbol} is stale (>30 seconds old)"
            
        data = await market_store.get(normalized)
        if not data:
            available = await market_store.get_all_contracts()
            return f"❌ No data for {symbol}. Available: {available}"
            
        price_fields = ["last_price", "price", "mark_price", "close"]
        
        for field in price_fields:
            if field in data:
                return f"💵 {symbol}: {data[field]} USDT"
                
        return f"⚠️ No price field found for {symbol}. Data: {json.dumps(data, indent=2)}"
        
    except Exception as e:
        logger.error(f"Error in get_crypto_price: {e}\n{traceback.format_exc()}")
        return f"❌ Error fetching price: {str(e)}"

@tool
async def get_crypto_info(symbol: str) -> str:
    """Get detailed cryptocurrency market data. Input should be a trading pair like 'BTC/USDT'."""
    try:
        normalized = normalize_symbol(symbol)
        
        if not await is_data_fresh(normalized):
            return f"⚠️ Data for {symbol} is stale (>30 seconds old)"
            
        data = await market_store.get(normalized)
        if not data:
            return f"❌ No data for {symbol}"
            
        info = [f"📊 {symbol.upper()} Market Data"]
        
        fields = {
            "last_price": "Price",
            "high_price": "24h High",
            "low_price": "24h Low",
            "volume": "Volume",
            "change": "Change",
            "change_ratio": "Change %",
            "funding_rate": "Funding Rate",
            "open_interest": "Open Interest"
        }
        
        for field, label in fields.items():
            if field in data:
                value = f"{data[field]}%" if field.endswith("_ratio") or field == "change" else data[field]
                info.append(f"{label}: {value}")
                
        timestamp = await market_store.get_timestamp(normalized)
        if timestamp:
            info.append(f"\n⏰ Updated: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
            
        return "\n".join(info)
        
    except Exception as e:
        logger.error(f"Error in get_crypto_info: {e}\n{traceback.format_exc()}")
        return f"❌ Error fetching info: {str(e)}"

# === API Endpoints ===
@app.get("/subscribe/{contract_code}")
async def subscribe_to_contract(contract_code: str):
    """Manually subscribe to a new contract"""
    try:
        if not qubit_client.is_connected:
            raise HTTPException(status_code=503, detail="WebSocket not connected")
            
        await qubit_client.subscribe(contract_code, ["market.ticker", "contracts.market"])
        return {"status": "subscribed", "contract": contract_code}
    except Exception as e:
        logger.error(f"Subscription failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/ask")
async def ask_stream(user_input: str):
    """Streaming endpoint for AI assistant"""
    async def generate_response():
        try:
            # Initialize AI client with retry
            @retry(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=4, max=10),
                retry=retry_if_exception_type(Exception)
            )
            async def get_ai_client():
                return AzureChatOpenAI(
                    azure_endpoint=endpoint,
                    azure_deployment=deployment,
                    openai_api_version=api_version,
                    api_key=azure_api_key,
                    timeout=30,
                    max_retries=3
                )

            client = await get_ai_client()
            client_with_tools = client.bind_tools([get_crypto_info, get_crypto_price])

            # Prepare conversation
            messages = [
                SystemMessage("You're a crypto assistant with real-time Qubit exchange data. Be concise but helpful."),
                HumanMessage(user_input),
            ]

            # Get initial response
            try:
                result = await client.ainvoke(messages)
                messages.append(result)
            except Exception as e:
                logger.error(f"AI invocation failed: {e}")
                yield json.dumps({
                    "type": "error",
                    "content": "Sorry, I encountered an error processing your request."
                }) + "\n"
                return

            # Handle tool calls
            if hasattr(result, 'tool_calls') and result.tool_calls:
                for tool_call in result.tool_calls:
                    yield json.dumps({
                        "type": "tool_call",
                        "name": tool_call["name"],
                        "args": tool_call["args"]
                    }) + "\n"

                    # Execute tool
                    try:
                        tool_fn = globals()[tool_call["name"]]
                        tool_result = await tool_fn.ainvoke(tool_call["args"])
                        messages.append(ToolMessage(
                            content=tool_result,
                            tool_call_id=tool_call["id"]
                        ))

                        yield json.dumps({
                            "type": "tool_result",
                            "content": tool_result
                        }) + "\n"
                    except Exception as e:
                        logger.error(f"Tool execution failed: {e}")
                        yield json.dumps({
                            "type": "error",
                            "content": f"Failed to execute {tool_call['name']}: {str(e)}"
                        }) + "\n"

            # Stream final response
            try:
                async for chunk in client_with_tools.astream(messages):
                    if hasattr(chunk, 'content') and chunk.content:
                        yield json.dumps({
                            "type": "text",
                            "content": chunk.content
                        }) + "\n"
            except Exception as e:
                logger.error(f"Streaming failed: {e}")
                yield json.dumps({
                    "type": "error",
                    "content": "Streaming response failed"
                }) + "\n"

        except Exception as e:
            logger.error(f"Error in ask_stream: {e}\n{traceback.format_exc()}")
            yield json.dumps({
                "type": "error",
                "content": f"An unexpected error occurred: {str(e)}"
            }) + "\n"

    return StreamingResponse(
        generate_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable buffering for nginx
        }
    )

@app.get("/market_data")
async def get_market_data():
    """Debug endpoint for market data"""
    try:
        contracts = await market_store.get_all_contracts()
        timestamps = {}
        
        for contract in contracts:
            ts = await market_store.get_timestamp(contract)
            if ts:
                timestamps[contract] = ts.isoformat()
        
        return {
            "connected": qubit_client.is_connected,
            "subscriptions": qubit_client.active_subscriptions,
            "available_contracts": contracts,
            "last_updated": timestamps
        }
    except Exception as e:
        logger.error(f"Error in market_data endpoint: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# === Exception Handlers ===
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler"""
    logger.error(f"Unhandled exception: {exc}\n{traceback.format_exc()}")
    return await http_exception_handler(request, exc)

@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTP exception handler"""
    logger.error(f"HTTP error {exc.status_code}: {exc.detail}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code
        }
    )

# === Health Check ===
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "websocket_connected": qubit_client.is_connected,
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,
        access_log=False,
        timeout_keep_alive=60
    )