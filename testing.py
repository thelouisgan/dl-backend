#Crishpy2deepseek

#!/usr/bin/env python3

import time
from typing import List
import os
import json
import asyncio
import logging
import ssl
import socket
import signal
import aiohttp
from contextlib import closing, asynccontextmanager
from datetime import datetime
from typing import Optional, Dict, Any

import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# ===== Pydantic Models =====
class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    market_data: Optional[Dict] = None
    timestamp: str

# ===== Configuration =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('tgx_crypto_bot.log', mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# App configuration - TGX Finance
config = {
    "ws_url": os.getenv("WS_URL", "wss://api.tgx.finance/v1/ws/"),
    "ping_interval": int(os.getenv("WS_PING_INTERVAL", "30")),
    "ping_timeout": int(os.getenv("WS_PING_TIMEOUT", "10")),
    "close_timeout": int(os.getenv("WS_CLOSE_TIMEOUT", "10")),
    "heartbeat_interval": int(os.getenv("HEARTBEAT_INTERVAL", "25")),
    "default_port": int(os.getenv("PORT", "8000")),  # Changed to 8000 to match frontend
    "port_range": 100,
    "ssl_enabled": os.getenv("SSL_ENABLED", "true").lower() == "true",
    "max_reconnect_attempts": 5,
    "reconnect_delay": 2.0
}

# ===== Helper Functions =====
def find_available_port(start_port: int, max_attempts: int) -> int:
    """Find an available port in the given range"""
    for port in range(start_port, start_port + max_attempts):
        try:
            with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(('', port))
                return port
        except OSError:
            continue
    raise OSError(f"No available ports in range {start_port}-{start_port+max_attempts}")

def format_price(price_str: str) -> str:
    """Format price string to readable format"""
    try:
        price = float(price_str)
        return f"${price:,.2f}"
    except (ValueError, TypeError):
        return str(price_str)

# ===== WebSocket Manager =====
class TGXWebSocketManager:
    def __init__(self):
        self.connection = None
        self.connected = False
        self._stop_event = asyncio.Event()
        self._market_data = {
            "btc_ticker": {},
            "contract_info": {},
            "market_depth": {},
            "price_history": [],
            "connection_status": "disconnected",
            "last_update": None,
            "message_count": 0,
            "latest_raw_message": {}
        }
        self._reconnect_task = None
        self._heartbeat_task = None

    async def connect(self):
        if self.connected and self.connection is not None:
            # Check if connection is still alive
            try:
                if hasattr(self.connection, 'closed') and not self.connection.closed:
                    logger.info("Already connected to TGX Finance!")
                    return
            except AttributeError:
                # Handle the 'ClientConnection' object has no attribute 'closed' error
                logger.warning("Connection object doesn't have 'closed' attribute, reconnecting...")
                self.connected = False
                self.connection = None

        max_retries = config["max_reconnect_attempts"]
        retry_count = 0

        while retry_count < max_retries and not self._stop_event.is_set():
            try:
                logger.info(f"Connecting to TGX Finance WebSocket: {config['ws_url']} (attempt {retry_count + 1})")
                
                # Create SSL context
                ssl_context = None
                if config["ssl_enabled"]:
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE

                # Connect with proper timeout settings
                self.connection = await websockets.connect(
                    config["ws_url"],
                    ping_interval=config["ping_interval"],
                    ping_timeout=config["ping_timeout"],
                    ssl=ssl_context if config["ssl_enabled"] else None,
                    close_timeout=config["close_timeout"]
                )
                
                self.connected = True
                self._market_data["connection_status"] = "connected"
                logger.info("✅ Connected to TGX Finance successfully!")

                # Subscribe to feeds
                await self._subscribe_to_all_feeds()
                
                # Start heartbeat task
                if self._heartbeat_task is None or self._heartbeat_task.done():
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                
                # Listen for messages
                await self._listen_messages()
                break
                
            except Exception as e:
                self.connected = False
                self.connection = None
                self._market_data["connection_status"] = "disconnected"
                logger.error(f"Connection failed: {e}")
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(config["reconnect_delay"])

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to keep connection alive"""
        try:
            while self.connected and not self._stop_event.is_set():
                await asyncio.sleep(config["heartbeat_interval"])
                if self.connection and self.connected:
                    try:
                        # Check if connection has closed attribute and use it safely
                        if hasattr(self.connection, 'closed'):
                            if not self.connection.closed:
                                await self.connection.ping()
                                logger.debug("💗 Heartbeat sent")
                            else:
                                logger.warning("Connection is closed, stopping heartbeat")
                                break
                        else:
                            # If no closed attribute, just try to ping
                            await self.connection.ping()
                            logger.debug("💗 Heartbeat sent")
                    except Exception as e:
                        logger.warning(f"Heartbeat ping failed: {e}")
                        break
        except Exception as e:
            logger.error(f"Heartbeat loop error: {e}")
        finally:
            await self._cleanup_connection()

    async def _subscribe_to_all_feeds(self):
        """Subscribe to all TGX Finance data feeds"""
        try:
            logger.info("Starting subscription process...")
            
            # Try different subscription formats that TGX Finance might expect
            subscriptions = [
                # Format 1: Standard subscription
                {
                    "action": "sub",
                    "topic": "market.ticker",
                    "params": {"contract_code": "BTCUSDT"}
                },
                # Format 2: Alternative format
                {
                    "action": "subscribe",
                    "channel": "ticker.BTCUSDT"
                },
                # Format 3: Simple format
                {
                    "method": "SUBSCRIBE",
                    "params": ["ticker@BTCUSDT"],
                    "id": 1
                }
            ]

            for i, sub in enumerate(subscriptions):
                if self.connection and self.connected:
                    try:
                        message = json.dumps(sub)
                        await self.connection.send(message)
                        logger.info(f"Sent subscription {i+1}: {message}")
                        await asyncio.sleep(0.5)  # Wait for response
                        
                    except Exception as e:
                        logger.error(f"Failed to send subscription {i+1}: {e}")
            
            # Also try sending a ping/identification message
            try:
                ping_msg = {"action": "ping"}
                await self.connection.send(json.dumps(ping_msg))
                logger.info("Sent ping message")
            except Exception as e:
                logger.warning(f"Ping message failed: {e}")
                        
        except Exception as e:
            logger.error(f"Subscription error: {e}")

    async def _listen_messages(self):
        """Listen for incoming messages with robust error handling"""
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        try:
            while not self._stop_event.is_set() and self.connected:
                try:
                    # Use asyncio.wait_for to add timeout
                    message = await asyncio.wait_for(
                        self.connection.recv(), 
                        timeout=60.0
                    )
                    
                    consecutive_errors = 0
                    await self._process_tgx_message(message)
                    
                except asyncio.TimeoutError:
                    logger.debug("Message receive timeout - checking connection")
                    if self.connection:
                        try:
                            # Check if connection is still alive
                            if hasattr(self.connection, 'closed') and self.connection.closed:
                                logger.warning("Connection closed during timeout")
                                break
                            await self.connection.ping()
                        except:
                            logger.warning("Ping failed during timeout")
                            break
                        
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("📤 TGX Finance WebSocket connection closed")
                    self.connected = False
                    break
                    
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"Error receiving TGX message (#{consecutive_errors}): {e}")
                    
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error("Too many consecutive errors, disconnecting")
                        self.connected = False
                        break
                    
                    await asyncio.sleep(1)
                    
        except Exception as e:
            logger.error(f"Critical error in message listener: {e}")
        finally:
            await self._cleanup_connection()
            if not self._stop_event.is_set():
                logger.info("🔄 Scheduling reconnection...")
                asyncio.create_task(self._reconnect_with_delay())

    async def _process_tgx_message(self, message: str):
        """Process TGX Finance messages and update market data"""
        try:
            logger.info(f"📨 Raw message received: {message[:500]}...")
            
            data = json.loads(message)
            self._market_data["message_count"] += 1
            self._market_data["last_update"] = datetime.now().isoformat()
            
            # Store raw message for debugging
            self._market_data["latest_raw_message"] = data
            
            # Log the structure we received
            logger.info(f"📊 Message structure: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            
            # Handle different response formats
            if isinstance(data, dict):
                # Check for various possible field names and structures
                possible_price_fields = ['price', 'last', 'lastPrice', 'close', 'c']
                possible_volume_fields = ['volume', 'vol', 'v', 'baseVolume']
                possible_change_fields = ['change', 'change_percent', 'priceChangePercent', 'changePercent']
                
                # Try to extract ticker data from various possible structures
                ticker_data = None
                
                # Method 1: Direct fields in root
                if any(field in data for field in possible_price_fields):
                    ticker_data = data
                
                # Method 2: Check for nested data
                elif 'data' in data:
                    ticker_data = data['data']
                elif 'tick' in data:
                    ticker_data = data['tick']
                elif 'ticker' in data:
                    ticker_data = data['ticker']
                
                # Method 3: Check if it's an array with ticker data
                elif isinstance(data.get('data'), list) and len(data['data']) > 0:
                    ticker_data = data['data'][0]
                
                # If we found ticker data, update our market data
                if ticker_data and isinstance(ticker_data, dict):
                    updated_fields = []
                    
                    # Extract price
                    for field in possible_price_fields:
                        if field in ticker_data:
                            price_value = ticker_data[field]
                            self._market_data["btc_ticker"]["price"] = price_value
                            updated_fields.append(f"price: {price_value}")
                            
                            # Add to price history
                            self._market_data["price_history"].append({
                                "price": price_value,
                                "timestamp": datetime.now().isoformat()
                            })
                            if len(self._market_data["price_history"]) > 100:
                                self._market_data["price_history"] = self._market_data["price_history"][-100:]
                            break
                    
                    # Extract volume
                    for field in possible_volume_fields:
                        if field in ticker_data:
                            self._market_data["btc_ticker"]["volume"] = ticker_data[field]
                            updated_fields.append(f"volume: {ticker_data[field]}")
                            break
                    
                    # Extract change
                    for field in possible_change_fields:
                        if field in ticker_data:
                            self._market_data["btc_ticker"]["change_percent"] = ticker_data[field]
                            updated_fields.append(f"change: {ticker_data[field]}")
                            break
                    
                    # Extract high/low if available
                    for high_field in ['high', 'h', 'high24h', 'highPrice']:
                        if high_field in ticker_data:
                            self._market_data["btc_ticker"]["high"] = ticker_data[high_field]
                            updated_fields.append(f"high: {ticker_data[high_field]}")
                            break
                    
                    for low_field in ['low', 'l', 'low24h', 'lowPrice']:
                        if low_field in ticker_data:
                            self._market_data["btc_ticker"]["low"] = ticker_data[low_field]
                            updated_fields.append(f"low: {ticker_data[low_field]}")
                            break
                    
                    # Set symbol if available
                    for symbol_field in ['symbol', 'contract_code', 'pair', 's']:
                        if symbol_field in ticker_data:
                            self._market_data["btc_ticker"]["contract_code"] = ticker_data[symbol_field]
                            break
                    
                    if updated_fields:
                        logger.info(f"✅ Updated market data: {', '.join(updated_fields)}")
                    else:
                        logger.warning(f"⚠️ No recognizable fields found in: {list(ticker_data.keys())}")
                
                # Handle subscription confirmations or errors
                if 'result' in data:
                    logger.info(f"📢 Subscription result: {data['result']}")
                elif 'error' in data:
                    logger.error(f"❌ WebSocket error: {data['error']}")
                elif 'ping' in data or 'pong' in data:
                    logger.debug("💓 Ping/Pong received")
                else:
                    logger.info(f"🔍 Unrecognized message format: {data}")
            
            else:
                logger.warning(f"Received non-dict message: {type(data)}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}\nMessage: {message[:200]}...")
            # Try to handle as plain text
            if "price" in message.lower() or "btc" in message.lower():
                logger.info(f"📝 Plain text message (might contain data): {message}")
        except Exception as e:
            logger.error(f"Processing error: {e}\nData: {str(data) if 'data' in locals() else 'N/A'}")

    async def _reconnect_with_delay(self):
        """Reconnect after a delay"""
        await asyncio.sleep(config["reconnect_delay"])
        if not self._stop_event.is_set():
            await self.connect()

    async def _cleanup_connection(self):
        """Clean up any existing connection"""
        self.connected = False
        if self.connection:
            try:
                if hasattr(self.connection, 'close'):
                    await self.connection.close()
            except:
                pass
        self.connection = None
        self._market_data["connection_status"] = "disconnected"

    def get_market_summary(self) -> Dict[str, Any]:
        """Get a formatted market summary"""
        ticker = self._market_data.get("btc_ticker", {})
        
        summary = {
            "symbol": ticker.get("contract_code", "BTCUSDT"),
            "price": ticker.get("price", "N/A"),
            "change_24h": ticker.get("change_percent", "N/A"),
            "volume": ticker.get("volume", "N/A"),
            "high_24h": ticker.get("high", "N/A"),
            "low_24h": ticker.get("low", "N/A"),
            "connection_status": self._market_data["connection_status"],
            "last_update": self._market_data["last_update"],
            "message_count": self._market_data["message_count"]
        }
        
        return summary

    def get_full_market_data(self) -> Dict[str, Any]:
        """Get all market data"""
        return self._market_data.copy()

    async def close(self):
        """Clean shutdown"""
        logger.info("🔌 Closing TGX Finance WebSocket connection...")
        self._stop_event.set()
        
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            
        await self._cleanup_connection()
        logger.info("✅ TGX Finance connection closed")

# ===== AI Response Generator =====
class CryptoAI:
    def __init__(self, ws_manager: TGXWebSocketManager):
        self.ws_manager = ws_manager
        self._fallback_data = {}
        self._last_fallback_fetch = None
        
    async def _fetch_fallback_data(self):
        """Fetch data from a public API as fallback"""
        try:
            # Use a simple, reliable crypto API
            async with aiohttp.ClientSession() as session:
                async with session.get('https://api.coindesk.com/v1/bpi/currentprice.json', timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        btc_price = data['bpi']['USD']['rate'].replace(',', '')
                        self._fallback_data = {
                            "price": float(btc_price),
                            "source": "CoinDesk API",
                            "timestamp": datetime.now().isoformat()
                        }
                        self._last_fallback_fetch = datetime.now()
                        logger.info(f"📡 Fallback data fetched: BTC ${btc_price}")
                        return True
        except Exception as e:
            logger.warning(f"Fallback API fetch failed: {e}")
            
            # Try alternative API
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd', timeout=5) as response:
                        if response.status == 200:
                            data = await response.json()
                            btc_price = data['bitcoin']['usd']
                            self._fallback_data = {
                                "price": btc_price,
                                "source": "CoinGecko API",
                                "timestamp": datetime.now().isoformat()
                            }
                            self._last_fallback_fetch = datetime.now()
                            logger.info(f"📡 Fallback data fetched from CoinGecko: BTC ${btc_price}")
                            return True
            except Exception as e2:
                logger.warning(f"Alternative fallback API also failed: {e2}")
        
        return False
        
    def generate_response(self, query: str) -> str:
        """Generate AI-like responses based on query and market data"""
        query_lower = query.lower()
        market_data = self.ws_manager.get_market_summary()
        
        connection_status = market_data.get("connection_status", "unknown")
        message_count = market_data.get("message_count", 0)
        
        # Check if we should use fallback data
        use_fallback = (market_data.get("price", "N/A") == "N/A" and 
                       self._fallback_data.get("price") is not None)
        
        if any(word in query_lower for word in ["price", "cost", "value", "btc", "bitcoin"]):
            price = market_data.get("price", "N/A")
            change = market_data.get("change_24h", "N/A")
            
            # Use fallback data if TGX data unavailable
            if price == "N/A" and use_fallback:
                fallback_price = self._fallback_data["price"]
                source = self._fallback_data["source"]
                return f"💰 Bitcoin (BTC) is currently trading at {format_price(str(fallback_price))} (via {source} - TGX Finance connection: {connection_status}). Live TGX data coming soon!"
            elif price == "N/A":
                return f"💰 Bitcoin price data is currently unavailable. Connection status: {connection_status} ({message_count} messages received). I'm working on getting live data!"
            
            return f"💰 Bitcoin (BTC) is currently trading at {format_price(str(price))}. The 24h change is {change}%. Market data is live from TGX Finance."
        
        elif any(word in query_lower for word in ["volume", "trading", "activity"]):
            volume = market_data.get("volume", "N/A")
            if volume == "N/A":
                return f"📊 Trading volume data is currently unavailable. Connection: {connection_status} ({message_count} messages). Connecting to TGX Finance feeds..."
            return f"📊 Current BTC trading volume is {volume}. This indicates market activity from TGX Finance data."
        
        elif any(word in query_lower for word in ["high", "low", "range", "24h"]):
            high = market_data.get("high_24h", "N/A")
            low = market_data.get("low_24h", "N/A")
            if high == "N/A" or low == "N/A":
                return f"📈 Price range data is currently unavailable. Status: {connection_status} ({message_count} messages). Establishing TGX Finance connection..."
            return f"📈 Today's BTC range: High {format_price(str(high))} | Low {format_price(str(low))}. Trading range from TGX Finance."
        
        elif any(word in query_lower for word in ["status", "connection", "working", "online", "debug"]):
            raw_data = self.ws_manager.get_full_market_data()
            latest_msg = raw_data.get("latest_raw_message", {})
            fallback_info = f"\n• Fallback data: {self._fallback_data.get('source', 'None')} (${self._fallback_data.get('price', 'N/A')})" if self._fallback_data else ""
            return f"""🔍 **System Status:**
• Connection: {connection_status}
• Messages received: {message_count}
• Last update: {market_data.get('last_update', 'Never')}
• Latest message keys: {list(latest_msg.keys()) if latest_msg else 'None'}
• BTC ticker data: {list(raw_data.get('btc_ticker', {}).keys())}{fallback_info}

I'm actively working to establish a stable data feed from TGX Finance!"""
        
        elif any(word in query_lower for word in ["crypto", "market", "analysis", "trend"]):
            price = market_data.get("price", "N/A")
            change = market_data.get("change_24h", "N/A")
            
            if price == "N/A" and use_fallback:
                fallback_price = self._fallback_data["price"]
                return f"🔍 Current market analysis: BTC at {format_price(str(fallback_price))} (via backup feed). Full TGX Finance analysis coming once connection stabilizes!"
            elif price == "N/A":
                return f"🔍 Market analysis is currently limited due to data connectivity. Status: {connection_status}. I'm establishing live TGX Finance feeds to provide accurate analysis!"
            return f"🔍 Current market analysis: BTC at {format_price(str(price))} with {change}% 24h change. Live TGX Finance data analysis."
        
        else:
            # Provide more helpful info when no real data is available
            if message_count == 0:
                return f"🤖 I'm your crypto assistant connecting to TGX Finance! Status: {connection_status}. Ask me about prices, status, or market data!"
            elif market_data.get("price", "N/A") == "N/A":
                if use_fallback:
                    fallback_price = self._fallback_data["price"]
                    return f"🤖 Connected to TGX Finance ({message_count} messages received) with backup price feed active: BTC {format_price(str(fallback_price))}. Ask me about prices or market data!"
                else:
                    return f"🤖 Connected to TGX Finance ({message_count} messages received) but still waiting for price data. Ask me about connection status or try asking about BTC price!"
            else:
                return f"🤖 I'm your crypto assistant powered by live TGX Finance data! Current BTC: {format_price(str(market_data.get('price', 'N/A')))}. Ask me about prices, volume, ranges, or market analysis!"

    async def generate_streaming_response(self, query: str):
        """Generate streaming response in the format expected by frontend"""
        try:
            response_text = self.generate_response(query)
            market_data = self.ws_manager.get_market_summary()
            
            # Create response in the expected format
            response_obj = {
                "type": "text",
                "id": f"msg-{datetime.now().timestamp()}",
                "data": response_text
            }
            
            # Yield as JSON array (matching frontend expectation)
            yield json.dumps([response_obj]) + "\n"
            
        except Exception as e:
            logger.error(f"Streaming response error: {e}")
            error_obj = {
                "type": "text", 
                "id": f"error-{datetime.now().timestamp()}",
                "data": f"Sorry, there was an error processing your request: {str(e)}"
            }
            yield json.dumps([error_obj]) + "\n"

# ===== FastAPI App Setup =====
ws_manager = None
crypto_ai = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown"""
    global ws_manager, crypto_ai
    
    logger.info("🚀 Starting TGX Finance Crypto Bot...")
    
    ws_manager = TGXWebSocketManager()
    crypto_ai = CryptoAI(ws_manager)
    
    # Start connection in background task
    asyncio.create_task(ws_manager.connect())
    
    app.state.ws_manager = ws_manager
    app.state.crypto_ai = crypto_ai
    
    logger.info("✅ TGX Finance Crypto Bot startup complete!")
    
    yield
    
    logger.info("🛑 Shutting down TGX Finance Crypto Bot...")
    if ws_manager:
        await ws_manager.close()
    logger.info("✅ Shutdown complete")

app = FastAPI(
    title="TGX Finance Crypto Assistant",
    description="Real-time cryptocurrency assistant powered by TGX Finance WebSocket feeds",
    version="2.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ===== API Endpoints =====

@app.get("/ask")
async def ask_question(user_input: str):
    """Main ask endpoint with streaming response (matching frontend expectation)"""
    if not crypto_ai:
        return StreamingResponse(
            iter([json.dumps([{"type": "text", "id": "error", "data": "AI service not initialized"}]) + "\n"]), 
            media_type="text/plain"
        )
    
    logger.info(f"💬 Received query: '{user_input}'")
    
    return StreamingResponse(
        crypto_ai.generate_streaming_response(user_input),
        media_type="text/plain"
    )

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    """Alternative chat endpoint for JSON requests"""
    try:
        if not crypto_ai:
            raise HTTPException(status_code=503, detail="AI service not initialized")
        
        ai_response = crypto_ai.generate_response(request.message)
        market_summary = ws_manager.get_market_summary() if ws_manager else {}
        
        response = ChatResponse(
            response=ai_response,
            market_data=market_summary,
            timestamp=datetime.now().isoformat()
        )
        
        logger.info(f"💬 Chat query: '{request.message}' | Response: {len(ai_response)} chars")
        return response
        
    except Exception as e:
        logger.error(f"Chat endpoint error: {e}")
        raise HTTPException(status_code=500, detail=f"Chat service error: {str(e)}")

@app.get("/market-data")
async def get_market_data():
    """Get comprehensive market data"""
    try:
        if not ws_manager:
            raise HTTPException(status_code=503, detail="WebSocket manager not initialized")
        
        full_data = ws_manager.get_full_market_data()
        summary = ws_manager.get_market_summary()
        
        return {
            "connected": ws_manager.connected,
            "summary": summary,
            "full_data": full_data,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Market data endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Comprehensive health check"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {
            "websocket": {
                "connected": ws_manager.connected if ws_manager else False,
                "status": ws_manager._market_data.get("connection_status", "unknown") if ws_manager else "not_initialized"
            },
            "ai": {
                "available": crypto_ai is not None,
                "status": "ready" if crypto_ai else "not_initialized"
            },
            "market_data": {
                "available": bool(ws_manager.get_full_market_data() if ws_manager else False),
                "message_count": ws_manager._market_data.get("message_count", 0) if ws_manager else 0
            }
        }
    }
    
    if not ws_manager or not ws_manager.connected or not crypto_ai:
        health_status["status"] = "degraded"
    
    return health_status

@app.get("/")
async def root():
    """API documentation root"""
    return {
        "title": "TGX Finance Crypto Assistant API",
        "version": "2.1.0",
        "description": "Real-time crypto assistant with live TGX Finance data",
        "endpoints": {
            "/": "API documentation",
            "/health": "Health check and service status",
            "/market-data": "Live market data from TGX Finance",
            "/ask?user_input=<question>": "Ask crypto questions (streaming)",
            "/api/chat": "Main chat endpoint (POST with JSON)"
        },
        "websocket_status": ws_manager.connected if ws_manager else False,
        "market_feeds": [
            "market.ticker",
            "market.contract", 
            "market.depth"
        ]
    }

# ===== Signal Handlers =====
def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ===== Main Execution =====
if __name__ == "__main__":
    import uvicorn

    try:
        port = find_available_port(config["default_port"], config["port_range"])
        logger.info(f"🚀 Starting TGX Finance Crypto Bot on port {port}")
        
        # Kill any existing processes on the port
        try:
            import subprocess
            import time
            
            result = subprocess.run(f"lsof -ti:{port}", shell=True, capture_output=True, text=True)
            if result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                for pid in pids:
                    if pid:
                        subprocess.run(f"kill -9 {pid}", shell=True, check=False)
                        logger.info(f"🔥 Killed process {pid} on port {port}")
                time.sleep(1)
        except Exception as e:
            logger.warning(f"Port cleanup warning: {e}")

        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            timeout_keep_alive=60,
            reload=False,
            access_log=True
        )
        
    except OSError as e:
        logger.error(f"❌ Failed to start server: {e}")
        print(f"💡 Try running: lsof -ti:{config['default_port']} | xargs kill -9")
    except KeyboardInterrupt:
        logger.info("⌨️  Keyboard interrupt - server shutdown initiated")
    except Exception as e:
        logger.error(f"💥 Unexpected error: {e}")
        raise