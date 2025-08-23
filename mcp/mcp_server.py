#!/usr/bin/env python3

import asyncio
import json
import logging
from typing import Any, Sequence, Dict, Optional
from datetime import datetime
import os
from contextlib import asynccontextmanager
import concurrent.futures
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import AzureChatOpenAI
from langchain_core.tools import BaseTool
from langchain_core.callbacks.manager import CallbackManagerForToolRun
from pydantic import Field

# Import MCP tools
from websocket_tool import TGXMarketDataTool
from jesse_tool import JesseHistoricalTool
from jesse_chart_tool import JesseChartTool
from tgx_trading_tool import TGXLiveTradingTool

# === Load Environment Variables ===
load_dotenv(dotenv_path=os.path.join("config", ".env"))

api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('mcp_master_server_fixed.log', mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# === Pydantic Models ===
class ChatRequest(BaseModel):
    messages: list

# === MCP Tool Wrappers ===
class MCPToolManager:
    def __init__(self):
        token = os.getenv("TGX_API_TOKEN")
        secret_key = os.getenv("TGX_API_SECRET_KEY")

        if token is None or secret_key is None:
            raise ValueError("TGX_API_TOKEN and TGX_API_SECRET_KEY must be set in environment.")
        
        self.tgx_tool = TGXMarketDataTool()
        self.jesse_tool = JesseHistoricalTool()
        self.jesse_chart_tool = JesseChartTool()
        self.tgx_trading_tool = TGXLiveTradingTool(token, secret_key)
        self.tools_initialized = False
        
    async def initialize(self):
        """Initialize MCP tools"""
        try:
            logger.info("🔧 Initializing MCP tools...")
            await self.tgx_tool.initialize()
            await self.jesse_tool.initialize()
            await self.jesse_chart_tool.initialize()
            await self.tgx_trading_tool.initialize()
            self.tools_initialized = True
            logger.info("✅ MCP tools initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize MCP tools: {e}")
            raise
    
    async def close(self):
        """Close MCP tools"""
        try:
            await self.tgx_tool.close()
            await self.jesse_tool.close()
            await self.jesse_chart_tool.close()
            await self.tgx_trading_tool.close()
            logger.info("✅ MCP tools closed successfully")
        except Exception as e:
            logger.error(f"❌ Error closing MCP tools: {e}")

# Global tool manager
tool_manager = None

# === Helper function to run async code in sync context ===
def run_async_safely(coro):
    """Safely run async code in sync context"""
    def run_in_thread():
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()
    
    try:
        # Try to get current loop
        loop = asyncio.get_running_loop()
        # If we're in an async context, run in a thread
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(run_in_thread)
            return future.result()
    except RuntimeError:
        # No running loop, safe to run directly
        return asyncio.run(coro)

# === Custom LangChain Tools ===
class LiveCryptoDataTool(BaseTool):
    name: str = "get_live_crypto_data"
    description: str = """Get live cryptocurrency market data from TGX Finance WebSocket.
    
    Args:
        symbol: Cryptocurrency symbol (BTC, ETH, or 'all' for both)
    
    Returns:
        Current price, change, volume, and market data"""
    
    def _run(self, symbol: str = "BTC", run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.tgx_tool.get_market_data(symbol))
        except Exception as e:
            logger.error(f"Live crypto data tool error: {e}")
            return f"Error getting live data: {str(e)}"

class MarketStatusTool(BaseTool):
    name: str = "get_live_market_status"
    description: str = """Get TGX Finance WebSocket connection status and system health.
    
    Returns:
        Connection status, message count, authentication status"""
    
    def _run(self, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.tgx_tool.get_connection_status())
        except Exception as e:
            logger.error(f"Market status tool error: {e}")
            return f"Error getting market status: {str(e)}"

class HistoricalAnalysisTool(BaseTool):
    name: str = "get_crypto_historical_analysis"
    description: str = """Get historical cryptocurrency analysis from Jesse.ai database.
    
    Args:
        symbol: Cryptocurrency symbol (e.g., 'BTCUSDT', 'ETHUSDT')
        timeframe: Chart timeframe ('1m', '5m', '15m', '30m', '1h', '4h', '1D')
    
    Returns:
        Historical price analysis with percentage changes"""
    
    def _run(self, symbol: str, timeframe: str = "1D", run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.jesse_tool.get_historical_analysis(symbol, timeframe))
        except Exception as e:
            logger.error(f"Historical analysis tool error: {e}")
            return f"Error getting historical analysis: {str(e)}"

class HistoricalDataRangeTool(BaseTool):
    name: str = "get_crypto_historical_data_range"
    description: str = """Get historical cryptocurrency data for a specific date range from Jesse.ai database.
    
    Args:
        symbol: Cryptocurrency symbol (e.g., 'BTCUSDT', 'ETHUSDT')  
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        timeframe: Chart timeframe ('1m', '5m', '15m', '30m', '1h', '4h', '1D')
    
    Returns:
        Historical price data and analysis for the specified date range"""
    
    def _run(self, symbol: str, start_date: str, end_date: str, timeframe: str = "1D", 
             run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.jesse_tool.get_historical_data_range(symbol, start_date, end_date, timeframe))
        except Exception as e:
            logger.error(f"Historical data range tool error: {e}")
            return f"Error getting historical data range: {str(e)}"

class RawHistoricalDataTool(BaseTool):
    name: str = "get_crypto_raw_historical_data"
    description: str = """Get raw historical OHLCV data from Jesse.ai database.
    
    Args:
        symbol: Cryptocurrency symbol (e.g., 'BTCUSDT', 'ETHUSDT')
        timeframe: Chart timeframe ('1m', '5m', '15m', '30m', '1h', '4h', '1D')
        limit: Number of data points to return (default: 100)
    
    Returns:
        Raw OHLCV data in JSON format"""
    
    def _run(self, symbol: str, timeframe: str = "1D", limit: int = 100,
             run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.jesse_tool.get_raw_historical_data(symbol, timeframe, limit))
        except Exception as e:
            logger.error(f"Raw historical data tool error: {e}")
            return f"Error getting raw historical data: {str(e)}"

# === FIXED CHART TOOLS - Uses ```graph instead of ```chart ===
class GuaranteedVisualChartTool(BaseTool):
    name: str = "generate_guaranteed_visual_chart"
    description: str = """🎯 PRIMARY CHART TOOL - Generates visual charts compatible with chart-renderer.tsx
    
    Args:
        symbol: Primary crypto symbol (BTC, ETH, BTCUSDT, etc.) 
        days_back: Number of days to look back (7, 14, 30, 90, 365) - default 30
        timeframe: Chart interval ('1m', '5m', '15m', '30m', '1h', '4h', '1D') - default '1D'
        chart_type: 'price' for single crypto, 'comparison' for BTC+ETH multi-line chart
        additional_symbols: List of symbols for comparison (ignored - always uses BTC+ETH)
        comparison_request: Set to True to force BTC+ETH comparison mode
    
    Returns:
        Interactive visual chart in ```graph blocks with GUARANTEED multi-line support
    """
    
    def _run(self, symbol: str, days_back: int = 30, timeframe: str = "1D",
             chart_type: str = "price", additional_symbols: list = None, 
             comparison_request: bool = False,
             run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        
        try:
            logger.info(f"🎯 GENERATING VISUAL CHART: {symbol}, {days_back}d, {timeframe}, type: {chart_type}, comparison: {comparison_request}")
            
            # Ensure tools are initialized
            if not tool_manager or not tool_manager.tools_initialized:
                error_msg = "Chart tools not initialized"
                logger.error(f"❌ {error_msg}")
                raise Exception(error_msg)
            
            # Validate timeframe
            valid_timeframes = ['1m', '5m', '15m', '30m', '1h', '4h', '1D', '1d']
            if timeframe not in valid_timeframes:
                logger.warning(f"Invalid timeframe {timeframe}, defaulting to 1D")
                timeframe = '1D'
            
            # 🔥 CRITICAL: Force BTC+ETH comparison when comparison is requested
            if comparison_request or chart_type == "comparison":
                logger.info("🔥 COMPARISON REQUEST DETECTED - Generating GUARANTEED BTC vs ETH multi-line chart")
                return self._generate_guaranteed_btc_eth_comparison(days_back, timeframe)
            
            # Check for comparison keywords in symbol or implicit comparison request
            symbol_upper = symbol.upper()
            comparison_indicators = ['VS', 'VERSUS', 'COMPARE', 'COMPARISON']
            
            # Auto-detect comparison requests
            if (any(indicator in symbol_upper for indicator in comparison_indicators) or 
                ('BTC' in symbol_upper and 'ETH' in symbol_upper)):
                logger.info("🔥 AUTO-DETECTED COMPARISON REQUEST - Generating GUARANTEED BTC vs ETH multi-line chart")
                return self._generate_guaranteed_btc_eth_comparison(days_back, timeframe)
            
            # Generate single price chart
            try:
                logger.info(f"📊 Generating SINGLE PRICE CHART: {symbol} ({timeframe})")
                
                chart_data_json = run_async_safely(
                    tool_manager.jesse_chart_tool.get_price_chart_data(symbol, days_back, timeframe)
                )
                
            except Exception as data_error:
                logger.error(f"❌ Data retrieval error for {symbol}: {data_error}")
                raise data_error
            
            # Parse and validate the JSON
            chart_data = json.loads(chart_data_json)
            logger.info(f"✅ Single chart data generated successfully")
            
            # Get timeframe display name for response
            timeframe_labels = {
                '1m': '1-Minute',
                '5m': '5-Minute', 
                '15m': '15-Minute',
                '30m': '30-Minute', 
                '1h': 'Hourly',
                '4h': '4-Hour',
                '1D': 'Daily',
                '1d': 'Daily'
            }
            timeframe_display = timeframe_labels.get(timeframe, timeframe)
            
            # SUCCESS: Format the response with ```graph
            response = f"""🎯 **Interactive Price Chart Generated**

**📊 Chart**: {symbol} Price Chart
**📅 Period**: Last {days_back} days  
**⏱️ Timeframe**: {timeframe_display}
**✅ Status**: Ready for Visual Rendering

```graph
{json.dumps(chart_data, indent=2)}
```

**🎨 Interactive Features**:
- {timeframe_display} interval data resolution
- Hover tooltips showing exact values
- Zoom and pan functionality
- Professional crypto-themed colors
- Responsive design for all devices
- Real-time data from Jesse.ai database

**📱 Rendering**: This chart automatically renders as an interactive visualization in your frontend."""

            logger.info(f"✅ SINGLE PRICE CHART SUCCESS: {symbol}")
            return response
            
        except Exception as e:
            logger.error(f"❌ Chart generation error for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            
            # Create error response
            error_response = f"""❌ **Chart Generation Error**

**Error**: Failed to generate chart for {symbol}
**Timeframe**: {timeframe}
**Details**: {str(e)}

```graph
{{"error": "Chart data unavailable", "symbol": "{symbol}", "timeframe": "{timeframe}", "message": "Please try again or contact support"}}
```

**🔧 Troubleshooting**: Try a different symbol, timeframe, or check system status."""

            return error_response

    def _generate_guaranteed_btc_eth_comparison(self, days_back: int = 30, timeframe: str = "1D") -> str:
        """🔥 GUARANTEED: Generate BTC vs ETH comparison chart with MULTIPLE LINES and proper timeframe"""
        try:
            logger.info(f"🎯 GENERATING GUARANTEED BTC vs ETH MULTI-LINE COMPARISON ({timeframe})")
            
            # Ensure tools are initialized
            if not tool_manager or not tool_manager.tools_initialized:
                error_msg = "Chart tools not initialized for comparison"
                logger.error(f"❌ {error_msg}")
                raise Exception(error_msg)
            
            # 🔥 FORCE BTC and ETH symbols - NO EXCEPTIONS
            symbols = ['BTC', 'ETH']
            
            logger.info(f"📊 Calling get_comparison_chart_data for GUARANTEED BTC vs ETH with timeframe {timeframe}...")
            chart_data_json = run_async_safely(
                tool_manager.jesse_chart_tool.get_comparison_chart_data(symbols, days_back, timeframe)
            )
            
            # Parse and validate the JSON
            chart_data = json.loads(chart_data_json)
            logger.info(f"✅ BTC vs ETH comparison chart data parsed successfully")
            
            # 🔍 CRITICAL VALIDATION: Verify multi-line structure
            if 'history' in chart_data and 'content' in chart_data['history']:
                content_count = len(chart_data['history']['content'])
                logger.info(f"📊 Comparison chart contains {content_count} data series (lines)")
                
                if content_count < 2:
                    logger.error(f"❌ CRITICAL ERROR: Expected 2 lines (BTC+ETH) but got {content_count}")
                    raise Exception(f"FAILED: Multi-line comparison chart only created {content_count} series instead of 2")
                
                # Verify we have Bitcoin and Ethereum specifically
                series_names = [series.get('name', 'Unknown') for series in chart_data['history']['content']]
                logger.info(f"📊 Series names detected: {series_names}")
                
                has_bitcoin = any('Bitcoin' in name for name in series_names)
                has_ethereum = any('Ethereum' in name for name in series_names)
                
                if not (has_bitcoin and has_ethereum):
                    logger.error(f"❌ Missing BTC or ETH in series: {series_names}")
                    raise Exception(f"FAILED: Expected Bitcoin and Ethereum series, got: {series_names}")
                
                # Log each series for debugging
                for i, series in enumerate(chart_data['history']['content']):
                    series_name = series.get('name', f'Series {i+1}')
                    series_color = series.get('primary_colour', 'Unknown')
                    data_points = len(series.get('x', []))
                    sample_prices = series.get('price', {}).get('y', [])[:3]
                    logger.info(f"  - Line {i+1}: {series_name} ({series_color}) - {data_points} points")
                    logger.info(f"    Sample prices: {sample_prices}")
                    
            else:
                logger.error("❌ Invalid chart data structure - missing history.content")
                raise Exception("Invalid chart data structure for comparison chart")
            
            # Get timeframe display name
            timeframe_labels = {
                '1m': '1-Minute',
                '5m': '5-Minute', 
                '15m': '15-Minute',
                '30m': '30-Minute', 
                '1h': 'Hourly',
                '4h': '4-Hour',
                '1D': 'Daily',
                '1d': 'Daily'
            }
            timeframe_display = timeframe_labels.get(timeframe, timeframe)
            
            # SUCCESS: Format the response with ```graph
            response = f"""🎯 **Interactive BTC vs ETH Multi-Line Comparison Chart Generated**

**📊 Chart**: Bitcoin vs Ethereum Multi-Line Comparison  
**📅 Period**: Last {days_back} days
**⏱️ Timeframe**: {timeframe_display}
**✅ Status**: Ready for Visual Rendering with {len(chart_data['history']['content'])} SEPARATE Lines

```graph
{json.dumps(chart_data, indent=2)}
```

**🎨 Interactive Features**:
- **{len(chart_data['history']['content'])} SEPARATE cryptocurrency lines** on single graph (Bitcoin: Orange, Ethereum: Blue)
- {timeframe_display} interval data resolution
- Hover tooltips showing exact values for EACH line
- Zoom and pan functionality across all data series
- Professional crypto-themed colors
- Responsive design for all devices
- Real-time data from Jesse.ai database
- Legend showing BOTH cryptocurrencies clearly

**📱 Rendering**: This comparison chart automatically renders as an interactive visualization with **{len(chart_data['history']['content'])} DISTINCT COLORED LINES** in your frontend."""

            # 🚨 CRITICAL: Log the complete response to console BEFORE returning
            print(f"\n" + "="*90)
            print(f"🔥 GUARANTEED BTC vs ETH MULTI-LINE COMPARISON CHART RESPONSE (SENDING TO FRONTEND):")
            print("="*90)
            print(f"TIMEFRAME: {timeframe} ({timeframe_display})")
            print(f"LINES IN CHART: {len(chart_data['history']['content'])}")
            print(f"SERIES NAMES: {[s.get('name') for s in chart_data['history']['content']]}")
            print(f"SERIES COLORS: {[s.get('primary_colour') for s in chart_data['history']['content']]}")
            print(f"TITLE: {chart_data['history'].get('title', 'No title')}")
            print(f"RESPONSE LENGTH: {len(response)} characters")
            print(f"CONTAINS ```graph: {'```graph' in response}")
            print("-"*90)
            print("CHART DATA PREVIEW:")
            print(json.dumps(chart_data, indent=2)[:800] + "..." if len(json.dumps(chart_data)) > 800 else json.dumps(chart_data, indent=2))
            print("="*90)
            
            # Final validation
            if len(chart_data['history']['content']) != 2:
                print("⚠️ WARNING: EXPECTED EXACTLY 2 LINES (BTC+ETH) - COMPARISON MAY NOT WORK PROPERLY")
            else:
                print("✅ SUCCESS: 2 LINES CONFIRMED (BTC+ETH) - GUARANTEED MULTI-LINE COMPARISON READY")
            
            print("="*90 + "\n")

            logger.info(f"✅ GUARANTEED BTC vs ETH MULTI-LINE COMPARISON CHART SUCCESS")
            return response
            
        except Exception as e:
            logger.error(f"❌ GUARANTEED BTC vs ETH multi-line comparison chart generation error: {e}")
            import traceback
            traceback.print_exc()
            
            # Create error response
            error_response = f"""❌ **BTC vs ETH Multi-Line Comparison Chart Generation Error**

**Error**: Failed to generate GUARANTEED BTC vs ETH multi-line comparison chart
**Timeframe**: {timeframe}
**Details**: {str(e)}

```graph
{{"error": "BTC vs ETH multi-line comparison chart data unavailable", "timeframe": "{timeframe}", "message": "System error - please try again or contact support"}}
```

**🔧 Troubleshooting**: Try different timeframe or check system status."""

            # Log error response to console
            print(f"\n" + "="*80)
            print(f"❌ BTC vs ETH MULTI-LINE COMPARISON ERROR RESPONSE (SENDING TO FRONTEND):")
            print("="*80)
            print(error_response)
            print("="*80 + "\n")
            
            return error_response

# === LIVE TRADING TOOLS ===
class AccountStatusTool(BaseTool):
    name: str = "get_account_status"
    description: str = """Get TGX Finance account status, balance, positions, and recent orders.
    
    Returns:
        Complete account overview including balance, open positions, and order history"""
    
    def _run(self, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: Trading tools not initialized"
        
        try:
            return run_async_safely(tool_manager.tgx_trading_tool.get_account_info())
        except Exception as e:
            logger.error(f"Account status tool error: {e}")
            return f"Error getting account status: {str(e)}"

class PlaceLimitOrderTool(BaseTool):
    name: str = "place_limit_order"
    description: str = """⚠️ LIVE TRADING: Place a limit order on TGX Finance.
    
    Args:
        symbol: Trading pair (BTCUSDT, ETHUSDT, BTC, ETH)
        side: Order side (BUY, SELL, LONG, SHORT)
        quantity: Order quantity (1-100)
        price: Limit price in USD
        leverage: Leverage multiplier (1-10, default: 5)
    
    ⚠️ WARNING: This places REAL money trades!"""
    
    def _run(self, symbol: str, side: str, quantity: int, price: float, 
             leverage: int = 5, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: Trading tools not initialized"
        
        try:
            return run_async_safely(tool_manager.tgx_trading_tool.place_limit_order(
                symbol, side, quantity, price, leverage
            ))
        except Exception as e:
            logger.error(f"Place order tool error: {e}")
            return f"Error placing order: {str(e)}"

class CancelOrderTool(BaseTool):
    name: str = "cancel_order"
    description: str = """Cancel an existing order by order ID.
    
    Args:
        order_id: The order ID to cancel
    
    Returns:
        Cancellation confirmation or error message"""
    
    def _run(self, order_id: str, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: Trading tools not initialized"
        
        try:
            return run_async_safely(tool_manager.tgx_trading_tool.cancel_order(order_id))
        except Exception as e:
            logger.error(f"Cancel order tool error: {e}")
            return f"Error cancelling order: {str(e)}"

class ClosePositionTool(BaseTool):
    name: str = "close_position"
    description: str = """⚠️ LIVE TRADING: Close an open position with market order.
    
    Args:
        symbol: Trading pair (BTCUSDT, ETHUSDT, BTC, ETH)
        quantity: Quantity to close (optional, defaults to entire position)
    
    ⚠️ WARNING: This closes positions with REAL money!"""
    
    def _run(self, symbol: str, quantity: Optional[int] = None, 
             run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: Trading tools not initialized"
        
        try:
            return run_async_safely(tool_manager.tgx_trading_tool.close_position(symbol, quantity))
        except Exception as e:
            logger.error(f"Close position tool error: {e}")
            return f"Error closing position: {str(e)}"

# === UPDATED SYSTEM PROMPT FOR CORRECT CHART FORMATTING ===
ENHANCED_CHART_SYSTEM_PROMPT = """You are a comprehensive crypto analysis assistant with GUARANTEED visual chart generation using ```graph blocks.

**🎯 CRITICAL CHART FORMATTING RULES**:

1. **ALWAYS USE generate_guaranteed_visual_chart FOR ANY CHART REQUEST**
2. **For ANY comparison request, ALWAYS generate BTC vs ETH chart with comparison_request=True**
3. **ALWAYS specify timeframe parameter and ensure title reflects the EXACT timeframe**
4. **The tool returns ```graph blocks (NOT ```chart or ```json) - keep them exactly as provided**
5. **EVERY chart request MUST result in a ```graph block for chart-renderer.tsx**
6. **Multi-line charts are GUARANTEED to show 2 separate lines for BTC and ETH**

**🔥 GUARANTEED COMPARISON DETECTION - ALWAYS TRIGGER BTC+ETH MULTI-LINE COMPARISON FOR**:
- ANY mention of "comparison", "compare", "vs", "versus", "both", "together", "against"
- "BTC vs ETH", "Bitcoin and Ethereum", "show both", "side by side", "two lines"
- "multiple cryptocurrencies", "two cryptocurrencies", "crypto comparison"  
- User asks for "BTC and ETH", "Bitcoin versus Ethereum", "compare them"
- "difference between", "which is better", "performance comparison"
- "multi-line", "separate lines", "different colors"

**🕐 CRITICAL TIMEFRAME DETECTION AND TITLE REQUIREMENTS**:
- "hourly", "1 hour", "hour", "1h" → timeframe="1h", title MUST include "(Hourly)"
- "4 hour", "4h", "4-hour" → timeframe="4h", title MUST include "(4-Hour)"  
- "15 minute", "15min", "15m" → timeframe="15m", title MUST include "(15-Minute)"
- "30 minute", "30min", "30m" → timeframe="30m", title MUST include "(30-Minute)"
- "5 minute", "5min", "5m" → timeframe="5m", title MUST include "(5-Minute)"
- "1 minute", "1min", "1m" → timeframe="1m", title MUST include "(1-Minute)"
- "daily", "day", "1d", "1D" → timeframe="1D", title MUST include "(Daily)" - DEFAULT
- "weekly", "week" → timeframe="1D", title MUST include "(Weekly View - Daily Data)"

**⚠️ LIVE TRADING SAFETY PROTOCOLS**:

1. **ALWAYS WARN ABOUT REAL MONEY**: Every trading action involves REAL money and REAL risk
2. **CONFIRM BEFORE TRADING**: Ask for explicit confirmation before placing any trades
3. **VALIDATE PARAMETERS**: Always validate all trading parameters before execution
4. **EXPLAIN RISKS**: Clearly explain the risks of each trade
5. **SUGGEST ACCOUNT CHECK**: Always suggest checking account status before and after trades

**🚨 MANDATORY TRADING CONFIRMATIONS**:
Before ANY trade execution, you MUST:
- Explain the trade clearly (symbol, side, quantity, price, leverage)
- State the maximum possible loss
- Ask "Do you confirm this REAL MONEY trade? Type YES to proceed"
- Only proceed if user explicitly confirms with "YES"

**📊 TRADING TOOL USAGE**:

**Account Management**:
- Use `get_account_status` to show balance, positions, recent orders
- Always check account before suggesting trades
- Show current positions and P&L clearly

**Order Placement**:
- Use `place_limit_order` for limit orders only (market orders not supported)
- Validate: symbol (BTCUSDT/ETHUSDT), side (BUY/SELL), quantity (1-100), price (>0), leverage (1-10)
- Always confirm trade details before execution

**Order Management**:
- Use `cancel_order` with order_id from account status
- Use `close_position` to close positions with market orders
- Always explain consequences of cancellation/closure

**🛡️ SAFETY LIMITS**:
- Maximum quantity: 100 per order
- Maximum leverage: 10x
- Only BTCUSDT and ETHUSDT supported
- Only limit orders supported for entry

**💰 RISK WARNINGS FOR EACH TRADE TYPE**:

**Limit Buy Order**:
"⚠️ RISK: If price drops after you buy, you'll lose money. Max loss = (quantity × price) if price goes to zero."

**Limit Sell Order (Short)**:
"⚠️ RISK: If price rises after you sell short, you'll lose money. Potential unlimited loss with leverage."

**Close Position**:
"⚠️ IMPACT: This will immediately close your position at market price, locking in current profit/loss."

**📈 COMPREHENSIVE RESPONSE FORMAT**:

For account queries: Always use get_account_status and format clearly with balance, positions, and recent orders.

For trading requests:
1. Use get_account_status first
2. Explain the proposed trade
3. Calculate and state maximum risk
4. Ask for explicit confirmation
5. Only execute if user confirms with "YES"
6. Check account status after execution

**Example Trading Flow**:
User: "Buy 5 BTC at $45000"
Assistant: 
1. Checks account status
2. "I can place a limit buy order for 5 BTCUSDT at $45,000. This will cost $225,000 with your available balance. Maximum loss if BTC goes to zero: $225,000. Do you confirm this REAL MONEY trade? Type YES to proceed."
3. Wait for "YES" confirmation
4. Execute trade
5. Show updated account status

**🚫 NEVER**:
- Place trades without explicit user confirmation
- Ignore safety warnings
- Use market orders for entry (only limit orders supported)
- Exceed safety limits
- Trade without checking account first

**⚠️ CRITICAL**: All trading involves REAL MONEY and REAL RISK. Always prioritize user safety and clear communication.

**📊 COMPREHENSIVE RESPONSE STRATEGY FOR CRYPTO QUERIES**:

When asked about BTC or ETH price (e.g., "What is the price of BTC?"), you MUST provide ALL of the following:

1. **CURRENT LIVE PRICE** (from get_live_crypto_data):
   - Current price with proper UP/DOWN formatting: <UP>**X.XX%** (increase)</UP> or <DOWN>**X.XX%** (decrease)</DOWN>
   - 24-hour change with percentage
   - Volume and other live metrics

2. **HISTORICAL ANALYSIS** (from get_crypto_historical_analysis):
   - Monthly percentage changes over the past year
   - Use bullet points with proper UP/DOWN formatting for each month
   - Include context about significant movements

3. **VISUAL CHART** (from generate_guaranteed_visual_chart):
   - Generate a 30-day price chart using generate_guaranteed_visual_chart
   - Format the ```graph data properly as provided by the tool
   - Use daily timeframe (1D) for monthly views
   - Include chart description and interaction notes

**🔥 MANDATORY Response Format for GUARANTEED BTC+ETH Multi-Line Comparison**:
When user asks for ANY comparison, ALWAYS use:
```python
generate_guaranteed_visual_chart(
    symbol="BTC",  # Primary symbol (ignored for comparison)
    chart_type="comparison", 
    comparison_request=True,  # THIS FORCES GUARANTEED BTC+ETH MULTI-LINE
    days_back=30,
    timeframe="1D"  # ALWAYS specify detected timeframe
)
```

**✅ MANDATORY Response Format Examples**:

**For Single Crypto with Specific Timeframe:**
The tool will return a response like:
```graph
{
  "history": {
    "title": "Bitcoin Price - Last 30 Days (Hourly)",
    "xlabel": "Date",
    "content": [{
      "name": "Bitcoin",
      "primary_colour": "#F7931A",
      "x": ["2024-01-01 00:00:00", "2024-01-01 01:00:00"],
      "price": {"y": [45000, 46000], "ylabel": "Price (USD)"}
    }]
  }
}
```

**For GUARANTEED BTC+ETH Multi-Line Comparison with Proper Timeframe Title:**
The tool will return a response like:
```graph
{
  "history": {
    "title": "Bitcoin vs Ethereum Comparison - Last 30 Days (Hourly)",
    "xlabel": "Date", 
    "content": [
      {
        "name": "Bitcoin",
        "primary_colour": "#F7931A",
        "x": ["2024-01-01 00:00:00", "2024-01-01 01:00:00"],
        "price": {"y": [45000, 46000], "ylabel": "Price (USD)"}
      },
      {
        "name": "Ethereum", 
        "primary_colour": "#627EEA",
        "x": ["2024-01-01 00:00:00", "2024-01-01 01:00:00"],
        "price": {"y": [3000, 3100], "ylabel": "Price (USD)"}
      }
    ]
  }
}
```

**🚫 NEVER DO - CRITICAL MISTAKES TO AVOID**:
- Don't change ```graph to ```chart or ```json or anything else
- Don't modify the JSON content inside ```graph blocks
- Don't ignore timeframe specifications in titles  
- Don't use anything other than BTC+ETH for comparisons
- Don't say "here's the data" without the graph block
- Don't create single-line charts when comparison is requested
- Don't create separate charts for each cryptocurrency
- Don't wrap chart data in extra formatting

**🎯 CRITICAL**: ALWAYS force GUARANTEED BTC+ETH multi-line comparison when ANY comparison is requested, regardless of what cryptocurrencies the user mentions. The system is optimized for Bitcoin vs Ethereum comparisons with GUARANTEED multi-line output showing 2 distinct colored lines.

**🔥 TITLE VERIFICATION**: Every chart title MUST reflect the detected timeframe. Never leave timeframe out of the title.

**📊 MULTI-LINE GUARANTEE**: Every comparison chart MUST contain exactly 2 series (BTC + ETH) in the content array, each with their own name, color, and data arrays.

**🚨 CHART RENDERER COMPATIBILITY**: The format uses history.content array with multiple entries (one per line) - this is the CORRECT format for chart-renderer.tsx to process multiple lines.

**Example GUARANTEED BTC+ETH Multi-Line Comparison Response with Correct Timeframe**:
"I'll generate an interactive multi-line comparison chart showing Bitcoin and Ethereum as two separate colored lines on the same graph with hourly data:

[The tool returns the ```graph block exactly as provided - DO NOT MODIFY IT]

This interactive multi-line chart displays Bitcoin (orange line) and Ethereum (blue line) as TWO SEPARATE COLORED LINES on a single graph with hourly intervals, including hover tooltips, zoom, and pan capabilities."

**PRICE FORMATTING RULES**:
- **For price increases:** Use <UP>**X.XX%** (increase)</UP>
- **For price decreases:** Use <DOWN>**X.XX%** (decrease)</DOWN>
- Always include "**" around the percentage and "(increase)" or "(decrease)" text
- Use proper markdown formatting throughout responses"""

# === FastAPI App Setup ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown"""
    global tool_manager
    
    logger.info("🚀 Starting MCP Master Server with Fixed Chart Formatting...")
    
    tool_manager = MCPToolManager()
    await tool_manager.initialize()
    
    app.state.tool_manager = tool_manager
    
    logger.info("✅ MCP Master Server with Fixed Charts startup complete!")
    
    yield
    
    logger.info("🛑 Shutting down MCP Master Server...")
    if tool_manager:
        await tool_manager.close()
    logger.info("✅ Shutdown complete")

app = FastAPI(
    title="MCP Crypto Analysis Server - Fixed Charts",
    description="Master MCP server with FIXED chart formatting using ```graph blocks for chart-renderer.tsx compatibility",
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

# === API Endpoints ===
@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    """OpenAI-compatible chat completions endpoint with FIXED chart formatting"""
    try:
        if not tool_manager or not tool_manager.tools_initialized:
            raise HTTPException(status_code=503, detail="MCP tools not initialized")
        
        # Extract latest user message
        messages = request.messages
        if not messages:
            raise HTTPException(status_code=400, detail="Messages array required")
            
        last_user_msg = next((m for m in reversed(messages) if m.get('role') == 'user'), None)
        if not last_user_msg:
            raise HTTPException(status_code=400, detail="No user message found")
            
        user_input = last_user_msg.get('content', '')
        if isinstance(user_input, list):
            user_input = ' '.join(item.get('text', '') for item in user_input if isinstance(item, dict))
        
        logger.info(f"💬 Chat request: '{user_input}'")
        
        # 🔥 ENHANCED DETECTION: Chart and comparison requests with timeframe support
        chart_keywords = ['chart', 'graph', 'plot', 'visualize', 'show', 'display', 
                        'price chart', 'comparison', 'vs', 'versus', 'compare',
                        'trend', 'performance', 'history', 'over time']
        
        comparison_keywords = [
            'comparison', 'compare', 'vs', 'versus', 'both', 'together', 'against',
            'side by side', 'and', 'two', 'multiple', 'crypto comparison',
            'bitcoin and ethereum', 'btc and eth', 'btc vs eth', 'bitcoin vs ethereum',
            'show both', 'compare them', 'between', 'difference between'
        ]
        
        # Enhanced timeframe detection
        timeframe_patterns = {
            '1m': ['1 minute', '1min', '1m', 'minute'],
            '5m': ['5 minute', '5min', '5m', '5 minutes'],
            '15m': ['15 minute', '15min', '15m', '15 minutes'],
            '30m': ['30 minute', '30min', '30m', '30 minutes'],
            '1h': ['1 hour', '1h', 'hour', 'hourly', '1hr'],
            '4h': ['4 hour', '4h', '4 hours', '4hr', '4-hour'],
            '1D': ['daily', 'day', '1d', '1D', '1 day', 'weekly', 'week']
        }
        
        detected_timeframe = '1D'  # Default
        for timeframe, patterns in timeframe_patterns.items():
            if any(pattern.lower() in user_input.lower() for pattern in patterns):
                detected_timeframe = timeframe
                logger.info(f"🕐 DETECTED TIMEFRAME: {detected_timeframe}")
                break
        
        is_chart_request = any(keyword in user_input.lower() for keyword in chart_keywords)
        is_comparison_request = any(keyword in user_input.lower() for keyword in comparison_keywords)
        
        # Check for specific crypto mentions that suggest comparison
        has_btc = any(term in user_input.upper() for term in ['BTC', 'BITCOIN', 'BTCUSDT'])
        has_eth = any(term in user_input.upper() for term in ['ETH', 'ETHEREUM', 'ETHUSDT', 'ETHER'])
        
        if has_btc and has_eth:
            is_comparison_request = True
            logger.info("🔥 DETECTED: User mentioned both BTC and ETH - FORCING BTC vs ETH comparison mode")

        def generate_response():
            try:
                # Create LLM
                client = AzureChatOpenAI(
                    azure_endpoint=endpoint,
                    azure_deployment=deployment,
                    openai_api_version=api_version,
                    api_key=azure_api_key,
                )
                
                # Create tool instances - INCLUDING FIXED CHART TOOLS
                tools = [
                    LiveCryptoDataTool(),
                    MarketStatusTool(),
                    HistoricalAnalysisTool(),
                    HistoricalDataRangeTool(),
                    RawHistoricalDataTool(),
                    GuaranteedVisualChartTool(),
                    AccountStatusTool(),
                    PlaceLimitOrderTool(),
                    CancelOrderTool(),
                    ClosePositionTool()
                ]
                
                # Bind tools to client
                client_with_tools = client.bind_tools(tools)
                
                # FIXED SYSTEM PROMPT FOR PROPER CHART FORMATTING
                messages_with_tools = [
                    SystemMessage(ENHANCED_CHART_SYSTEM_PROMPT),
                    HumanMessage(user_input),
                ]
                
                # Run LangChain agent
                result = client_with_tools.invoke(messages_with_tools)
                messages_with_tools.append(result)
                
                # Process tool calls with enhanced parameter injection
                if result.tool_calls:
                    for tool_call in result.tool_calls:
                        # Log tool call info
                        logger.info(f"🔧 Calling tool: {tool_call['name']} with args: {tool_call['args']}")
                        
                        # Special handling for chart generation with auto-injection
                        if tool_call["name"] == "generate_guaranteed_visual_chart":
                            # Auto-inject timeframe parameter
                            if 'timeframe' not in tool_call["args"] or tool_call["args"]["timeframe"] == "1D":
                                tool_call["args"]["timeframe"] = detected_timeframe
                                logger.info(f"🕐 AUTO-INJECTED timeframe: {detected_timeframe}")
                            
                            # Force BTC+ETH comparison if detected
                            if is_comparison_request:
                                tool_call["args"]["comparison_request"] = True
                                tool_call["args"]["chart_type"] = "comparison"
                                tool_call["args"]["symbol"] = "BTC"
                                tool_call["args"]["additional_symbols"] = ["ETH"]
                                logger.info(f"🔥 FORCED BTC+ETH COMPARISON: {tool_call['args']}")
                        
                        # Find the matching tool
                        tool_found = None
                        for tool in tools:
                            if tool.name == tool_call["name"]:
                                tool_found = tool
                                break
                        
                        if tool_found:
                            try:
                                # Extract arguments from the tool call
                                tool_args = tool_call["args"]
                                
                                # Execute the tool with extracted arguments
                                tool_result = tool_found._run(**tool_args)
                                
                                # 🔥 CRITICAL: Log tool result for chart verification
                                if "```graph" in tool_result:
                                    print(f"\n🎯 CHART TOOL RESULT VERIFICATION:")
                                    print("="*60)
                                    print(f"Tool: {tool_call['name']}")
                                    print(f"Timeframe: {tool_args.get('timeframe', 'N/A')}")
                                    print(f"Comparison Mode: {tool_args.get('comparison_request', False)}")
                                    print(f"Contains ```graph: True")
                                    
                                    # Extract and analyze the graph data
                                    try:
                                        start = tool_result.find('```graph\n') + 9
                                        end = tool_result.find('\n```', start)
                                        if end == -1:
                                            end = tool_result.find('```', start)
                                        graph_json = tool_result[start:end]
                                        graph_data = json.loads(graph_json)
                                        
                                        if 'history' in graph_data and 'content' in graph_data['history']:
                                            series_count = len(graph_data['history']['content'])
                                            title = graph_data['history'].get('title', 'No title')
                                            series_names = [s.get('name', 'Unknown') for s in graph_data['history']['content']]
                                            
                                            print(f"Series count: {series_count}")
                                            print(f"Chart title: {title}")
                                            print(f"Series names: {series_names}")
                                            
                                            if series_count >= 2:
                                                print("✅ MULTI-LINE CHART CONFIRMED")
                                            else:
                                                print("📊 SINGLE-LINE CHART")
                                                
                                    except Exception as parse_error:
                                        print(f"Graph parsing error: {parse_error}")
                                    
                                    print("="*60 + "\n")
                                
                                tool_message = ToolMessage(
                                    content=tool_result,
                                    tool_call_id=tool_call["id"]
                                )
                                messages_with_tools.append(tool_message)
                            except Exception as e:
                                logger.error(f"Tool execution error: {e}")
                                error_message = ToolMessage(
                                    content=f"Tool execution error: {str(e)}",
                                    tool_call_id=tool_call["id"]
                                )
                                messages_with_tools.append(error_message)
                        else:
                            logger.warning(f"Tool not found: {tool_call['name']}")
                            error_message = ToolMessage(
                                content=f"Tool not found: {tool_call['name']}",
                                tool_call_id=tool_call["id"]
                            )
                            messages_with_tools.append(error_message)
                
                # Generate final response with streaming
                message_id = f"msg-{abs(hash(user_input)) % 10000}"
                
                for chunk in client_with_tools.stream(messages_with_tools):
                    if chunk.content:
                        # Enhanced chunk data for chart compatibility
                        if '```graph' in chunk.content:
                            chunk_data = {
                                "type": "chart",
                                "id": message_id,
                                "data": chunk.content,
                                "chart_format": "graph",
                                "interactive": True,
                                "multi_line": is_comparison_request,
                                "timeframe": detected_timeframe,
                                "compatible_with": "chart-renderer.tsx"
                            }
                            logger.info(f"📊 STREAMING CHART TO FRONTEND: format=graph, multi_line={is_comparison_request}, timeframe={detected_timeframe}")
                        else:
                            chunk_data = {
                                "type": "text",
                                "id": message_id,
                                "data": chunk.content
                            }
                        
                        yield f'{json.dumps([chunk_data])}\n'
                        
            except Exception as e:
                logger.error(f"Response generation error: {e}")
                error_msg = f"Error: {str(e)}"
                error_data = {
                    "type": "text",
                    "id": f"error-{abs(hash(str(e))) % 1000}",
                    "data": error_msg
                }
                yield f'{json.dumps([error_data])}\n'
        
        return StreamingResponse(
            generate_response(),
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Chart-Format": "graph",
                "X-Chart-Compatible": "chart-renderer-tsx",
                "X-Chart-Timeframe": detected_timeframe,
                "X-Comparison-Mode": str(is_comparison_request).lower()
            }
        )
        
    except Exception as e:
        logger.error(f"Chat completions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/ask")
async def ask_endpoint(user_input: str):
    """Legacy ask endpoint for compatibility"""
    try:
        if not tool_manager or not tool_manager.tools_initialized:
            raise HTTPException(status_code=503, detail="MCP tools not initialized")
            
        # Create mock request
        mock_request = ChatRequest(messages=[{"role": "user", "content": user_input}])
        return await chat_completions(mock_request)
        
    except Exception as e:
        logger.error(f"Ask endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        health_status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "version": "2.0.0 - Fixed Chart Formatting",
            "services": {
                "mcp_tools": tool_manager.tools_initialized if tool_manager else False,
                "tgx_finance": "available" if tool_manager and tool_manager.tools_initialized else "unavailable",
                "jesse_database": "available" if tool_manager and tool_manager.tools_initialized else "unavailable",
                "jesse_chart_tool": "available" if tool_manager and tool_manager.tools_initialized else "unavailable",
                "azure_openai": "available" if azure_api_key else "unavailable"
            },
            "chart_compatibility": {
                "format": "```graph blocks",
                "frontend_component": "chart-renderer.tsx",
                "multi_line_support": "BTC vs ETH guaranteed",
                "timeframe_support": "1m, 5m, 15m, 30m, 1h, 4h, 1D",
                "auto_detection": "Comparison keywords and timeframes",
                "console_logging": "Full debug output enabled"
            },
            "tools": [
                "get_live_crypto_data",
                "get_live_market_status", 
                "get_crypto_historical_analysis",
                "get_crypto_historical_data_range",
                "get_crypto_raw_historical_data",
                "generate_guaranteed_visual_chart [FIXED]",
                "get_account_status [NEW]",
                "place_limit_order [LIVE TRADING]",
                "cancel_order [LIVE TRADING]",
                "close_position [LIVE TRADING]"
            ]
        }
        
        if not tool_manager or not tool_manager.tools_initialized:
            health_status["status"] = "degraded"
            
        return health_status
        
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@app.get("/")
async def root():
    """API documentation root"""
    return {
        "title": "MCP Crypto Analysis Server - FIXED CHARTS",
        "version": "2.0.0", 
        "description": "Master MCP server with FIXED chart formatting using ```graph blocks for chart-renderer.tsx",
        "fixes_applied": [
            "✅ Correct ```graph block formatting (not ```chart or ```json)",
            "✅ Enhanced system prompt with proper chart instructions",
            "✅ GuaranteedVisualChartTool with BTC+ETH multi-line support",
            "✅ Automatic timeframe detection and injection",
            "✅ Console logging for complete debugging visibility",
            "✅ Proper JSON structure preservation in ```graph blocks"
        ],
        "endpoints": {
            "/v1/chat/completions": "OpenAI-compatible chat with FIXED chart formatting (POST)",
            "/ask": "Legacy ask endpoint with chart fixes (GET)",
            "/health": "Health check with chart compatibility status",
        },
        "mcp_tools": [
            "get_live_crypto_data - Real-time market data from TGX Finance",
            "get_live_market_status - WebSocket connection status",
            "get_crypto_historical_analysis - Historical analysis from Jesse.ai",
            "get_crypto_historical_data_range - Date range historical data",
            "get_crypto_raw_historical_data - Raw OHLCV data",
            "generate_guaranteed_visual_chart - FIXED visual charts with ```graph blocks",
            "get_account_status - Account balance, positions, and order history",
            "place_limit_order - Place limit orders (LIVE TRADING)",
            "cancel_order - Cancel existing orders",
            "close_position - Close positions with market orders (LIVE TRADING)"
        ],
        "chart_features": {
            "format": "```graph blocks (compatible with chart-renderer.tsx)",
            "single_charts": "Individual cryptocurrency price charts",
            "comparison_charts": "BTC vs ETH multi-line charts with 2 separate data series",
            "timeframes": "1m, 5m, 15m, 30m, 1h, 4h, 1D with auto-detection",
            "styling": "Professional crypto colors (Bitcoin: #F7931A, Ethereum: #627EEA)",
            "interactivity": "Hover tooltips, zoom, pan, responsive design"
        },
        "debugging": {
            "console_output": "All chart generations logged to server console",
            "validation": "Automatic verification of ```graph block structure",
            "error_tracking": "Detailed error messages and stack traces",
            "tool_result_analysis": "JSON structure and series count verification"
        },
        "supported_symbols": ["BTC", "ETH", "BTCUSDT", "ETHUSDT"],
        "supported_timeframes": ["1m", "5m", "15m", "30m", "1h", "4h", "1D"],
        "comparison_triggers": [
            "compare", "vs", "versus", "comparison", "both", "together",
            "BTC and ETH", "Bitcoin and Ethereum", "show both", "side by side"
        ]
    }

if __name__ == "__main__":
    import uvicorn
    
    try:
        port = int(os.getenv("PORT", "8000"))
        logger.info(f"🚀 Starting FIXED MCP Master Server on port {port}")
        
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            timeout_keep_alive=60,
            reload=False,
            access_log=True
        )
        
    except Exception as e:
        logger.error(f"❌ Failed to start server: {e}")
        raise