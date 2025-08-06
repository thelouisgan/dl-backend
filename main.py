#Crispy2qbit
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
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

# ===== Configuration =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log', mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# App configuration - removed required Azure vars for now
config = {
    "api_version": os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    "deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4"),
    "ws_url": os.getenv("WS_URL", "wss://api.tgx.finance/v1/ws/"),  # Using Binance as test
    "ping_interval": int(os.getenv("WS_PING_INTERVAL", "30")),
    "default_port": int(os.getenv("PORT", "8001")),
    "port_range": 100,
    "ssl_enabled": os.getenv("SSL_ENABLED", "true").lower() == "true"
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

# ===== WebSocket Manager =====
class WebSocketManager:
    def __init__(self):
        self.connection = None
        self.connected = False
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._message_queue = asyncio.Queue()
        self._market_data = {}
        self._reconnect_task = None

    async def connect(self):
        """Establish WebSocket connection with retry logic"""
        if self.connected:
            return

        try:
            logger.info(f"Connecting to Qubit WebSocket: {config['ws_url']}")
            
            # Create SSL context for TGX Finance
            ssl_context = None
            if config["ssl_enabled"]:
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

            self.connection = await websockets.connect(
                config["ws_url"],
                ping_interval=config["ping_interval"],
                ping_timeout=10,
                ssl=ssl_context
            )
            
            self.connected = True
            logger.info("Connected to Qubit successfully!")
            
            # Subscribe to market data immediately after connection
            await self._subscribe_to_market_data()
            
            # Start listening task
            asyncio.create_task(self._listen_messages())
            
        except Exception as e:
            logger.error(f"Qubit WebSocket connection failed: {e}")
            self.connected = False
            # Schedule reconnection
            if not self._reconnect_task or self._reconnect_task.done():
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _subscribe_to_market_data(self):
        """Subscribe to Qubit market data feeds"""
        try:
            # Subscribe to BTC market data (based on your screenshots)
            subscription_messages = [
                {
                    "action": "sub",
                    "data": {"contract_code": "BTCUSDT"},
                    "topic": "market.ticker"
                },
                {
                    "action": "sub", 
                    "data": {"contract_code": "BTCUSDT"},
                    "topic": "market.contract"
                },
                {
                    "action": "sub",
                    "data": {},
                    "topic": "contracts.market"
                }
            ]
            
            for message in subscription_messages:
                await self.connection.send(json.dumps(message))
                logger.info(f"Subscribed to: {message['topic']}")
                await asyncio.sleep(0.1)  # Small delay between subscriptions
                
        except Exception as e:
            logger.error(f"Failed to subscribe to market data: {e}")

    async def _reconnect_loop(self):
        """Handle reconnection with exponential backoff"""
        wait_time = 1
        while not self._stop_event.is_set() and not self.connected:
            await asyncio.sleep(wait_time)
            try:
                await self.connect()
                wait_time = 1  # Reset on successful connection
            except Exception as e:
                logger.error(f"Reconnection failed: {e}")
                wait_time = min(wait_time * 2, 30)  # Exponential backoff, max 30s

    async def _listen_messages(self):
        """Listen for incoming messages"""
        try:
            while not self._stop_event.is_set() and self.connected:
                try:
                    message = await asyncio.wait_for(self.connection.recv(), timeout=30.0)
                    await self._process_message(message)
                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    if self.connection and not self.connection.closed:
                        await self.connection.ping()
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("WebSocket connection closed")
                    self.connected = False
                    break
                except Exception as e:
                    logger.error(f"Error receiving message: {e}")
                    break
        finally:
            self.connected = False
            # Start reconnection
            if not self._stop_event.is_set():
                if not self._reconnect_task or self._reconnect_task.done():
                    self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _process_message(self, message: str):
        """Process received messages from Qubit"""
        try:
            data = json.loads(message)
            
            # Handle Qubit message format
            if isinstance(data, dict):
                # Store based on topic type
                topic = data.get('topic', 'unknown')
                action = data.get('action', 'unknown')
                
                if action == 'notify':
                    # This is market data
                    if 'data' in data:
                        market_info = data['data']
                        
                        # Store by topic for easy access
                        if topic not in self._market_data:
                            self._market_data[topic] = {}
                            
                        self._market_data[topic].update(market_info)
                        self._market_data['last_update'] = datetime.now().isoformat()
                        
                        logger.debug(f"Updated {topic}: {market_info}")
                
                # Always store the raw message too
                self._market_data['latest_message'] = data
                    
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Qubit message: {e}")
        except Exception as e:
            logger.error(f"Error processing Qubit message: {e}")

    def get_market_data(self) -> Dict[str, Any]:
        """Get current market data"""
        return self._market_data.copy()

    async def close(self):
        """Clean shutdown"""
        logger.info("Closing WebSocket connection...")
        self._stop_event.set()
        
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            
        if self.connection and not self.connection.closed:
            await self.connection.close()
        
        self.connected = False

# ===== FastAPI App =====
ws_manager = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown"""
    global ws_manager
    
    # Initialize WebSocket
    ws_manager = WebSocketManager()
    await ws_manager.connect()
    app.state.ws_manager = ws_manager
    
    logger.info("Application startup complete")
    
    yield  # App runs here
    
    # Cleanup
    logger.info("Shutting down application...")
    if ws_manager:
        await ws_manager.close()

app = FastAPI(
    title="Crypto Assistant API",
    description="Real-time cryptocurrency data assistant",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== API Endpoints =====
@app.get("/ask")
async def ask_question(query: str):
    """AI response endpoint with market data"""
    
    async def generate():
        try:
            # Get current market data from Qubit
            market_data = ws_manager.get_market_data() if ws_manager else {}
            
            # Extract useful info for AI response
            btc_info = market_data.get('market.ticker', {})
            contract_info = market_data.get('contracts.market', {})
            
            response_data = {
                "query": query,
                "btc_data": btc_info,
                "contract_data": contract_info,
                "response": f"Query: '{query}' | BTC Contract: {btc_info.get('contract_code', 'N/A')} | Last Update: {market_data.get('last_update', 'No data')}",
                "timestamp": datetime.now().isoformat()
            }
            
            yield json.dumps(response_data) + "\n"
            
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            error_data = {"error": str(e), "timestamp": datetime.now().isoformat()}
            yield json.dumps(error_data) + "\n"

    return StreamingResponse(generate(), media_type="application/json")

@app.get("/market-data")
async def get_market_data():
    """Get current market data"""
    if not ws_manager:
        raise HTTPException(status_code=503, detail="WebSocket manager not initialized")
    
    data = ws_manager.get_market_data()
    return {
        "connected": ws_manager.connected,
        "data": data,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "websocket_connected": ws_manager.connected if ws_manager else False,
        "market_data_available": bool(ws_manager.get_market_data()) if ws_manager else False,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Crypto Assistant API",
        "endpoints": {
            "/health": "Health check",
            "/market-data": "Current market data",
            "/ask?query=<question>": "Ask AI questions"
        }
    }

# ===== Main Execution =====
if __name__ == "__main__":
    import uvicorn

    try:
        port = find_available_port(config["default_port"], config["port_range"])
        logger.info(f"Starting server on port {port}")
        
        # Kill any existing process on the port
        try:
            import subprocess
            import time
            
            # More aggressive port cleanup
            result = subprocess.run(f"lsof -ti:{port}", shell=True, capture_output=True, text=True)
            if result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                for pid in pids:
                    if pid:
                        subprocess.run(f"kill -9 {pid}", shell=True, check=False)
                        print(f"Killed process {pid} on port {port}")
                time.sleep(1)  # Wait a moment
        except Exception as e:
            print(f"Port cleanup error: {e}")

        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            timeout_keep_alive=60,
            reload=False  # Disable reload to prevent issues
        )
        
    except OSError as e:
        logger.error(f"Failed to start server: {e}")
        print(f"Try running: lsof -ti:8000 | xargs kill -9")
    except KeyboardInterrupt:
        logger.info("Server shutdown complete")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")