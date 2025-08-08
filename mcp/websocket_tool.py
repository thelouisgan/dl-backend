#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import ssl
import socket
import time
import aiohttp
import websockets
from datetime import datetime
from typing import Dict, Any, Optional
import hashlib
import hmac
from dotenv import load_dotenv

# Setup logging
logger = logging.getLogger(__name__)

class TGXMarketDataTool:
    """MCP Tool for TGX Finance WebSocket market data"""
    
    def __init__(self):
        load_dotenv()
        
        self.config = {
            "ws_url": os.getenv("WS_URL", "wss://api.tgx.finance/v1/ws/"),
            "app_id": int(os.getenv("APP_ID", "1")),
            "token": os.getenv("TOKEN", ""),
            "secret": os.getenv("SECRET", ""),
            "ping_interval": int(os.getenv("WS_PING_INTERVAL", "30")),
            "ping_timeout": int(os.getenv("WS_PING_TIMEOUT", "10")),
            "heartbeat_interval": int(os.getenv("HEARTBEAT_INTERVAL", "50")),
            "ssl_enabled": os.getenv("SSL_ENABLED", "true").lower() == "true",
            "use_fallback_api": os.getenv("USE_FALLBACK_API", "true").lower() == "true",
        }
        
        self.connection = None
        self.connected = False
        self._stop_event = asyncio.Event()
        self._authenticated = False
        self._heartbeat_task = None
        self._fallback_task = None
        
        self._market_data = {
            "btc_ticker": {},
            "eth_ticker": {},
            "connection_status": "disconnected",
            "last_update": None,
            "message_count": 0,
            "connection_attempts": 0,
            "connection_errors": [],
            "fallback_data": {}
        }

    async def initialize(self):
        """Initialize the TGX WebSocket connection"""
        logger.info("🔧 Initializing TGX Finance MCP Tool...")
        
        # Start WebSocket connection
        await self.connect()
        
        # Start fallback data fetching if enabled
        if self.config["use_fallback_api"] and not self._fallback_task:
            self._fallback_task = asyncio.create_task(self._fallback_data_loop())
            
        logger.info("✅ TGX Finance MCP Tool initialized")

    async def connect(self):
        """Connect to TGX Finance WebSocket"""
        if self.connected and self.connection and not self.connection.closed:
            logger.info("Already connected to TGX Finance!")
            return

        max_retries = 3
        retry_count = 0
        
        self._market_data["connection_attempts"] += 1

        while retry_count < max_retries and not self._stop_event.is_set():
            try:
                logger.info(f"🔄 Connecting to TGX Finance: {self.config['ws_url']} (attempt {retry_count + 1})")
                
                ssl_context = None
                if self.config["ssl_enabled"]:
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE

                self.connection = await websockets.connect(
                    self.config["ws_url"],
                    ping_interval=self.config["ping_interval"],
                    ping_timeout=self.config["ping_timeout"],
                    ssl=ssl_context
                )
                
                self.connected = True
                self._market_data["connection_status"] = "connected"
                self._market_data["connection_errors"].clear()
                logger.info("✅ Connected to TGX Finance successfully!")

                # Authenticate if credentials provided
                if self.config.get("token") and self.config.get("secret"):
                    await self._authenticate()
                
                # Subscribe to market feeds
                await self._subscribe_to_feeds()
                
                # Start heartbeat
                if self._heartbeat_task is None or self._heartbeat_task.done():
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                
                # Start message listening
                asyncio.create_task(self._listen_messages())
                break
                
            except Exception as e:
                error_msg = f"Connection attempt {retry_count + 1} failed: {str(e)}"
                logger.error(error_msg)
                
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
                    await asyncio.sleep(2.0)
        
        if retry_count >= max_retries:
            self._market_data["connection_status"] = "failed_max_retries"
            logger.error("❌ Failed to connect after maximum attempts. Using fallback data only.")

    async def _authenticate(self):
        """Authenticate with TGX Finance"""
        try:
            ts = int(time.time())
            auth_data = {
                "app_id": self.config["app_id"],
                "token": self.config["token"],
                "ts": ts
            }
            
            if self.config.get("secret"):
                auth_data["sign"] = self._create_signature(auth_data, self.config["secret"])
            
            auth_message = {
                "action": "auth",
                "topic": "auth",
                "data": auth_data
            }
            
            await self.connection.send(json.dumps(auth_message))
            logger.info("🔐 Authentication request sent")
            
        except Exception as e:
            logger.warning(f"Authentication failed: {e}")

    def _create_signature(self, data: Dict[str, Any], secret: str) -> str:
        """Create HMAC signature for authentication"""
        sorted_params = sorted(data.items())
        query_string = "&".join([f"{k}={v}" for k, v in sorted_params if k != "sign"])
        signature = hmac.new(
            secret.encode('utf-8'), 
            query_string.encode('utf-8'), 
            hashlib.sha256
        ).hexdigest()
        return signature

    async def _subscribe_to_feeds(self):
        """Subscribe to BTC and ETH market feeds"""
        if not self.connection or not self.connected:
            return

        try:
            # Subscribe to BTC
            await self.connection.send(json.dumps({
                "action": "sub",
                "topic": "market.contract.switch",
                "data": {"contract_code": "BTCUSDT"}
            }))
            
            # Subscribe to ETH
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
            
            logger.info("📡 Subscribed to BTC/ETH market feeds")
            
        except Exception as e:
            logger.error(f"Subscription error: {e}")

    async def _heartbeat_loop(self):
        """Send periodic heartbeats"""
        try:
            while self.connected and not self._stop_event.is_set():
                await asyncio.sleep(self.config["heartbeat_interval"])
                if self.connection and self.connected:
                    try:
                        ts = int(time.time())
                        heartbeat_data = {"app_id": self.config["app_id"], "ts": ts}
                        
                        if self.config.get("token"):
                            heartbeat_data["token"] = self.config["token"]
                            if self.config.get("secret"):
                                heartbeat_data["sign"] = self._create_signature(heartbeat_data, self.config["secret"])
                        
                        heartbeat_msg = {
                            "action": "heart",
                            "topic": "heart", 
                            "data": heartbeat_data
                        }
                        
                        await self.connection.send(json.dumps(heartbeat_msg))
                        logger.debug("💓 Heartbeat sent")
                        
                    except Exception as e:
                        logger.warning(f"Heartbeat failed: {e}")
                        break
        except Exception as e:
            logger.error(f"Heartbeat loop error: {e}")

    async def _listen_messages(self):
        """Listen for incoming WebSocket messages"""
        try:
            while not self._stop_event.is_set() and self.connected:
                try:
                    message = await asyncio.wait_for(self.connection.recv(), timeout=120.0)
                    await self._process_message(message)
                    
                except asyncio.TimeoutError:
                    logger.debug("Message timeout - checking connection")
                    if self.connection and hasattr(self.connection, 'closed') and self.connection.closed:
                        logger.warning("Connection closed during timeout")
                        break
                        
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("WebSocket connection closed")
                    self.connected = False
                    break
                    
                except Exception as e:
                    logger.error(f"Error receiving message: {e}")
                    break
                    
        except Exception as e:
            logger.error(f"Message listener error: {e}")
        finally:
            await self._cleanup_connection()

    async def _process_message(self, message: str):
        """Process incoming TGX messages"""
        try:
            data = json.loads(message)
            self._market_data["message_count"] += 1
            self._market_data["last_update"] = datetime.now().isoformat()

            action = data.get("action")
            topic = data.get("topic")
            msg_data = data.get("data", {})

            if action == "notify":
                await self._handle_notification(topic, msg_data, data.get("ts"))
                
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON: {message[:200]}...")
        except Exception as e:
            logger.error(f"Message processing error: {e}")

    async def _handle_notification(self, topic: str, data: Dict[str, Any], timestamp: Optional[int]):
        """Handle notification messages"""
        try:
            if topic == "market.ticker":
                contract_code = data.get("contract_code", "")
                trade_list = data.get("list", [])
                
                if trade_list:
                    latest_trade = trade_list[0]
                    price = latest_trade.get("trade_price", latest_trade.get("price", ""))
                    
                    if "BTC" in contract_code:
                        self._market_data["btc_ticker"].update({
                            "contract_code": contract_code,
                            "price": price,
                            "last_trade": latest_trade,
                            "timestamp": timestamp or int(time.time())
                        })
                        
                    elif "ETH" in contract_code:
                        self._market_data["eth_ticker"].update({
                            "contract_code": contract_code,
                            "price": price,
                            "last_trade": latest_trade,
                            "timestamp": timestamp or int(time.time())
                        })

            elif topic == "contracts.market":
                if isinstance(data, list):
                    for contract in data:
                        contract_code = contract.get("contract_code", "").upper()
                        
                        if "BTC" in contract_code:
                            self._market_data["btc_ticker"].update({
                                "contract_code": contract_code,
                                "price": contract.get("price", ""),
                                "index_price": contract.get("index_price", ""),
                                "change_ratio": contract.get("change_ratio", ""),
                                "change": contract.get("change", ""),
                                "buy_count": contract.get("buy_count", 0),
                                "sell_count": contract.get("sell_count", 0)
                            })
                            
                        elif "ETH" in contract_code:
                            self._market_data["eth_ticker"].update({
                                "contract_code": contract_code,
                                "price": contract.get("price", ""),
                                "index_price": contract.get("index_price", ""),
                                "change_ratio": contract.get("change_ratio", ""),
                                "change": contract.get("change", ""),
                                "buy_count": contract.get("buy_count", 0),
                                "sell_count": contract.get("sell_count", 0)
                            })

            elif topic == "contract.applies":
                contract_code = data.get("contract_code", "").upper()
                
                update_data = {
                    "contract_code": contract_code,
                    "change_ratio": data.get("change_ratio", ""),
                    "change": data.get("change", ""),
                    "high_24h": data.get("high_price", ""),
                    "low_24h": data.get("low_price", ""),
                    "volume_24h": data.get("trade_24h", "")
                }
                
                if "BTC" in contract_code:
                    self._market_data["btc_ticker"].update(update_data)
                elif "ETH" in contract_code:
                    self._market_data["eth_ticker"].update(update_data)
                    
        except Exception as e:
            logger.error(f"Notification handling error for topic {topic}: {e}")

    async def _fallback_data_loop(self):
        """Fetch fallback data from public APIs"""
        while not self._stop_event.is_set():
            try:
                await self._fetch_fallback_data()
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Fallback data loop error: {e}")
                await asyncio.sleep(30)

    async def _fetch_fallback_data(self):
        """Fetch data from CoinGecko as fallback"""
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        self._market_data["fallback_data"] = {
                            "btc": {
                                "price": str(data["bitcoin"]["usd"]),
                                "change_24h": str(data["bitcoin"].get("usd_24h_change", 0)),
                                "symbol": "BTCUSDT"
                            },
                            "eth": {
                                "price": str(data["ethereum"]["usd"]),
                                "change_24h": str(data["ethereum"].get("usd_24h_change", 0)),
                                "symbol": "ETHUSDT"
                            },
                            "source": "CoinGecko",
                            "timestamp": datetime.now().isoformat()
                        }
                        
                        logger.info("📡 Fallback data updated from CoinGecko")
                        return True
                        
        except Exception as e:
            logger.warning(f"Failed to fetch fallback data: {e}")
            
        return False

    async def _cleanup_connection(self):
        """Clean up WebSocket connection"""
        self.connected = False
        if self.connection:
            try:
                await self.connection.close()
            except:
                pass
        self.connection = None
        self._market_data["connection_status"] = "disconnected"

    # === MCP Tool Methods ===
    
    async def get_market_data(self, symbol: str = "all") -> str:
        """Get live market data for specified symbol"""
        try:
            btc_ticker = self._market_data.get("btc_ticker", {})
            eth_ticker = self._market_data.get("eth_ticker", {})
            fallback_data = self._market_data.get("fallback_data", {})
            
            def format_coin_data(coin_name: str, ticker_data: dict, fallback_coin_data: dict) -> str:
                # Use live data if available, otherwise fallback
                data = ticker_data if ticker_data.get("price") else fallback_coin_data
                
                if not data.get("price"):
                    return f"{coin_name}: No data available"
                
                price = data.get("price", "N/A")
                change = data.get("change_24h", data.get("change_ratio", "N/A"))
                volume = data.get("volume_24h", "N/A")
                source = "TGX Live" if ticker_data.get("price") else f"Fallback ({fallback_data.get('source', 'N/A')})"
                
                result = f"💎 {coin_name} ({data.get('contract_code', data.get('symbol', ''))}):\n"
                result += f"  Price: ${float(price):,.2f}\n" if price != "N/A" else f"  Price: {price}\n"
                
                if change != "N/A":
                    change_float = float(change)
                    if change_float > 0:
                        result += f"  24h Change: +{change_float:.2f}%\n"
                    else:
                        result += f"  24h Change: {change_float:.2f}%\n"
                
                if volume != "N/A":
                    result += f"  24h Volume: {volume}\n"
                    
                result += f"  Source: {source}"
                
                return result
            
            symbol_lower = symbol.lower()
            
            if symbol_lower in ["btc", "bitcoin"]:
                fallback_btc = fallback_data.get("btc", {})
                return format_coin_data("Bitcoin", btc_ticker, fallback_btc)
                
            elif symbol_lower in ["eth", "ethereum"]:
                fallback_eth = fallback_data.get("eth", {})
                return format_coin_data("Ethereum", eth_ticker, fallback_eth)
                
            else:  # "all" or any other value
                fallback_btc = fallback_data.get("btc", {})
                fallback_eth = fallback_data.get("eth", {})
                
                btc_data = format_coin_data("Bitcoin", btc_ticker, fallback_btc)
                eth_data = format_coin_data("Ethereum", eth_ticker, fallback_eth)
                
                return f"{btc_data}\n\n{eth_data}"
                
        except Exception as e:
            return f"Error retrieving market data: {str(e)}"

    async def get_connection_status(self) -> str:
        """Get TGX WebSocket connection status"""
        try:
            status = self._market_data["connection_status"]
            message_count = self._market_data["message_count"]
            last_update = self._market_data.get("last_update", "Never")
            connection_attempts = self._market_data["connection_attempts"]
            error_count = len(self._market_data["connection_errors"])
            
            result = f"🔍 TGX Finance WebSocket Status:\n"
            result += f"  Connection: {status}\n"
            result += f"  Authenticated: {self._authenticated}\n"
            result += f"  Messages Received: {message_count}\n"
            result += f"  Last Update: {last_update}\n"
            result += f"  Connection Attempts: {connection_attempts}\n"
            result += f"  Connection Errors: {error_count}\n"
            
            # Add fallback status
            fallback_data = self._market_data.get("fallback_data", {})
            if fallback_data:
                result += f"  Fallback Data: Available ({fallback_data.get('source', 'Unknown')})\n"
                result += f"  Fallback Updated: {fallback_data.get('timestamp', 'Unknown')}"
            else:
                result += f"  Fallback Data: Not available"
            
            # Add recent errors if any
            recent_errors = self._market_data["connection_errors"][-3:]
            if recent_errors:
                result += f"\n\nRecent Errors:"
                for error in recent_errors:
                    result += f"\n  • {error.get('error', 'Unknown error')}"
            
            return result
            
        except Exception as e:
            return f"Error getting connection status: {str(e)}"

    async def close(self):
        """Close the TGX tool and cleanup resources"""
        logger.info("🔌 Closing TGX Finance MCP Tool...")
        
        self._stop_event.set()
        
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            
        if self._fallback_task and not self._fallback_task.done():
            self._fallback_task.cancel()
            
        await self._cleanup_connection()
        
        logger.info("✅ TGX Finance MCP Tool closed")