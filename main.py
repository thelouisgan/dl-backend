#Crispy2bogget
#!/usr/bin/env python3

import time
from typing import List, Dict, Any, Optional
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
import hashlib
import hmac
import asyncio
import uuid

import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# ===== Pydantic Models =====
class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None

class FrontendChatRequest(BaseModel):
    user_input: str

class ChatResponse(BaseModel):
    response: str
    market_data: Optional[Dict] = None
    timestamp: str

class FrontendChatResponse(BaseModel):
    id: str
    message: str
    timestamp: str
    market_data: Optional[Dict] = None

class StreamResponse(BaseModel):
    id: str
    message: str
    timestamp: str
    market_data: Optional[Dict] = None

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

load_dotenv()

config = {
    "ws_url": os.getenv("WS_URL", "wss://api.tgx.finance/v1/ws/"),
    "app_id": int(os.getenv("APP_ID", "1")),
    "token": os.getenv("TOKEN", ""),
    "secret": os.getenv("SECRET", ""),
    "ping_interval": int(os.getenv("WS_PING_INTERVAL", "30")),
    "ping_timeout": int(os.getenv("WS_PING_TIMEOUT", "10")),
    "close_timeout": int(os.getenv("WS_CLOSE_TIMEOUT", "10")),
    "heartbeat_interval": int(os.getenv("HEARTBEAT_INTERVAL", "50")),
    "default_port": int(os.getenv("PORT", "8000")),
    "port_range": 100,
    "ssl_enabled": os.getenv("SSL_ENABLED", "true").lower() == "true",
    "max_reconnect_attempts": 5,
    "reconnect_delay": 2.0,
    "use_fallback_api": os.getenv("USE_FALLBACK_API", "true").lower() == "true",
    "debug_mode": os.getenv("DEBUG_MODE", "false").lower() == "true"
}

# ===== Simple in-memory chat storage =====
chat_sessions = {}

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

def create_signature(data: Dict[str, Any], secret: str) -> str:
    """Create signature for TGX authentication (if needed)"""
    if not secret:
        return ""
    
    # Sort parameters and create query string
    sorted_params = sorted(data.items())
    query_string = "&".join([f"{k}={v}" for k, v in sorted_params if k != "sign"])
    
    # Create HMAC SHA256 signature
    signature = hmac.new(
        secret.encode('utf-8'), 
        query_string.encode('utf-8'), 
        hashlib.sha256
    ).hexdigest()
    
    return signature

class TGXWebSocketManager:
    def __init__(self):
        self.connection = None
        self.connected = False
        self._stop_event = asyncio.Event()
        self._market_data = {
            "btc_ticker": {},
            "eth_ticker": {},
            "contract_info": {},
            "price_history": [],
            "connection_status": "disconnected",
            "last_update": None,
            "message_count": 0,
            "latest_raw_message": {},
            "subscriptions": [],
            "connection_attempts": 0,
            "last_connection_attempt": None,
            "connection_errors": [],
            "fallback_data": {}
        }
        self._reconnect_task = None
        self._heartbeat_task = None
        self._authenticated = False
        self._fallback_task = None

    async def connect(self):
        """Connect to TGX Finance WebSocket with enhanced error tracking"""
        if self.connected and self.connection is not None:
            try:
                if hasattr(self.connection, 'closed') and not self.connection.closed:
                    logger.info("Already connected to TGX Finance!")
                    return
            except AttributeError:
                logger.warning("Connection object doesn't have 'closed' attribute, reconnecting...")
                self.connected = False
                self.connection = None

        max_retries = config["max_reconnect_attempts"]
        retry_count = 0
        
        self._market_data["connection_attempts"] += 1
        self._market_data["last_connection_attempt"] = datetime.now().isoformat()

        # Start fallback data fetching if enabled
        if config["use_fallback_api"] and not self._fallback_task:
            self._fallback_task = asyncio.create_task(self._fallback_data_loop())

        while retry_count < max_retries and not self._stop_event.is_set():
            try:
                logger.info(f"🔄 Connecting to TGX Finance WebSocket: {config['ws_url']} (attempt {retry_count + 1}/{max_retries})")
                
                ssl_context = None
                if config["ssl_enabled"]:
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE

                self.connection = await websockets.connect(
                    config["ws_url"],
                    ping_interval=config["ping_interval"],
                    ping_timeout=config["ping_timeout"],
                    ssl=ssl_context if config["ssl_enabled"] else None,
                    close_timeout=config["close_timeout"]
                )
                
                self.connected = True
                self._market_data["connection_status"] = "connected"
                self._market_data["connection_errors"].clear()  # Clear previous errors
                logger.info("✅ Connected to TGX Finance successfully!")

                # Authenticate if credentials are provided
                if config.get("token") and config.get("secret"):
                    await self._authenticate()
                
                # Subscribe to market feeds
                await self._subscribe_to_feeds()
                
                # Start heartbeat task
                if self._heartbeat_task is None or self._heartbeat_task.done():
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                
                # Start listening for messages
                await self._listen_messages()
                break
                
            except Exception as e:
                error_msg = f"Connection attempt {retry_count + 1} failed: {str(e)}"
                logger.error(error_msg)
                
                # Track connection errors
                self._market_data["connection_errors"].append({
                    "attempt": retry_count + 1,
                    "error": str(e),
                    "timestamp": datetime.now().isoformat()
                })
                
                self.connected = False
                self.connection = None
                self._market_data["connection_status"] = f"failed_attempt_{retry_count + 1}"
                retry_count += 1
                
                if retry_count < max_retries:
                    logger.info(f"⏳ Retrying in {config['reconnect_delay']} seconds...")
                    await asyncio.sleep(config["reconnect_delay"])
        
        if retry_count >= max_retries:
            self._market_data["connection_status"] = "failed_max_retries"
            logger.error(f"❌ Failed to connect after {max_retries} attempts. Using fallback data only.")

    async def _fallback_data_loop(self):
        """Continuously fetch fallback data from public APIs"""
        while not self._stop_event.is_set():
            try:
                await self._fetch_fallback_data()
                await asyncio.sleep(60)  # Fetch every minute
            except Exception as e:
                logger.error(f"Fallback data loop error: {e}")
                await asyncio.sleep(30)

    async def _fetch_fallback_data(self):
        """Fetch data from multiple public APIs as fallback"""
        fallback_sources = [
            {
                "name": "CoinGecko",
                "url": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true",
                "parser": self._parse_coingecko_data
            },
            {
                "name": "CoinDesk",
                "url": "https://api.coindesk.com/v1/bpi/currentprice.json",
                "parser": self._parse_coindesk_data
            }
        ]
        
        for source in fallback_sources:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(source["url"], timeout=10) as response:
                        if response.status == 200:
                            data = await response.json()
                            parsed_data = source["parser"](data)
                            if parsed_data:
                                self._market_data["fallback_data"] = {
                                    **parsed_data,
                                    "source": source["name"],
                                    "timestamp": datetime.now().isoformat(),
                                    "last_update": datetime.now().isoformat()
                                }
                                logger.info(f"📡 Fallback data updated from {source['name']}")
                                return True
            except Exception as e:
                logger.warning(f"Failed to fetch from {source['name']}: {e}")
                continue
        
        return False

    def _parse_coingecko_data(self, data):
        """Parse CoinGecko API response"""
        try:
            return {
                "btc": {
                    "price": str(data["bitcoin"]["usd"]),
                    "change_24h": str(data["bitcoin"].get("usd_24h_change", 0)),
                    "symbol": "BTCUSDT"
                },
                "eth": {
                    "price": str(data["ethereum"]["usd"]),
                    "change_24h": str(data["ethereum"].get("usd_24h_change", 0)),
                    "symbol": "ETHUSDT"
                }
            }
        except Exception as e:
            logger.error(f"Error parsing CoinGecko data: {e}")
            return None

    def _parse_coindesk_data(self, data):
        """Parse CoinDesk API response"""
        try:
            btc_price = data["bpi"]["USD"]["rate"].replace(",", "").replace("$", "")
            return {
                "btc": {
                    "price": btc_price,
                    "change_24h": "0.00",  # CoinDesk doesn't provide change
                    "symbol": "BTCUSDT"
                }
            }
        except Exception as e:
            logger.error(f"Error parsing CoinDesk data: {e}")
            return None

    async def _authenticate(self):
        """Authenticate with TGX Finance (optional)"""
        try:
            ts = int(time.time())
            auth_data = {
                "app_id": config["app_id"],
                "token": config["token"],
                "ts": ts
            }
            
            # Create signature if secret is provided
            if config.get("secret"):
                auth_data["sign"] = create_signature(auth_data, config["secret"])
            
            auth_message = {
                "action": "auth",
                "topic": "auth",
                "data": auth_data
            }
            
            await self.connection.send(json.dumps(auth_message))
            logger.info("🔐 Sent authentication request")
            
        except Exception as e:
            logger.warning(f"Authentication failed: {e}")

    async def _heartbeat_loop(self):
        """Send periodic heartbeats using TGX protocol"""
        try:
            while self.connected and not self._stop_event.is_set():
                await asyncio.sleep(config["heartbeat_interval"])
                if self.connection and self.connected:
                    try:
                        ts = int(time.time())
                        heartbeat_data = {
                            "app_id": config["app_id"],
                            "ts": ts
                        }
                        
                        # Add token and signature if available
                        if config.get("token"):
                            heartbeat_data["token"] = config["token"]
                            if config.get("secret"):
                                heartbeat_data["sign"] = create_signature(heartbeat_data, config["secret"])
                        
                        heartbeat_msg = {
                            "action": "heart",
                            "topic": "heart",
                            "data": heartbeat_data
                        }
                        
                        await self.connection.send(json.dumps(heartbeat_msg))
                        logger.debug("💓 Sent TGX heartbeat")
                        
                    except Exception as e:
                        logger.warning(f"Heartbeat failed: {e}")
                        break
  
        except Exception as e:
            logger.error(f"Heartbeat loop error: {e}")
        finally:
            await self._cleanup_connection()

    async def _subscribe_to_feeds(self):
        """Subscribe to TGX Finance data feeds using correct protocol - FIXED TO INCLUDE ETH"""
        if not self.connection or not self.connected:
            logger.warning("Cannot subscribe - not connected")
            return
    
        try:
            await self.connection.send(json.dumps({
                "action": "sub",
                "topic": "market.contract.switch",
                "data": {"contract_code": "BTCUSDT"}
            }))
        
        # Subscribe to ETH (NEW)
            await self.connection.send(json.dumps({
                "action": "sub",
                "topic": "market.contract.switch",
                "data": {"contract_code": "ETHUSDT"} 
            }))
        
        # Subscribe to all contracts
            await self.connection.send(json.dumps({
                "action": "sub",
                "topic": "contracts.market",
                "data": {}
            }))
            
        except Exception as e:
            logger.error(f"Subscription error: {e}")    
            

    async def _listen_messages(self):
        """Listen for incoming messages with TGX protocol handling"""
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        try:
            while not self._stop_event.is_set() and self.connected:
                try:
                    message = await asyncio.wait_for(
                        self.connection.recv(), 
                        timeout=120.0
                    )
                    
                    consecutive_errors = 0
                    await self._process_tgx_message(message)
                    
                except asyncio.TimeoutError:
                    logger.debug("Message receive timeout - checking connection")
                    if self.connection:
                        try:
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
        """Process TGX Finance messages according to protocol specification"""
        try:
            if config["debug_mode"]:
                logger.info(f"📨 Raw TGX message: {message[:300]}...")
            
            data = json.loads(message)
            self._market_data["message_count"] += 1
            self._market_data["last_update"] = datetime.now().isoformat()
            self._market_data["latest_raw_message"] = data

            # Handle different TGX message types based on action and topic
            action = data.get("action")
            topic = data.get("topic")
            msg_data = data.get("data", {})
            
            if action == "auth" and topic == "auth":
                # Authentication response
                code = data.get("code", -1)
                if code == 0:
                    self._authenticated = True
                    logger.info("🔐 Authentication successful")
                else:
                    logger.warning(f"❌ Authentication failed with code: {code}")
                    
            elif action == "sub":
                # Subscription confirmation
                code = data.get("code", -1)
                if code == 0:
                    logger.info(f"✅ Subscription confirmed for topic: {topic}")
                else:
                    err_msg = data.get("err_msg", "Unknown error")
                    logger.warning(f"❌ Subscription failed for {topic}: {err_msg}")
                    
            elif action == "notify":
                # Data push notifications
                await self._handle_notification(topic, msg_data, data.get("ts"))
                
            else:
                logger.info(f"🔍 Unhandled message type: action={action}, topic={topic}")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}\nMessage: {message[:200]}...")
        except Exception as e:
            logger.error(f"Message processing error: {e}")

    async def _handle_notification(self, topic: str, data: Dict[str, Any], timestamp: Optional[int]):
        """Handle different types of notification messages"""
        try:
            if topic == "market.ticker":
                # Latest trades notification
                contract_code = data.get("contract_code", "")
                trade_list = data.get("list", [])
                
                if trade_list:
                    latest_trade = trade_list[0]
                    price = latest_trade.get("trade_price", latest_trade.get("price", ""))

                    ticker_key = "btc_ticker" if "BTC" in contract_code else "eth_ticker" if "ETH" in contract_code else None

                    if ticker_key:
                        # Update ticker data
                        self._market_data[ticker_key].update({
                            "contract_code": contract_code,
                            "price": price,
                            "last_trade": latest_trade,
                            "timestamp": timestamp or int(time.time())
                        })
                    
                        # Add to price history
                        if price:
                            self._market_data["price_history"].append({
                                "symbol": "BTC" if ticker_key == "btc_ticker" else "ETH",
                                "price": price,
                                "timestamp": datetime.now().isoformat()
                            })
                            if len(self._market_data["price_history"]) > 100:
                                self._market_data["price_history"] = self._market_data["price_history"][-100:]

                        logger.info(f"📈 TRADE UPDATE: {contract_code} @ {price}")

            elif topic == "market.price.index":
                # Contract index price push
                contract_code = data.get("contract_code", "").upper()
                price = data.get("price", "")
                spot_price = data.get("spot_index_price", "")

                ticker_key = "btc_ticker" if "BTC" in contract_code else "eth_ticker" if "ETH" in contract_code else None
                    
                if ticker_key:
                    self._market_data[ticker_key].update({
                        "contract_code": contract_code,
                        "index_price": price,
                        "spot_index_price": spot_price,
                        "timestamp": data.get("ts", timestamp)
                    })
                
                    logger.info(f"📊 INDEX UPDATE: {contract_code} Index: {price}, Spot: {spot_price}")

            elif topic == "contracts.market":
                # All contracts market data
                if isinstance(data, list):
                    for contract in data:
                        contract_code = contract.get("contract_code", "").upper()
                        ticker_key = None
                    
                        if "BTC" in contract_code:
                            ticker_key = "btc_ticker"
                        elif "ETH" in contract_code:
                            ticker_key = "eth_ticker"

                        if ticker_key:
                            self._market_data[ticker_key].update({
                                "contract_code": contract_code,
                                "price": contract.get("price", ""),
                                "index_price": contract.get("index_price", ""),
                                "change_ratio": contract.get("change_ratio", ""),
                                "change": contract.get("change", ""),
                                "buy_count": contract.get("buy_count", 0),
                                "sell_count": contract.get("sell_count", 0)
                            })

                            logger.info(f"💰 MARKET UPDATE: {contract_code} @ {contract.get('price', 'N/A')} ({contract.get('change_ratio', 'N/A')}%)")

            elif topic == "contract.applies":
                # Contract change ratio update
                contract_code = data.get("contract_code", "").upper()
                change_ratio = data.get("change_ratio", "")
                change = data.get("change", "")
                high_price = data.get("high_price", "")
                low_price = data.get("low_price", "")
                trade_24h = data.get("trade_24h", "")

                ticker_key = None
                if "BTC" in contract_code:
                    ticker_key = "btc_ticker"
                elif "ETH" in contract_code:
                    ticker_key = "eth_ticker"

                if ticker_key:
                    self._market_data[ticker_key].update({
                        "contract_code": contract_code,
                        "change_ratio": change_ratio,
                        "change": change,
                        "high_24h": high_price,
                        "low_24h": low_price,
                        "volume_24h": trade_24h
                    })
                
                    logger.info(f"📊 STATS UPDATE: {contract_code} Change: {change_ratio}%, High: {high_price}, Low: {low_price}")
            
            else:
                if config["debug_mode"]:
                    logger.info(f"🔍 Unhandled notification topic: {topic}")
            
        except Exception as e:
            logger.error(f"Notification handling error for topic {topic}: {e}")

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
        """Get a formatted market summary with fallback data"""
        btc_ticker = self._market_data.get("btc_ticker", {})
        eth_ticker = self._market_data.get("eth_ticker", {})
        fallback_data = self._market_data.get("fallback_data", {})

        # Use TGX data if available, otherwise use fallback
        btc_data = btc_ticker if btc_ticker.get("price") else fallback_data.get("btc", {})
        eth_data = eth_ticker if eth_ticker.get("price") else fallback_data.get("eth", {})

        summary = {
            "btc": {
                "symbol": btc_data.get("contract_code", btc_data.get("symbol", "BTCUSDT")),
                "price": btc_data.get("price", "N/A"),
                "index_price": btc_data.get("index_price", "N/A"),
                "change_24h": btc_data.get("change_ratio", "N/A"),
                "change_abs": btc_data.get("change", "N/A"),
                "volume": btc_data.get("volume_24h", "N/A"),
                "high_24h": btc_data.get("high_24h", "N/A"),
                "low_24h": btc_data.get("low_24h", "N/A"),
                "buy_count": btc_data.get("buy_count", 0),
                "sell_count": btc_data.get("sell_count", 0),
                "data_source": "TGX Live" if btc_ticker.get("price") else f"Fallback ({fallback_data.get('source', 'N/A')})"
            },
        
            "eth": {
                "symbol": eth_data.get("contract_code", eth_data.get("symbol", "ETHUSDT")),
                "price": eth_data.get("price", "N/A"),
                "index_price": eth_data.get("index_price", "N/A"),
                "change_24h": eth_data.get("change_ratio", "N/A"),
                "change_abs": eth_data.get("change", "N/A"),
                "volume": eth_data.get("volume_24h", "N/A"),
                "high_24h": eth_data.get("high_24h", "N/A"),
                "low_24h": eth_data.get("low_24h", "N/A"),
                "buy_count": eth_data.get("buy_count", 0),
                "sell_count": eth_data.get("sell_count", 0),
                "data_source": "TGX Live" if eth_ticker.get("price") else f"Fallback ({fallback_data.get('source', 'N/A')})"
            },
            "connection_status": self._market_data["connection_status"],
            "last_update": self._market_data["last_update"],
            "message_count": self._market_data["message_count"],
            "authenticated": self._authenticated,
            "subscriptions": len(self._market_data["subscriptions"]),
            "has_fallback_data": bool(fallback_data),
            "fallback_source": fallback_data.get("source", "N/A"),
            "connection_attempts": self._market_data["connection_attempts"],
            "connection_errors": len(self._market_data["connection_errors"])
        }
    
        return summary

    def get_full_market_data(self) -> Dict[str, Any]:
        """Get all market data including debug info"""
        return self._market_data.copy()

    async def close(self):
        """Clean shutdown"""
        logger.info("🔌 Closing TGX Finance WebSocket connection...")
        self._stop_event.set()
        
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        if self._fallback_task and not self._fallback_task.done():
            self._fallback_task.cancel()
            
        await self._cleanup_connection()
        logger.info("✅ TGX Finance connection closed")

# ===== AI Response Generator =====
class CryptoAI:
    def __init__(self, ws_manager: TGXWebSocketManager):
        self.ws_manager = ws_manager
        
    def generate_response(self, query: str) -> str:
        """Generate AI-like responses based on query and market data with fallback support"""
        query_lower = query.lower()
        market_data = self.ws_manager.get_market_summary()
        
        connection_status = market_data.get("connection_status", "unknown")
        message_count = market_data.get("message_count", 0)
  

        btc_data = market_data.get("btc", {})
        eth_data = market_data.get("eth", {})


        logger.info(f"BTC Data: {btc_data}")
        logger.info(f"ETH Data: {eth_data}")
        
        def format_coin_response(coin: str, data: dict) -> str:
            symbol = data.get("symbol", "N/A")
            price = data.get("price", "N/A")
            index_price = data.get("index_price", "N/A")
            change = data.get("change_24h", "N/A")
            data_source = data.get("data_source", "Unknown")

            if price == "N/A":
                return None
        
            response = f"💎 {coin} ({symbol}) is currently trading at {format_price(str(price))}"
            if index_price != "N/A":
                response += f" (Index: {format_price(str(index_price))})"
            if change != "N/A":
                response += f". The 24h change is {change}%"
            response += f". Data source: {data_source}"
            return response
                
        # ETH price queries - FIXED TO HANDLE ETH PROPERLY
        if any(word in query_lower for word in ["eth", "ethereum"]):
            eth_response = format_coin_response("Ethereum", eth_data)
            if eth_response:
                return eth_response
            return f"💎 Ethereum price data is currently unavailable. Connection: {connection_status}"
            
        elif any(word in query_lower for word in ["btc", "bitcoin"]):
            btc_response = format_coin_response("Bitcoin", btc_data)
            if btc_response:
                return btc_response
            return f"💰 Bitcoin price data is currently unavailable. Connection: {connection_status}"
        

        elif any(word in query_lower for word in ["price", "cost", "value", "how much"]):
            responses = []
            btc_response = format_coin_response("Bitcoin", btc_data)
            eth_response = format_coin_response("Ethereum", eth_data)
        
            if btc_response:
                responses.append(btc_response)
            if eth_response:
                responses.append(eth_response)
            
            if responses:
                return "\n\n".join(responses)
            return "❌ Crypto price data is currently unavailable. Please try again later."

        elif any(word in query_lower for word in ["volume", "trading", "activity", "buy", "sell"]):
            btc_volume = btc_data.get("volume", "N/A")
            eth_volume = eth_data.get("volume", "N/A")
            data_source = btc_data.get("data_source", "Unknown")
        
            response = f"📊 Trading Volume ({data_source}):"
            if btc_volume != "N/A":
                response += f"\n• BTC 24h Volume: {btc_volume}"
            if eth_volume != "N/A":
                response += f"\n• ETH 24h Volume: {eth_volume}"
            
            if btc_volume == "N/A" and eth_volume == "N/A":
                response = "📊 Trading volume data is currently unavailable."
        
            return response
        
        elif any(word in query_lower for word in ["high", "low", "range", "24h"]):
            response = "📈 24h Price Ranges:"
        
        # BTC range
            btc_high = btc_data.get("high_24h", "N/A")
            btc_low = btc_data.get("low_24h", "N/A")
            if btc_high != "N/A" or btc_low != "N/A":
                response += f"\n🟠 BTC:"
                if btc_high != "N/A":
                    response += f" High: {format_price(str(btc_high))}"
                if btc_low != "N/A":
                    response += f" Low: {format_price(str(btc_low))}"

            eth_high = eth_data.get("high_24h", "N/A")
            eth_low = eth_data.get("low_24h", "N/A")
            if eth_high != "N/A" or eth_low != "N/A":
                response += f"\n🔵 ETH:"
                if eth_high != "N/A":
                    response += f" High: {format_price(str(eth_high))}"
                if eth_low != "N/A":
                    response += f" Low: {format_price(str(eth_low))}"

            if (btc_high == "N/A" and btc_low == "N/A" and 
                eth_high == "N/A" and eth_low == "N/A"):
                response = "📈 Price range data is currently unavailable."
            
            return response
        
        elif any(word in query_lower for word in ["debug", "status", "connection", "working", "online"]):
            full_data = self.ws_manager.get_full_market_data()
            subscriptions = full_data.get("subscriptions", [])
            connection_errors = full_data.get("connection_errors", [])

            debug_info = f"""🔍 System Status:
    • Connection: {connection_status}
    • Messages received: {message_count}
    • BTC Price: {btc_data.get('price', 'N/A')}
    • ETH Price: {eth_data.get('price', 'N/A')}
    • BTC Change: {btc_data.get('change_24h', 'N/A')}%
    • ETH Change: {eth_data.get('change_24h', 'N/A')}%
    • Last update: {market_data.get('last_update', 'Never')}""" 
            
            if connection_errors:
                debug_info += f"\n\nRecent Errors:"
                for error in connection_errors[-3:]:
                    debug_info += f"\n• {error.get('error', 'Unknown error')}"
                
            return debug_info


        else:
            responses = []
            btc_response = format_coin_response("Bitcoin", btc_data)
            eth_response = format_coin_response("Ethereum", eth_data)
        
            if btc_response:
                responses.append(btc_response)
            if eth_response:
                responses.append(eth_response)
            
            if responses:
                return "🤖 Crypto Market Update:\n\n" + "\n\n".join(responses)
        
            status_emoji = "🔄" if connection_status == "connecting" else "⚠️"
            return f"{status_emoji} System status: {connection_status} | Messages: {message_count}"


    async def generate_streaming_response(self, query: str):
        """Generate streaming response"""
        try:
            response_text = self.generate_response(query)
            market_summary = self.ws_manager.get_market_summary()
            
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
    description="Real-time cryptocurrency assistant powered by TGX Finance WebSocket feeds with fallback support",
    version="2.3.0",
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
    """Main ask endpoint with streaming response - FIXED TO RETURN PLAIN TEXT"""
    if not crypto_ai:
        return PlainTextResponse("AI service not initialized", status_code=503)
    
    logger.info(f"💬 Received query: '{user_input}'")
    
    # Generate response as plain text instead of streaming JSON
    response_text = crypto_ai.generate_response(user_input)
    
    return PlainTextResponse(response_text)

# ===== FRONTEND COMPATIBLE ENDPOINTS =====

@app.post("/api/chat")
async def frontend_chat_endpoint(request: FrontendChatRequest):
    """Frontend compatible chat endpoint - creates a new chat"""
    try:
        if not crypto_ai:
            raise HTTPException(status_code=503, detail="AI service not initialized")
        
        logger.info(f"📱 Frontend chat request: '{request.user_input}'")
        
        # Generate a new chat ID
        chat_id = str(uuid.uuid4())
        
        # Generate AI response
        ai_response = crypto_ai.generate_response(request.user_input)
        market_summary = ws_manager.get_market_summary() if ws_manager else {}
        
        # Store chat session
        chat_sessions[chat_id] = {
            "id": chat_id,
            "messages": [
                {"role": "user", "content": request.user_input, "timestamp": datetime.now().isoformat()},
                {"role": "assistant", "content": ai_response, "timestamp": datetime.now().isoformat()}
            ],
            "created_at": datetime.now().isoformat(),
            "market_data": market_summary
        }
        
        response = FrontendChatResponse(
            id=chat_id,
            message=ai_response,
            timestamp=datetime.now().isoformat(),
            market_data=market_summary
        )
        
        logger.info(f"✅ Created chat {chat_id}: Response length {len(ai_response)} chars | Connection: {market_summary.get('connection_status', 'unknown')}")
        return response
        
    except Exception as e:
        logger.error(f"❌ Frontend chat endpoint error: {e}")
        raise HTTPException(status_code=500, detail=f"Chat service error: {str(e)}")

@app.post("/api/chat/{chat_id}/stream")
async def frontend_stream_endpoint(chat_id: str, request: FrontendChatRequest):
    """Frontend compatible streaming endpoint"""
    try:
        if not crypto_ai:
            raise HTTPException(status_code=503, detail="AI service not initialized")
        
        logger.info(f"🌊 Frontend stream request for chat {chat_id}: '{request.user_input}'")
        
        # Check if chat exists, create if not
        if chat_id not in chat_sessions:
            chat_sessions[chat_id] = {
                "id": chat_id,
                "messages": [],
                "created_at": datetime.now().isoformat(),
                "market_data": {}
            }
        
        # Generate AI response
        ai_response = crypto_ai.generate_response(request.user_input)
        market_summary = ws_manager.get_market_summary() if ws_manager else {}
        
        # Update chat session
        chat_sessions[chat_id]["messages"].extend([
            {"role": "user", "content": request.user_input, "timestamp": datetime.now().isoformat()},
            {"role": "assistant", "content": ai_response, "timestamp": datetime.now().isoformat()}
        ])
        chat_sessions[chat_id]["market_data"] = market_summary
        
        response = StreamResponse(
            id=chat_id,
            message=ai_response,
            timestamp=datetime.now().isoformat(),
            market_data=market_summary
        )
        
        logger.info(f"✅ Stream chat {chat_id}: Response length {len(ai_response)} chars | Connection: {market_summary.get('connection_status', 'unknown')}")
        return response
        
    except Exception as e:
        logger.error(f"❌ Frontend stream endpoint error: {e}")
        raise HTTPException(status_code=500, detail=f"Stream service error: {str(e)}")

@app.get("/api/chat/{chat_id}")
async def get_chat_session(chat_id: str):
    """Get a specific chat session"""
    if chat_id not in chat_sessions:
        raise HTTPException(status_code=404, detail="Chat session not found")
    
    return chat_sessions[chat_id]

@app.get("/api/chats")
async def list_chat_sessions():
    """List all chat sessions"""
    return {
        "chats": [
            {
                "id": chat_id,
                "created_at": session["created_at"],
                "message_count": len(session["messages"]),
                "last_message": session["messages"][-1]["content"] if session["messages"] else None
            }
            for chat_id, session in chat_sessions.items()
        ],
        "total": len(chat_sessions)
    }

@app.delete("/api/chat/{chat_id}")
async def delete_chat_session(chat_id: str):
    """Delete a chat session"""
    if chat_id not in chat_sessions:
        raise HTTPException(status_code=404, detail="Chat session not found")
    
    del chat_sessions[chat_id]
    return {"message": f"Chat session {chat_id} deleted"}

# ===== ORIGINAL ENDPOINTS (BACKWARD COMPATIBILITY) =====

@app.post("/chat")
async def legacy_chat_endpoint(request: ChatRequest):
    """Legacy chat endpoint for backward compatibility"""
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
        
        logger.info(f"💬 Legacy chat query: '{request.message}' | Response: {len(ai_response)} chars")
        return response
        
    except Exception as e:
        logger.error(f"Legacy chat endpoint error: {e}")
        raise HTTPException(status_code=500, detail=f"Chat service error: {str(e)}")

@app.get("/market-data")
async def get_market_data():
    """Get comprehensive market data including debug information"""
    try:
        if not ws_manager:
            raise HTTPException(status_code=503, detail="WebSocket manager not initialized")
        
        full_data = ws_manager.get_full_market_data()
        summary = ws_manager.get_market_summary()
        
        return {
            "connected": ws_manager.connected,
            "authenticated": ws_manager._authenticated,
            "summary": summary,
            "full_data": full_data,
            "subscriptions": full_data.get("subscriptions", []),
            "connection_attempts": full_data.get("connection_attempts", 0),
            "connection_errors": full_data.get("connection_errors", []),
            "has_fallback_data": bool(full_data.get("fallback_data")),
            "fallback_source": full_data.get("fallback_data", {}).get("source", "N/A"),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Market data endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Comprehensive health check with debug info"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {
            "websocket": {
                "connected": ws_manager.connected if ws_manager else False,
                "authenticated": ws_manager._authenticated if ws_manager else False,
                "status": ws_manager._market_data.get("connection_status", "unknown") if ws_manager else "not_initialized",
                "connection_attempts": ws_manager._market_data.get("connection_attempts", 0) if ws_manager else 0,
                "connection_errors": len(ws_manager._market_data.get("connection_errors", [])) if ws_manager else 0
            },
            "ai": {
                "available": crypto_ai is not None,
                "status": "ready" if crypto_ai else "not_initialized"
            },
            "market_data": {
                "available": bool(ws_manager.get_full_market_data() if ws_manager else False),
                "message_count": ws_manager._market_data.get("message_count", 0) if ws_manager else 0,
                "subscriptions": len(ws_manager._market_data.get("subscriptions", [])) if ws_manager else 0,
                "has_fallback": bool(ws_manager._market_data.get("fallback_data")) if ws_manager else False,
                "fallback_source": ws_manager._market_data.get("fallback_data", {}).get("source", "N/A") if ws_manager else "N/A"
            },
            "chat_sessions": {
                "active": len(chat_sessions),
                "total_messages": sum(len(session.get("messages", [])) for session in chat_sessions.values())
            }
        },
        "debug_mode": config.get("debug_mode", False),
        "fallback_enabled": config.get("use_fallback_api", True)
    }
    
    if not ws_manager or not ws_manager.connected or not crypto_ai:
        health_status["status"] = "degraded"
        
    # Add detailed connection info if available
    if ws_manager and ws_manager._market_data.get("connection_errors"):
        health_status["recent_errors"] = ws_manager._market_data["connection_errors"][-3:]  # Last 3 errors
    
    return health_status

@app.post("/subscribe")
async def subscribe_to_feed(contract_code: str = "BTCUSDT"):
    """Manually subscribe to a specific contract"""
    try:
        if not ws_manager or not ws_manager.connected:
            raise HTTPException(status_code=503, detail="WebSocket not connected")
        
        subscription = {
            "action": "sub",
            "topic": "market.contract.switch",
            "data": {
                "contract_code": contract_code.upper()
            }
        }
        
        await ws_manager.connection.send(json.dumps(subscription))
        logger.info(f"📡 Manual subscription sent for {contract_code}")
        
        return {
            "status": "success",
            "message": f"Subscription sent for {contract_code}",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Manual subscription error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug")
async def debug_endpoint():
    """Enhanced debug endpoint with comprehensive system information"""
    try:
        if not ws_manager:
            return {"error": "WebSocket manager not initialized"}
            
        full_data = ws_manager.get_full_market_data()
        summary = ws_manager.get_market_summary()
        
        debug_info = {
            "system": {
                "config": {k: v for k, v in config.items() if k not in ["token", "secret"]},  # Hide sensitive data
                "chat_sessions_count": len(chat_sessions),
                "python_version": f"{os.sys.version_info.major}.{os.sys.version_info.minor}.{os.sys.version_info.micro}"
            },
            "websocket": {
                "connected": ws_manager.connected,
                "authenticated": ws_manager._authenticated,
                "connection_status": full_data.get("connection_status"),
                "connection_attempts": full_data.get("connection_attempts", 0),
                "message_count": full_data.get("message_count", 0),
                "subscriptions": full_data.get("subscriptions", []),
                "last_update": full_data.get("last_update"),
                "connection_errors": full_data.get("connection_errors", [])
            },
            "market_data": {
                "btc_ticker": full_data.get("btc_ticker", {}),
                "eth_ticker": full_data.get("eth_ticker", {}),
                "price_history_count": len(full_data.get("price_history", [])),
                "fallback_data": full_data.get("fallback_data", {}),
                "latest_raw_message": full_data.get("latest_raw_message", {})
            },
            "summary": summary,
            "timestamp": datetime.now().isoformat()
        }
        
        return debug_info
        
    except Exception as e:
        logger.error(f"Debug endpoint error: {e}")
        return {"error": str(e), "timestamp": datetime.now().isoformat()}

@app.get("/")
async def root():
    """API documentation root with enhanced information"""
    market_summary = ws_manager.get_market_summary() if ws_manager else {}
    
    return {
        "title": "TGX Finance Crypto Assistant API",
        "version": "2.3.1",
        "description": "Real-time crypto assistant with live TGX Finance WebSocket data and fallback support",
        "protocol": "TGX Finance WebSocket API v1",
        "status": {
            "websocket_connected": ws_manager.connected if ws_manager else False,
            "authenticated": ws_manager._authenticated if ws_manager else False,
            "connection_status": market_summary.get("connection_status", "unknown"),
            "has_fallback_data": market_summary.get("has_fallback_data", False),
            "fallback_source": market_summary.get("fallback_source", "N/A"),
            "active_chats": len(chat_sessions)
        },
        "frontend_endpoints": {
            "/api/chat": "Create new chat (POST with {user_input: string})",
            "/api/chat/{chat_id}/stream": "Stream chat response (POST with {user_input: string})",
            "/api/chat/{chat_id}": "Get chat session (GET)",
            "/api/chats": "List all chats (GET)",
            "/api/chat/{chat_id}": "Delete chat (DELETE)"
        },
        "debug_endpoints": {
            "/debug": "Comprehensive debug information",
            "/health": "Health check with detailed status",
            "/market-data": "Live market data with debug info"
        },
        "legacy_endpoints": {
            "/ask?user_input=<question>": "Ask crypto questions (plain text response)",
            "/chat": "Legacy chat endpoint (POST with JSON)",
            "/subscribe": "Manual subscription to contract feeds"
        },
        "supported_topics": [
            "market.contract.switch",
            "contracts.market", 
            "market.ticker",
            "market.price.index",
            "contract.applies"
        ],
        "sample_queries": [
            "What's the current BTC price?",
            "What's the current ETH price?",
            "Show me trading volume",
            "What's the 24h high and low?",
            "Debug status",
            "Connection status"
        ],
        "features": {
            "fallback_api": config.get("use_fallback_api", True),
            "debug_mode": config.get("debug_mode", False),
            "authentication": bool(config.get("token") and config.get("secret")),
            "auto_reconnect": True,
            "heartbeat_monitoring": True,
            "eth_support": True
        }
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
        logger.info(f"🚀 Starting Enhanced TGX Finance Crypto Bot on port {port}")
        
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

        # Print configuration info
        logger.info(f"""
🔧 Enhanced TGX Finance Configuration:
• WebSocket URL: {config['ws_url']}
• App ID: {config['app_id']}
• Authentication: {'Enabled' if config.get('token') and config.get('secret') else 'Disabled (public feeds only)'}
• Heartbeat interval: {config['heartbeat_interval']}s
• Port: {port}
• Fallback API: {'Enabled' if config['use_fallback_api'] else 'Disabled'}
• Debug Mode: {'Enabled' if config['debug_mode'] else 'Disabled'}
• Frontend Compatible: ✅ (POST /api/chat, POST /api/chat/{{id}}/stream)
• Debug Endpoints: ✅ (/debug, /health with enhanced info)
• ETH Support: ✅ (Now subscribing to ETHUSDT feeds)
        """)

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