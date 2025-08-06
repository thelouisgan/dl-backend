#Crispy2testingv10000

#!/usr/bin/env python3
import os
import json
import asyncio
import logging
import ssl
import socket
import signal
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
    "default_port": int(os.getenv("PORT", "8001")),
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
            "message_count": 0
        }
        self._reconnect_task = None
        self._heartbeat_task = None

    async def connect(self):
        if self.connected and self.connection is not None:
            logger.info("Already connected to TGX Finance!")
            return

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

            # Connect without extra_headers
                self.connection = await websockets.connect(
                    config["ws_url"],
                    ping_interval=config.get("ping_interval", 20),
                    ping_timeout=config.get("ping_timeout", 10),
                    ssl=ssl_context if config["ssl_enabled"] else None,
                    close_timeout=config.get("close_timeout", 10)
                )
                self.connected = True
            
            # Update market data connection status
                self._market_data["connection_status"] = "connected"
                logger.info("✅ Connected to TGX Finance successfully!")

            # Start tasks with correct method names
                
            
            # Subscribe to feeds
                await self._subscribe_to_all_feeds()
            
            # Listen for messages
                await self._listen_messages()
                break
            
            except Exception as e:
                self.connected = False
                self.connection = None
                logger.error(f"Connection failed: {e}")
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(2)  # Wait before retry

    async def _heartbeat_loop(self):
        try:
            while True:
                await asyncio.sleep(config.get("heartbeat_interval", 25))
                if self.connection and self.connected:
                    await self.connection.ping()
                    logger.debug("💗 Heartbeat sent")
        except Exception as e:
            logger.warning(f"Heartbeat error: {e}")
            await self._cleanup_connection()
        
    async def _subscribe_to_all_feeds(self):
        try:
            subscriptions = [
                {
                    "action": "sub",
                    "topic": "market.ticker",
                    "params": {"contract_code": "BTCUSDT"}
                },
                {
                    "action": "sub", 
                    "topic": "market.contract",
                    "params": {"contract_code": "BTCUSDT"}
                },
                {
                    "action": "sub",
                    "topic": "market.depth",
                    "params": {"contract_code": "BTCUSDT"}
                }
            ]

            for sub in subscriptions:
                if self.connection and self.connected:
                    await self.connection.send(json.dumps(sub))
                    logger.info(f"Subscribed to: {sub['topic']}")
                    await asyncio.sleep(0.2)
                
        except Exception as e:
            logger.error(f"Subscription error: {e}")

    async def _listen_messages(self):
        consecutive_errors = 0
        max_retries = config.get("max_reconnect_attempts", 5)
        retry_count = 0
    
        try:
            async for message in self.connection:
                try:
                    # Process the message here
                    logger.info(f"Received message: {message}")
                    consecutive_errors = 0  # Reset on successful message
                
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"Message processing error: {e}")
                
                    if consecutive_errors > 10:
                        logger.error("Too many consecutive errors, disconnecting")
                        break
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket connection closed")
        except Exception as e:
            logger.error(f"Listen error: {e}")

        finally:
            await self._cleanup_connection()
                
        if retry_count < max_retries:
            delay = config["reconnect_delay"] * (2 ** (retry_count - 1))
            logger.info(f"Retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)
            retry_count += 1

        self._market_data["connection_status"] = "failed"
        logger.error("❌ Maximum connection attempts reached")



    async def _cleanup_connection(self):
        """Clean up any existing connection"""
        if self.connection:
            await self.connection.close()
        self.connected = False
        self._market_data["connection_status"] = "disconnected"

    async def _heartbeat_loop(self):
        while self.connected and not self._stop_event.is_set():
            try:
                await asyncio.sleep(config.get("heartbeat_interval", 25))
                if self.connection and self.connected:
                    await self.connection.ping()
                    logger.debug("♥️ Heartbeat sent")
            except Exception as e:
                logger.warning(f"Heartbeat error: {e}")
                await self._cleanup_connection()
                break

    async def _subscribe_to_all_feeds(self):
        try:
            logger.info("Starting subscription process...")
            logger.info(f"Connection state: {self.connected}, Connection object: {self.connection}")
            subscriptions = [
                {
                    "action": "sub",
                    "topic": "market.ticker",
                    "params": {"contract_code": "BTCUSDT"}  # Changed "data" → "params"
                },
                {
                    "action": "sub",
                    "topic": "market.contract",
                    "params": {"contract_code": "BTCUSDT"}  # Changed structure
                },
                {
                    "action": "sub",
                    "topic": "market.depth",
                    "params": {"contract_code": "BTCUSDT"}  # Simplified
                }
            ]
        
            for sub in subscriptions:
                if self.connection and self.connected:
                    await self.connection.send(json.dumps(sub))
                    logger.info(f"Subscribed to: {sub['topic']}")
                    await asyncio.sleep(0.2)
                else:
                    logger.error(f"Cannot subscribe - connection: {self.connection}, connected: {self.connected}")
                
        except Exception as e:
            logger.error(f"Subscription error: {e}")
            logger.error(f"Exception type: {type(e)}")
            raise

    async def _listen_messages(self):
        """Listen for incoming messages with robust error handling"""
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        try:
            while not self._stop_event.is_set() and self.connected:
                try:
                    message = await asyncio.wait_for(
                        self.connection.recv(), 
                        timeout=60.0
                    )
                    
                    consecutive_errors = 0
                    await self._process_tgx_message(message)
                    
                except asyncio.TimeoutError:
                    logger.debug("Message receive timeout - sending ping")
                    if self.connection and not self.connection.closed:
                        await self.connection.ping()
                        
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
            self.connected = False
            self._market_data["connection_status"] = "disconnected"
            if not self._stop_event.is_set():
                logger.info("🔄 Scheduling reconnection...")
                asyncio.create_task(self._reconnect_with_delay())

    async def _process_tgx_message(self, message: str):
        """Process TGX Finance messages and update market data"""
        try:
            data = json.loads(message)
            self._market_data["message_count"] += 1
            self._market_data["last_update"] = datetime.now().isoformat()
        
        # Skip if not a dictionary
            if not isinstance(data, dict):
                logger.debug(f"Received non-dict message: {str(data)[:100]}...")
                return
            
            topic = data.get('topic', 'unknown')
            action = data.get('action', 'unknown')
            msg_data = data.get('data', {})
        
        # Store raw message for debugging
            self._market_data["latest_raw_message"] = data
        
        # Handle different message types
            if action == "notify":
                if topic == "market.ticker":
                    if isinstance(msg_data, dict):
                        self._market_data["btc_ticker"] = msg_data  # Replace instead of update
                        if "price" in msg_data:
                            self._market_data["price_history"].append({
                                "price": msg_data["price"],
                                "timestamp": datetime.now().isoformat()
                            })
                        # Keep only last 100 prices
                            if len(self._market_data["price_history"]) > 100:
                                self._market_data["price_history"] = self._market_data["price_history"][-100:]
            
                elif topic == "market.contract" and isinstance(msg_data, dict):
                    self._market_data["contract_info"] = msg_data
            
                elif topic == "market.depth" and isinstance(msg_data, dict):
                    self._market_data["market_depth"] = msg_data
            
                elif topic == "contracts.market" and isinstance(msg_data, dict):
                    self._market_data["contract_info"] = msg_data
        
            logger.debug(f"Processed {topic} message")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}\nMessage: {message[:200]}...")
        except Exception as e:
            logger.error(f"Processing error: {e}\nData: {str(data)[:200] if 'data' in locals() else 'N/A'}")

    async def _reconnect_with_delay(self):
        """Reconnect after a delay"""
        await asyncio.sleep(config["reconnect_delay"])
        await self.connect()

    def get_market_summary(self) -> Dict[str, Any]:
        """Get a formatted market summary"""
        ticker = self._market_data.get("btc_ticker", {})
        contract = self._market_data.get("contract_info", {})
        
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
            
        if self.connection and not self.connection.closed:
            await self.connection.close()
        
        self.connected = False
        self._market_data["connection_status"] = "disconnected"
        logger.info("✅ TGX Finance connection closed")

# ===== AI Response Generator =====
class CryptoAI:
    def __init__(self, ws_manager: TGXWebSocketManager):
        self.ws_manager = ws_manager
        
    def generate_response(self, query: str) -> str:
        """Generate AI-like responses based on query and market data"""
        query_lower = query.lower()
        market_data = self.ws_manager.get_market_summary()
        
        if any(word in query_lower for word in ["price", "cost", "value", "btc", "bitcoin"]):
            price = market_data.get("price", "N/A")
            change = market_data.get("change_24h", "N/A")
            return f"💰 Bitcoin (BTC) is currently trading at {format_price(str(price))}. The 24h change is {change}%. Market data is live from TGX Finance."
        
        elif any(word in query_lower for word in ["volume", "trading", "activity"]):
            volume = market_data.get("volume", "N/A")
            return f"📊 Current BTC trading volume is {volume}. This indicates market activity from TGX Finance data."
        
        elif any(word in query_lower for word in ["high", "low", "range", "24h"]):
            high = market_data.get("high_24h", "N/A")
            low = market_data.get("low_24h", "N/A")
            return f"📈 Today's BTC range: High {format_price(str(high))} | Low {format_price(str(low))}. Trading range from TGX Finance."
        
        elif any(word in query_lower for word in ["status", "connection", "working", "online"]):
            status = market_data.get("connection_status", "unknown")
            msg_count = market_data.get("message_count", 0)
            return f"🟢 TGX Finance connection is {status}. Received {msg_count} market updates so far. All systems operational!"
        
        elif any(word in query_lower for word in ["crypto", "market", "analysis", "trend"]):
            price = market_data.get("price", "N/A")
            change = market_data.get("change_24h", "N/A")
            return f"🔍 Current market analysis: BTC at {format_price(str(price))} with {change}% 24h change. Live TGX Finance data analysis."
        
        else:
            return f"🤖 I'm your crypto assistant powered by live TGX Finance data! Current BTC: {format_price(str(market_data.get('price', 'N/A')))}. Ask me about prices, volume, ranges, or market analysis!"

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
    
    await ws_manager.connect()
    
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
    version="2.0.0",
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

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    """Main chat endpoint for frontend compatibility"""
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

@app.get("/ask")
async def ask_question(query: str):
    """Legacy ask endpoint with streaming response"""
    async def generate():
        try:
            if not crypto_ai:
                yield json.dumps({"error": "AI service not initialized"}) + "\n"
                return
            
            ai_response = crypto_ai.generate_response(query)
            market_data = ws_manager.get_market_summary() if ws_manager else {}
            
            response_data = {
                "query": query,
                "response": ai_response,
                "market_data": market_data,
                "timestamp": datetime.now().isoformat()
            }
            
            yield json.dumps(response_data) + "\n"
            
        except Exception as e:
            logger.error(f"Ask endpoint error: {e}")
            error_data = {
                "error": str(e), 
                "timestamp": datetime.now().isoformat()
            }
            yield json.dumps(error_data) + "\n"

    return StreamingResponse(generate(), media_type="application/json")

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
        "version": "2.0.0",
        "description": "Real-time crypto assistant with live TGX Finance data",
        "endpoints": {
            "/": "API documentation",
            "/health": "Health check and service status",
            "/market-data": "Live market data from TGX Finance",
            "/ask?query=<question>": "Ask crypto questions (streaming)",
            "/api/chat": "Main chat endpoint (POST with JSON)"
        },
        "websocket_status": ws_manager.connected if ws_manager else False,
        "market_feeds": [
            "market.ticker",
            "market.contract", 
            "contracts.market",
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