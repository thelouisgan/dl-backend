#!/usr/bin/env python3
"""
TGX Live Trading Tool for MCP Server
===================================
Provides live trading functionality through TGX Finance API
"""

import requests
import time
import hashlib
import urllib.parse
import json
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)

class TGXLiveTradingTool:
    """Live trading interface for TGX Finance API"""
    
    def __init__(self, token: str, secret_key: str):
        self.BASE_URL = "https://api.tgx.finance"
        self.TOKEN = token
        self.SECRET_KEY = secret_key
        
        # Safety limits
        self.MAX_LEVERAGE = 10  # Maximum allowed leverage
        self.MAX_TRADE_SIZE = 100  # Maximum trade volume
        self.MIN_TRADE_SIZE = 1   # Minimum trade volume
        
    async def initialize(self):
        """Initialize the trading tool"""
        logger.info("🔧 TGX Trading Tool initialized")
        
    async def close(self):
        """Close the trading tool"""
        logger.info("✅ TGX Trading Tool closed")
    
    def get_sig(self, data: Dict[str, Any], public: Dict[str, Any]) -> str:
        """Generate API signature"""
        params = {**data, **public}
        params = {k: v for k, v in params.items() if v not in (None, "", [], {}) and not isinstance(v, (list, dict, bytes))}
        sorted_items = sorted(params.items())
        encoded = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted_items)
        string_to_sign = encoded + self.SECRET_KEY
        md5_1 = hashlib.md5(string_to_sign.encode()).hexdigest()
        md5_2 = hashlib.md5(md5_1.encode()).hexdigest()
        signature = hashlib.sha256(md5_2.encode()).hexdigest()
        return signature
    
    def signed_request(self, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make authenticated API request"""
        if data is None:
            data = {}
            
        max_retries = 3
        for attempt in range(max_retries):
            try:
                ts = int(time.time())
                public = {
                    "api_version": "V2",
                    "device": "TradingBot",
                    "device_id": "mcp-trading-tool",
                    "version": "1.0.0",
                    "req_os": 3,
                    "req_lang": 1,
                    "ts": ts,
                }
                
                sig = self.get_sig(data, public)
                payload = {**public, "data": data, "sig": sig}
                
                headers = {
                    "Content-Type": "application/json",
                    "token": self.TOKEN,
                    "User-Agent": "TGXTradingTool/1.0",
                    "Accept": "application/json",
                }
                
                response = requests.post(self.BASE_URL + path, json=payload, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    logger.warning("🚫 Rate limited, waiting...")
                    time.sleep(10)
                    
            except Exception as e:
                logger.warning(f"Request failed: {e}")
                
            if attempt < max_retries - 1:
                time.sleep(2)
                
        return {"ret": -1, "msg": "Request failed"}
    
    async def get_account_info(self) -> str:
        """Get account information and balance"""
        try:
            logger.info("📊 Getting account information...")
            
            # Get account balance
            balance_res = self.signed_request("/v1/account/balance", {})
            
            if balance_res.get("ret") != 0:
                return f"❌ Failed to get account balance: {balance_res.get('msg', 'Unknown error')}"
            
            balance_data = balance_res.get("data", {})
            
            # Format balance information
            account_info = "💰 **TGX Finance Account Status**\n\n"
            
            if "balance" in balance_data:
                balance = float(balance_data.get("balance", 0))
                available = float(balance_data.get("available_balance", 0))
                frozen = float(balance_data.get("frozen_balance", 0))
                
                account_info += f"💵 **Total Balance**: ${balance:,.2f}\n"
                account_info += f"✅ **Available**: ${available:,.2f}\n"
                account_info += f"🔒 **Frozen**: ${frozen:,.2f}\n\n"
            
            # Get current positions
            positions_info = await self.get_positions()
            account_info += positions_info
            
            # Get recent orders
            orders_info = await self.get_recent_orders()
            account_info += "\n" + orders_info
            
            return account_info
            
        except Exception as e:
            logger.error(f"Error getting account info: {e}")
            return f"❌ Error getting account information: {str(e)}"
    
    async def get_positions(self) -> str:
        """Get current open positions"""
        try:
            logger.info("📈 Getting current positions...")
            
            # Try multiple contracts
            contracts = ["BTCUSDT", "ETHUSDT"]
            all_positions = []
            
            for contract in contracts:
                res = self.signed_request("/v1/order/hold", {
                    "contract_code": contract,
                    "page": 0,
                    "count": 20
                })
                
                if res.get("ret") == 0:
                    positions = res.get("data", {}).get("list", [])
                    all_positions.extend(positions)
            
            if not all_positions:
                return "📊 **Current Positions**: No open positions"
            
            positions_text = "📊 **Current Positions**:\n\n"
            total_pnl = 0.0
            
            for pos in all_positions:
                symbol = pos.get("contract_code", "Unknown")
                side = "🟢 LONG" if pos.get("side") == "B" else "🔴 SHORT"
                volume = int(pos.get("hold_volume", 0))
                avg_price = float(pos.get("hold_avg_price", 0))
                current_price = float(pos.get("current_price", 0))
                unrealized_pnl = float(pos.get("unrealized_pnl", 0))
                
                total_pnl += unrealized_pnl
                
                positions_text += f"📈 **{symbol}** {side}\n"
                positions_text += f"   Volume: {volume}\n"
                positions_text += f"   Avg Price: ${avg_price:,.2f}\n"
                positions_text += f"   Current: ${current_price:,.2f}\n"
                positions_text += f"   P&L: ${unrealized_pnl:+.2f}\n\n"
            
            positions_text += f"💰 **Total Unrealized P&L**: ${total_pnl:+.2f}"
            
            return positions_text
            
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return f"❌ Error getting positions: {str(e)}"
    
    async def get_recent_orders(self, limit: int = 10) -> str:
        """Get recent order history"""
        try:
            logger.info("📋 Getting recent orders...")
            
            # Get order history
            contracts = ["BTCUSDT", "ETHUSDT"]
            all_orders = []
            
            for contract in contracts:
                res = self.signed_request("/v1/order/history", {
                    "contract_code": contract,
                    "page": 0,
                    "count": limit,
                    "order_type": 0  # All orders
                })
                
                if res.get("ret") == 0:
                    orders = res.get("data", {}).get("list", [])
                    all_orders.extend(orders)
            
            if not all_orders:
                return "📋 **Recent Orders**: No recent orders"
            
            # Sort by timestamp (most recent first)
            all_orders.sort(key=lambda x: x.get("create_time", 0), reverse=True)
            recent_orders = all_orders[:limit]
            
            orders_text = f"📋 **Recent Orders** (Last {len(recent_orders)}):\n\n"
            
            for order in recent_orders:
                symbol = order.get("contract_code", "Unknown")
                side = "🟢 BUY" if order.get("side") == "B" else "🔴 SELL"
                volume = int(order.get("order_quantity", 0))
                price = float(order.get("entrust_price", 0))
                status = self._get_order_status(order.get("status", 0))
                timestamp = datetime.fromtimestamp(order.get("create_time", 0) / 1000).strftime("%Y-%m-%d %H:%M:%S")
                
                orders_text += f"🕐 {timestamp}\n"
                orders_text += f"   {symbol} {side} {volume} @ ${price:,.2f}\n"
                orders_text += f"   Status: {status}\n\n"
            
            return orders_text
            
        except Exception as e:
            logger.error(f"Error getting recent orders: {e}")
            return f"❌ Error getting recent orders: {str(e)}"
    
    def _get_order_status(self, status_code: int) -> str:
        """Convert order status code to readable text"""
        status_map = {
            0: "⏳ Pending",
            1: "✅ Filled", 
            2: "❌ Cancelled",
            3: "⚡ Partial Fill",
            4: "🔄 Processing"
        }
        return status_map.get(status_code, f"Unknown ({status_code})")
    
    async def place_limit_order(self, symbol: str, side: str, quantity: int, price: float, leverage: int = 5) -> str:
        """Place a limit order"""
        try:
            # Validate inputs
            if not self._validate_trade_params(symbol, side, quantity, price, leverage):
                return "❌ Invalid trade parameters"
            
            side_code = "B" if side.upper() in ["BUY", "B", "LONG"] else "S"
            side_text = "BUY" if side_code == "B" else "SELL"
            
            logger.info(f"📝 Placing limit order: {side_text} {quantity} {symbol} @ ${price:,.2f} (Leverage: {leverage}x)")
            
            order_data = {
                "contract_code": symbol,
                "side": side_code,
                "entrust_type": 1,  # Limit order
                "leverage": leverage,
                "hold_type": 1,
                "order_quantity": quantity,
                "entrust_price": str(round(price, 2))
            }
            
            res = self.signed_request("/v1/order/place", order_data)
            
            if res.get("ret") == 0:
                order_id = res.get("data", {}).get("order_id", "Unknown")
                
                success_msg = f"✅ **Order Placed Successfully**\n\n"
                success_msg += f"📊 **Details**:\n"
                success_msg += f"   Symbol: {symbol}\n"
                success_msg += f"   Side: {side_text}\n"
                success_msg += f"   Quantity: {quantity}\n"
                success_msg += f"   Price: ${price:,.2f}\n"
                success_msg += f"   Leverage: {leverage}x\n"
                success_msg += f"   Order ID: {order_id}\n\n"
                success_msg += f"⚠️ **Order Status**: Pending execution\n"
                success_msg += f"💡 **Tip**: Use 'check account status' to monitor your order"
                
                logger.info(f"✅ Order placed successfully: {order_id}")
                return success_msg
            else:
                error_msg = res.get("msg", "Unknown error")
                logger.error(f"❌ Order failed: {error_msg}")
                return f"❌ **Order Failed**: {error_msg}"
                
        except Exception as e:
            logger.error(f"Order placement error: {e}")
            return f"❌ **Order Error**: {str(e)}"
    
    async def cancel_order(self, order_id: str) -> str:
        """Cancel an existing order"""
        try:
            logger.info(f"❌ Cancelling order: {order_id}")
            
            res = self.signed_request("/v1/order/cancel", {
                "order_id": order_id
            })
            
            if res.get("ret") == 0:
                return f"✅ **Order Cancelled Successfully**\n\nOrder ID: {order_id}"
            else:
                error_msg = res.get("msg", "Unknown error")
                return f"❌ **Cancel Failed**: {error_msg}"
                
        except Exception as e:
            logger.error(f"Cancel order error: {e}")
            return f"❌ **Cancel Error**: {str(e)}"
    
    async def close_position(self, symbol: str, quantity: int = None) -> str:
        """Close a position (market order)"""
        try:
            logger.info(f"🔴 Closing position: {symbol}")
            
            # Get current position first
            positions_res = self.signed_request("/v1/order/hold", {
                "contract_code": symbol,
                "page": 0,
                "count": 10
            })
            
            if positions_res.get("ret") != 0:
                return f"❌ **Position Error**: Could not get position data"
            
            positions = positions_res.get("data", {}).get("list", [])
            if not positions:
                return f"❌ **No Position**: No open position found for {symbol}"
            
            position = positions[0]  # Take first position
            position_id = position.get("position_id")
            current_volume = int(position.get("hold_volume", 0))
            
            # Use specified quantity or close entire position
            close_quantity = quantity if quantity else current_volume
            
            if close_quantity > current_volume:
                return f"❌ **Invalid Quantity**: Cannot close {close_quantity}, only {current_volume} available"
            
            res = self.signed_request("/v1/position/closeout", {
                "position_id": position_id,
                "close_type": 2,  # Market close
                "order_quantity": close_quantity,
                "entrust_type": 0  # Market order
            })
            
            if res.get("ret") == 0:
                success_msg = f"✅ **Position Closed Successfully**\n\n"
                success_msg += f"📊 **Details**:\n"
                success_msg += f"   Symbol: {symbol}\n"
                success_msg += f"   Quantity: {close_quantity}\n"
                success_msg += f"   Close Type: Market Order\n\n"
                success_msg += f"💡 **Tip**: Check account status for updated P&L"
                
                return success_msg
            else:
                error_msg = res.get("msg", "Unknown error")
                return f"❌ **Close Failed**: {error_msg}"
                
        except Exception as e:
            logger.error(f"Close position error: {e}")
            return f"❌ **Close Error**: {str(e)}"
    
    def _validate_trade_params(self, symbol: str, side: str, quantity: int, price: float, leverage: int) -> bool:
        """Validate trading parameters"""
        # Check symbol
        valid_symbols = ["BTCUSDT", "ETHUSDT", "BTC", "ETH"]
        if symbol.upper() not in valid_symbols:
            logger.error(f"Invalid symbol: {symbol}")
            return False
        
        # Check side
        valid_sides = ["BUY", "SELL", "B", "S", "LONG", "SHORT"]
        if side.upper() not in valid_sides:
            logger.error(f"Invalid side: {side}")
            return False
        
        # Check quantity
        if not (self.MIN_TRADE_SIZE <= quantity <= self.MAX_TRADE_SIZE):
            logger.error(f"Invalid quantity: {quantity} (must be {self.MIN_TRADE_SIZE}-{self.MAX_TRADE_SIZE})")
            return False
        
        # Check price
        if price <= 0:
            logger.error(f"Invalid price: {price}")
            return False
        
        # Check leverage
        if not (1 <= leverage <= self.MAX_LEVERAGE):
            logger.error(f"Invalid leverage: {leverage} (must be 1-{self.MAX_LEVERAGE})")
            return False
        
        return True