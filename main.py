#Crispy2FINALPT2

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

load_dotenv()

config = {
    "ws_url": os.getenv("WS_URL", "wss://api.tgx.finance/v1/ws/"),
    "app_id": int(os.getenv("APP_ID", "1")),
    "token": os.getenv("TOKEN", ""),  # Optional - only needed for user-specific data
    "secret": os.getenv("SECRET", ""),  # Optional - for authentication
    "ping_interval": int(os.getenv("WS_PING_INTERVAL", "30")),
    "ping_timeout": int(os.getenv("WS_PING_TIMEOUT", "10")),
    "close_timeout": int(os.getenv("WS_CLOSE_TIMEOUT", "10")),
    "heartbeat_interval": int(os.getenv("HEARTBEAT_INTERVAL", "50")),  # 50s as per docs
    "default_port": int(os.getenv("PORT", "8000")),
    "port_range": 100,
    "ssl_enabled": os.getenv("SSL_ENABLED", "true").lower() == "true",
    "max_reconnect_attempts": 5,
    "reconnect_delay": 2.0
}

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
            "subscriptions": []
        }
        self._reconnect_task = None
        self._heartbeat_task = None
        self._authenticated = False

    async def connect(self):
        """Connect to TGX Finance WebSocket"""
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

        while retry_count < max_retries and not self._stop_event.is_set():
            try:
                logger.info(f"Connecting to TGX Finance WebSocket: {config['ws_url']} (attempt {retry_count + 1})")
                
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
                self.connected = False
                self.connection = None
                self._market_data["connection_status"] = "disconnected"
                logger.error(f"Connection failed: {e}")
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(config["reconnect_delay"])

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
        """Subscribe to TGX Finance data feeds using correct protocol"""
        if not self.connection or not self.connected:
            logger.warning("Cannot subscribe - not connected")
            return
    
        try:
            logger.info("📡 Subscribing to TGX Finance feeds...")
            

            btc_switch_sub = {
                "action": "sub",
                "topic": "market.contract.switch",
                "data": {
                    "contract_code": "BTCUSDT"
                }
            }
            await self.connection.send(json.dumps(btc_switch_sub))
            logger.info(f"📊 Subscribed to BTC contract feed")
            await asyncio.sleep(0.5)
            
            # 2. Subscribe to all contracts market data
            all_contracts_sub = {
                "action": "sub",
                "topic": "contracts.market",
                "data": {}
            }
            await self.connection.send(json.dumps(all_contracts_sub))
            logger.info(f"📈 Subscribed to all contracts market feed")
            await asyncio.sleep(0.5)
            
            self._market_data["subscriptions"] = [
                "market.contract.switch:BTCUSDT",
                "contracts.market",
            ]
            
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
                        timeout=120.0  # Longer timeout for TGX
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
        """Get a formatted market summary"""
        btc_ticker = self._market_data.get("btc_ticker", {})
        eth_ticker = self._market_data.get("eth_ticker", {})



        summary = {
            "btc": {
                "symbol": btc_ticker.get("contract_code", "BTCUSDT"),
                "price": btc_ticker.get("price", "N/A"),
                "index_price": btc_ticker.get("index_price", "N/A"),
                "change_24h": btc_ticker.get("change_ratio", "N/A"),
                "change_abs": btc_ticker.get("change", "N/A"),
                "volume": btc_ticker.get("volume_24h", "N/A"),
                "high_24h": btc_ticker.get("high_24h", "N/A"),
                "low_24h": btc_ticker.get("low_24h", "N/A"),
                "buy_count": btc_ticker.get("buy_count", 0),
                "sell_count": btc_ticker.get("sell_count", 0),
            },
        
            "eth": {
                "symbol": eth_ticker.get("contract_code", "ETHUSDT"),
                "price": eth_ticker.get("price", "N/A"),
                "index_price": eth_ticker.get("index_price", "N/A"),
                "change_24h": eth_ticker.get("change_ratio", "N/A"),
                "change_abs": eth_ticker.get("change", "N/A"),
                "volume": eth_ticker.get("volume_24h", "N/A"),
                "high_24h": eth_ticker.get("high_24h", "N/A"),
                "low_24h": eth_ticker.get("low_24h", "N/A"),
                "buy_count": eth_ticker.get("buy_count", 0),
                "sell_count": eth_ticker.get("sell_count", 0),
            },
            "connection_status": self._market_data["connection_status"],
            "last_update": self._market_data["last_update"],
            "message_count": self._market_data["message_count"],
            "authenticated": self._authenticated,
            "subscriptions": len(self._market_data["subscriptions"]),
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
        
        return False
        
    def generate_response(self, query: str) -> str:
        """Generate AI-like responses based on query and market data"""
        query_lower = query.lower()
        market_data = self.ws_manager.get_market_summary()
        
        connection_status = market_data.get("connection_status", "unknown")
        message_count = market_data.get("message_count", 0)
        authenticated = market_data.get("authenticated", False)
        
        # Check if we should use fallback data
        use_fallback = (market_data.get("price", "N/A") == "N/A" and 
                       self._fallback_data.get("price") is not None)
        
        if any(word in query_lower for word in ["price", "cost", "value", "btc", "bitcoin"]):
            price = market_data.get("price", "N/A")
            index_price = market_data.get("index_price", "N/A")
            change = market_data.get("change_24h", "N/A")
            
            if price != "N/A":
                response = f"💰 Bitcoin (BTC) is currently trading at {format_price(str(price))}"
                if index_price != "N/A":
                    response += f" (Index: {format_price(str(index_price))})"
                if change != "N/A":
                    response += f". The 24h change is {change}%"
                response += ". Data is live from TGX Finance!"
                return response
            elif use_fallback:
                fallback_price = self._fallback_data["price"]
                source = self._fallback_data["source"]
                return f"💰 Bitcoin (BTC) is currently trading at {format_price(str(fallback_price))} (via {source} - TGX Finance: {connection_status}). TGX live data coming soon!"
            else:
                return f"💰 Bitcoin price data is currently unavailable. Connection: {connection_status} | Messages: {message_count} | Auth: {'✅' if authenticated else '❌'}"
        
        elif any(word in query_lower for word in ["volume", "trading", "activity", "buy", "sell"]):
            volume = market_data.get("volume", "N/A")
            buy_count = market_data.get("buy_count", 0)
            sell_count = market_data.get("sell_count", 0)
            
            response = f"📊 Trading Data:"
            if volume != "N/A":
                response += f"\n• 24h Volume: {volume}"
            if buy_count or sell_count:
                response += f"\n• Bullish traders: {buy_count}"
                response += f"\n• Bearish traders: {sell_count}"
                sentiment = "Bullish" if buy_count > sell_count else "Bearish" if sell_count > buy_count else "Neutral"
                response += f"\n• Market sentiment: {sentiment}"
            
            if volume == "N/A" and not buy_count and not sell_count:
                response = f"📊 Trading volume data is currently unavailable. Connection: {connection_status}"
            
            return response
        
        elif any(word in query_lower for word in ["high", "low", "range", "24h"]):
            high = market_data.get("high_24h", "N/A")
            low = market_data.get("low_24h", "N/A")
            change = market_data.get("change_abs", "N/A")
            
            if high != "N/A" or low != "N/A":
                response = f"📈 24h BTC Range:"
                if high != "N/A":
                    response += f"\n• High: {format_price(str(high))}"
                if low != "N/A":
                    response += f"\n• Low: {format_price(str(low))}"
                if change != "N/A":
                    response += f"\n• Net Change: {change}"
                return response
            else:
                return f"📈 Price range data is currently unavailable. Status: {connection_status}"
        
        
        elif any(word in query_lower for word in ["status", "connection", "working", "online", "debug"]):
            full_data = self.ws_manager.get_full_market_data()
            subscriptions = full_data.get("subscriptions", [])
            
            return f"""🔍 **TGX Finance System Status:**
• Connection: {connection_status}
• Authentication: {'✅ Authenticated' if authenticated else '❌ Not authenticated'}
• Messages received: {message_count}
• Active subscriptions: {len(subscriptions)}
• Last update: {market_data.get('last_update', 'Never')}


Subscribed feeds: {', '.join(subscriptions) if subscriptions else 'None'}
Protocol: TGX Finance WebSocket API v1"""
        
        else:
            # Default response with current data
            price = market_data.get("price", "N/A")
            if price != "N/A":
                return f"🤖 I'm your TGX Finance crypto assistant! Current BTC: {format_price(str(price))} ({market_data.get('change_24h', 'N/A')}% 24h). Ask me about prices, volume or ranges!"
            elif use_fallback:
                fallback_price = self._fallback_data["price"]
                return f"🤖 TGX Finance crypto assistant ready! BTC: {format_price(str(fallback_price))} (backup feed). Establishing live TGX connection..."
            else:
                return f"🤖 TGX Finance crypto assistant connecting... Status: {connection_status} | Messages: {message_count}. Ask about connection status or try again in a moment!"

    async def generate_streaming_response(self, query: str):
        """Generate streaming response"""
        try:
            # Fetch fallback data if needed
            market_data = self.ws_manager.get_market_summary()
            if market_data.get("price", "N/A") == "N/A" and not self._fallback_data:
                await self._fetch_fallback_data()
            
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
    description="Real-time cryptocurrency assistant powered by TGX Finance WebSocket feeds",
    version="2.2.0",
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
    """Main ask endpoint with streaming response"""
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
            "authenticated": ws_manager._authenticated,
            "summary": summary,
            "full_data": full_data,
            "subscriptions": full_data.get("subscriptions", []),
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
                "authenticated": ws_manager._authenticated if ws_manager else False,
                "status": ws_manager._market_data.get("connection_status", "unknown") if ws_manager else "not_initialized"
            },
            "ai": {
                "available": crypto_ai is not None,
                "status": "ready" if crypto_ai else "not_initialized"
            },
            "market_data": {
                "available": bool(ws_manager.get_full_market_data() if ws_manager else False),
                "message_count": ws_manager._market_data.get("message_count", 0) if ws_manager else 0,
                "subscriptions": len(ws_manager._market_data.get("subscriptions", [])) if ws_manager else 0
            }
        }
    }
    
    if not ws_manager or not ws_manager.connected or not crypto_ai:
        health_status["status"] = "degraded"
    
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

@app.get("/")
async def root():
    """API documentation root"""
    return {
        "title": "TGX Finance Crypto Assistant API",
        "version": "2.2.0",
        "description": "Real-time crypto assistant with live TGX Finance WebSocket data",
        "protocol": "TGX Finance WebSocket API v1",
        "endpoints": {
            "/": "API documentation",
            "/health": "Health check and service status",
            "/market-data": "Live market data from TGX Finance",
            "/ask?user_input=<question>": "Ask crypto questions (streaming)",
            "/api/chat": "Main chat endpoint (POST with JSON)",
            "/subscribe": "Manual subscription to contract feeds"
        },
        "websocket_status": ws_manager.connected if ws_manager else False,
        "authenticated": ws_manager._authenticated if ws_manager else False,
        "supported_topics": [
            "market.contract.switch",
            "contracts.market", 
            "market.ticker",
            "market.price.index",
            "contract.applies"
        ],
        "sample_queries": [
            "What's the current BTC price?",
            "Show me trading volume",
            "What's the 24h high and low?",
            "Connection status"
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

        # Print configuration info
        logger.info(f"""
🔧 TGX Finance Configuration:
• WebSocket URL: {config['ws_url']}
• App ID: {config['app_id']}
• Authentication: {'Enabled' if config.get('token') and config.get('secret') else 'Disabled (public feeds only)'}
• Heartbeat interval: {config['heartbeat_interval']}s
• Port: {port}
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